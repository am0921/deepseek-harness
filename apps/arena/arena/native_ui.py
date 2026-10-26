"""Prefix-stripping proxy for unmodified Dsh Web assets, authentication, and live streams."""

from __future__ import annotations

import asyncio
from urllib.parse import urlencode

from aiohttp import ClientSession, ClientTimeout, ClientWSTimeout, WSMsgType, web
from yarl import URL

from .coordinator import Coordinator

HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "content-length",
    "content-encoding",
}


class NativeUI:
    """Give each standard Dsh app its own mount and browser-cookie path."""

    def __init__(self, controller: Coordinator, client: ClientSession) -> None:
        self.controller = controller
        self.client = client

    async def proxy(self, request: web.Request) -> web.StreamResponse:
        """Preserve Host/Origin, strip the mount, scope cookies, and stream without buffering."""
        lane = self.controller.lanes.get(request.match_info["lane"])
        if lane is None or lane.runtime is None or lane.runtime.web_port is None:
            raise web.HTTPServiceUnavailable(text="Dsh is preparing or this view has been stopped.")
        runtime = lane.runtime
        prefix = f"/dsh/{lane.id}/"
        tail = request.match_info["tail"]
        # Dsh combo scripts use `plugins/??a,b&rev=...`; decoding/re-encoding that query changes it.
        relative = request.raw_path.removeprefix(prefix)
        url = URL(f"http://127.0.0.1:{runtime.web_port}/{relative}", encoded=True)
        headers = {key: value for key, value in request.headers.items() if key.lower() not in HOP_HEADERS}
        if request.headers.get("Upgrade", "").lower() == "websocket":
            # Each hop negotiates its own framing. Forwarding a browser's compression offer without
            # enabling the upstream decoder breaks Dsh's first compressed history frame.
            socket_headers = {
                key: value for key, value in headers.items() if not key.lower().startswith("sec-websocket-")
            }
            outgoing = await self.client.ws_connect(
                url,
                headers=socket_headers,
                compress=15,
                max_msg_size=32 * 1024 * 1024,
                timeout=ClientWSTimeout(ws_close=5),
            )
            incoming = web.WebSocketResponse(max_msg_size=32 * 1024 * 1024)
            await incoming.prepare(request)

            async def copy(source, target):
                async for message in source:
                    if message.type == WSMsgType.TEXT:
                        await target.send_str(message.data)
                    elif message.type == WSMsgType.BINARY:
                        await target.send_bytes(message.data)
                    elif message.type in {WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.ERROR}:
                        break

            tasks = [
                asyncio.create_task(copy(incoming, outgoing)),
                asyncio.create_task(copy(outgoing, incoming)),
            ]
            try:
                await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                await outgoing.close()
                await incoming.close()
            return incoming
        body = await request.read() if request.can_read_body else None
        params = list(request.query.items())
        upstream = await self.client.request(
            request.method,
            url,
            headers=headers,
            data=body,
            allow_redirects=False,
            timeout=ClientTimeout(total=None, connect=10),
        )
        if upstream.status == 401 and tail in {"", "index.html"} and runtime.ui_token:
            upstream.release()
            params = [(key, value) for key, value in params if key != "token"]
            params.append(("token", runtime.ui_token))
            root = URL(f"http://127.0.0.1:{runtime.web_port}/").with_query(params)
            upstream = await self.client.get(root, headers=headers, allow_redirects=False)
        try:
            response = web.StreamResponse(status=upstream.status)
            for key, value in upstream.headers.items():
                if key.lower() in HOP_HEADERS or key.lower() == "set-cookie":
                    continue
                if key.lower() == "location" and value == "./" and "arenaSession" in request.query:
                    value = "./?" + urlencode({"arenaSession": request.query["arenaSession"]})
                response.headers[key] = value
            for cookie in upstream.headers.getall("Set-Cookie", []):
                response.headers.add("Set-Cookie", cookie.replace("Path=/;", f"Path={prefix};"))
            await response.prepare(request)
            async for chunk in upstream.content.iter_any():
                await response.write(chunk)
            await response.write_eof()
            return response
        except ConnectionResetError:
            return response
        finally:
            upstream.release()
