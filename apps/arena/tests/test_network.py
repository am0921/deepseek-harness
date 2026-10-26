"""Real Docker evidence: inference remains reachable while host control TCP is blocked."""

import asyncio
import json
import os
import secrets

import pytest
from aiohttp import ClientSession, web
from aiohttp.test_utils import TestServer

from arena import docker
from arena.config import Limits
from arena.network import Relay


async def test_cancel_during_allocation_waits_for_ack_and_preserves_cleanup_ownership(monkeypatch):
    entered, complete = asyncio.Event(), asyncio.Event()
    calls = []

    async def command(*arguments, **_kwargs):
        calls.append(arguments)
        if arguments[:2] == ("network", "create"):
            entered.set()
            await complete.wait()
        return 0, b"", b""

    monkeypatch.setattr(docker, "command", command)
    relay = Relay("owned-cancel-test", 0, Limits(), 8766, docker.AGENT_IMAGE)
    preparation = asyncio.create_task(relay.prepare())
    await entered.wait()
    preparation.cancel()
    complete.set()
    try:
        with pytest.raises(asyncio.CancelledError):
            await preparation
        assert relay.network_created
    finally:
        await relay.close()
    assert calls[-1] == ("network", "rm", relay.network)


async def test_failed_allocation_does_not_claim_or_remove_an_existing_network(monkeypatch):
    calls = []

    async def command(*arguments, **_kwargs):
        calls.append(arguments)
        raise RuntimeError("name already exists")

    monkeypatch.setattr(docker, "command", command)
    relay = Relay("unowned-name", 0, Limits(), 8766, docker.AGENT_IMAGE)
    with pytest.raises(RuntimeError, match="already exists"):
        await relay.prepare()
    await relay.close()
    assert len(calls) == 1


async def test_cancel_during_workspace_creation_removes_the_acknowledged_volume(monkeypatch):
    entered, complete = asyncio.Event(), asyncio.Event()
    calls = []

    async def command(*arguments, **_kwargs):
        calls.append(arguments)
        if arguments[:2] == ("volume", "create"):
            entered.set()
            await complete.wait()
            return 0, b"owned-workspace\n", b""
        return 0, b"", b""

    monkeypatch.setattr(docker, "command", command)
    allocation = asyncio.create_task(docker.create_workspace())
    await entered.wait()
    allocation.cancel()
    complete.set()
    with pytest.raises(asyncio.CancelledError):
        await allocation
    assert calls[-1] == ("volume", "rm", "owned-workspace")


@pytest.mark.skipif(os.environ.get("ARENA_DOCKER_TESTS") != "1", reason="requires local Docker")
async def test_agent_can_reach_only_the_model_relay_and_cannot_dial_host_control():
    control_calls = []
    model_called = asyncio.Event()

    async def gateway(_request):
        model_called.set()
        return web.Response(text="gateway-ok")

    async def control(_request):
        control_calls.append(True)
        return web.Response(text="control-must-stay-private")

    gateway_app, control_app = web.Application(), web.Application()
    gateway_app.router.add_get("/", gateway)
    control_app.router.add_get("/", control)
    async with (
        TestServer(gateway_app, host="0.0.0.0") as model,
        TestServer(control_app) as private,
    ):
        name = "dsh-arena-network-test-" + secrets.token_hex(6)
        relay = Relay(name, 0, Limits(), model.port, docker.AGENT_IMAGE)
        client_name = name + "-client"
        client = None
        try:
            await relay.prepare()
            _, host, _ = await docker.command(
                "exec",
                relay.name,
                "python3",
                "-c",
                "import socket; print(socket.gethostbyname('host.docker.internal'))",
            )
            program = f"""
import http.server, json, socket, urllib.request
class UI(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'native-ui-ok')
    def log_message(self, *arguments):
        pass
server = http.server.HTTPServer(('0.0.0.0', 3080), UI)
server.timeout = 5
body = urllib.request.urlopen('http://arena-relay:{model.port}/', timeout=5).read().decode()
blocked = False
try:
    connection = socket.create_connection(({host.decode().strip()!r}, {private.port}), timeout=1)
    connection.close()
except OSError:
    blocked = True
print(json.dumps({{'body': body, 'controlBlocked': blocked}}))
server.handle_request()
server.server_close()
"""
            client = asyncio.create_task(
                docker.command(
                    "run",
                    "--rm",
                    "-i",
                    "--name",
                    client_name,
                    "--network",
                    relay.network,
                    "--network-alias",
                    "arena-agent",
                    "--cap-drop",
                    "ALL",
                    "--entrypoint",
                    "python3",
                    docker.AGENT_IMAGE,
                    "-",
                    data=program.encode(),
                    timeout=15,
                )
            )
            await asyncio.wait_for(model_called.wait(), 10)
            _, published, _ = await docker.command("port", relay.name, f"{relay.ui_port}/tcp")
            ui_port = int(published.decode().strip().rsplit(":", 1)[1])
            async with ClientSession() as browser:
                async with browser.get(f"http://127.0.0.1:{ui_port}/") as response:
                    assert await response.text() == "native-ui-ok"
            _, output, _ = await client
            assert json.loads(output) == {"body": "gateway-ok", "controlBlocked": True}
            assert control_calls == []
        finally:
            if client and not client.done():
                client.cancel()
                await asyncio.gather(client, return_exceptions=True)
            await docker.remove(client_name)
            await relay.close()
