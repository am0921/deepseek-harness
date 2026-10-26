"""Loopback control UI and a separate, authenticated Docker-to-provider gateway."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import secrets
import webbrowser
from dataclasses import replace

from aiohttp import ClientSession, ClientTimeout, WSMsgType, web

from .config import APP, STATE, Limits, Rates, providers, validate_endpoint
from .coordinator import Coordinator
from .demo import Demo
from .gateway import Gateway
from .native_ui import NativeUI


async def serve(port: int, *, demo: bool = False, open_browser: bool = False) -> None:
    """Serve the control plane on loopback and only credential-scoped relays on the gateway port."""
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    csrf = secrets.token_urlsafe(32)
    controller = Coordinator(providers(), port + 1)
    demo_server = Demo()
    if demo:
        for name, provider in controller.providers.items():
            provider.base_url = f"http://127.0.0.1:{port + 2}"
            provider.models_url = f"http://127.0.0.1:{port + 2}/models"
            provider.key = f"demo-{name}"
    client = ClientSession(timeout=ClientTimeout(total=300))
    gateway = Gateway(controller, client)
    native = NativeUI(controller, client)

    @web.middleware
    async def control_security(request: web.Request, handler):
        allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        if request.host not in allowed_hosts:
            raise web.HTTPForbidden(text="Unexpected Host header")
        if request.method not in {"GET", "HEAD"} and not request.path.startswith("/dsh/"):
            if request.headers.get("Origin") not in {f"http://{host}" for host in allowed_hosts}:
                raise web.HTTPForbidden(text="Control requests must originate from this UI")
            if not secrets.compare_digest(request.headers.get("X-Arena-Token", ""), csrf):
                raise web.HTTPForbidden(text="Invalid control token")
        try:
            response = await handler(request)
        except (ValueError, TypeError) as error:
            response = web.json_response({"error": str(error)}, status=400)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        if not request.path.startswith("/dsh/"):
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; style-src 'self'; script-src 'self'; "
                "connect-src 'self'; frame-ancestors 'none'"
            )
        return response

    async def index(_request):
        return web.FileResponse(APP / "static/index.html")

    async def state(_request):
        return web.json_response({**controller.public(), "csrf": csrf, "demo": demo})

    async def start(_request):
        run_id = await controller.start()
        return web.json_response({"runId": run_id})

    async def stop(_request):
        await controller.stop()
        return web.json_response({"stopped": True})

    async def configure(request):
        if controller.active:
            raise ValueError("Settings are locked while an experiment is running")
        data = await request.json()
        updates = {}
        for name, current in controller.providers.items():
            item = data.get("providers", {}).get(name, {})
            base = validate_endpoint(item.get("baseUrl", current.base_url))
            model = item.get("model", current.model)
            if not isinstance(model, str) or not model.strip() or len(model) > 200:
                raise ValueError("Model id must be a nonempty string of at most 200 characters")
            key = item.get("key", "").strip() or current.key
            rates = Rates(**item["rates"]) if "rates" in item else current.rates
            updates[name] = replace(current, base_url=base, model=model, key=key, rates=rates)
        limits = Limits(**data["limits"]) if "limits" in data else controller.limits
        controller.providers = updates
        controller.limits = limits
        if data.get("saveKeys") and not demo:
            path = STATE / "keys.env"
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(descriptor, "w") as output:
                for name, variable in (("official", "DEEPSEEK_API_KEY"), ("engy", "ENGY_API_KEY")):
                    key = updates[name].key
                    if any(character in key for character in "\n\r\"'"):
                        raise ValueError("API keys must not contain quotes or newlines")
                    output.write(f"{variable}={key}\n")
        return web.json_response({"saved": True})

    async def socket(request):
        if request.headers.get("Origin") not in {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}:
            raise web.HTTPForbidden(text="WebSocket must originate from this UI")
        response = web.WebSocketResponse(heartbeat=20, max_msg_size=1024)
        await response.prepare(request)
        queue = asyncio.Queue(maxsize=512)
        controller.subscribers.add(queue)
        await response.send_json({"kind": "snapshot", "state": {**controller.public(), "demo": demo}})

        async def send():
            while True:
                item = await queue.get()
                await response.send_json(item)
                if item["kind"] == "reconnect":
                    await response.close()
                    break

        sender = asyncio.create_task(send())
        try:
            async for message in response:
                if message.type == WSMsgType.ERROR:
                    break
        finally:
            sender.cancel()
            await asyncio.gather(sender, return_exceptions=True)
            controller.subscribers.discard(queue)
        return response

    async def artifact(request):
        root = (STATE / "runs").resolve()
        target = (root / request.match_info["path"]).resolve()
        if (
            not target.is_relative_to(root)
            or not target.is_file()
            or target.suffix not in {".json", ".jsonl", ".tar"}
        ):
            raise web.HTTPNotFound()
        return web.FileResponse(
            target, headers={"Content-Disposition": f'attachment; filename="{target.name}"'}
        )

    ui = web.Application(middlewares=[control_security], client_max_size=32 * 1024 * 1024)
    ui.add_routes(
        [
            web.get("/", index),
            web.get("/api/state", state),
            web.post("/api/start", start),
            web.post("/api/stop", stop),
            web.post("/api/config", configure),
            web.get("/ws", socket),
            web.get("/artifacts/{path:.*}", artifact),
        ]
    )
    ui.router.add_route("*", "/dsh/{lane}/{tail:.*}", native.proxy)
    ui.router.add_static("/static", APP / "static", show_index=False)
    relay = web.Application(client_max_size=32 * 1024 * 1024)
    relay.router.add_route("*", "/gateway/{run}/{lane}/{tail:.*}", gateway.relay)
    runners = [web.AppRunner(ui, access_log=None), web.AppRunner(relay, access_log=None)]
    if demo:
        mock = web.Application(client_max_size=32 * 1024 * 1024)
        mock.add_routes(
            [web.get("/models", demo_server.models), web.post("/v1/messages", demo_server.messages)]
        )
        runners.append(web.AppRunner(mock, access_log=None))
    try:
        for index, runner in enumerate(runners):
            await runner.setup()
            await web.TCPSite(runner, "0.0.0.0" if index == 1 else "127.0.0.1", port + index).start()

        async def heartbeat():
            ticks = 0
            while True:
                if controller.active:
                    for queue in tuple(controller.subscribers):
                        if not queue.full():
                            queue.put_nowait(
                                {
                                    "kind": "status",
                                    "state": {**controller.public(), "events": [], "task": "", "demo": demo},
                                }
                            )
                    ticks += 1
                    if ticks % 60 == 1:
                        asyncio.create_task(gateway.ping())
                await asyncio.sleep(0.25)

        ticker = asyncio.create_task(heartbeat())
        print(f"Dsh Arena{' DEMO — no paid API calls' if demo else ''}: http://127.0.0.1:{port}", flush=True)
        if open_browser:
            webbrowser.open(f"http://127.0.0.1:{port}")
        try:
            await asyncio.Event().wait()
        finally:
            ticker.cancel()
            await asyncio.gather(ticker, return_exceptions=True)
    finally:
        await controller.stop()
        for runner in reversed(runners):
            await runner.cleanup()
        await client.close()


def main() -> None:
    """Launch the local experiment controller; model execution still uses the normal dsh profile."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--demo", action="store_true")
    parser.add_argument("--open", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING)
    try:
        asyncio.run(serve(args.port, demo=args.demo, open_browser=args.open))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
