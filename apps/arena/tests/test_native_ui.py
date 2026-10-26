"""Native Dsh proxy checks over real local HTTP and WebSocket transports."""

from types import SimpleNamespace

from aiohttp import ClientSession, web
from aiohttp.test_utils import TestClient, TestServer
from yarl import URL

from arena.native_ui import NativeUI


async def test_native_combo_script_keeps_double_question_mark_query_bytes():
    async def combo(request):
        return web.Response(text=request.raw_path)

    upstream = web.Application()
    upstream.router.add_get("/plugins/", combo)
    async with TestServer(upstream) as source, ClientSession() as client:
        runtime = SimpleNamespace(web_port=source.port, ui_token="token")
        controller = SimpleNamespace(lanes={"engy": SimpleNamespace(id="engy", runtime=runtime)})
        app = web.Application()
        app.router.add_route("*", "/dsh/{lane}/{tail:.*}", NativeUI(controller, client).proxy)
        async with TestClient(TestServer(app)) as browser:
            url = URL(
                "/dsh/engy/plugins/??@deepseek-ai/test/client.js&rev=abc",
                encoded=True,
            )
            response = await browser.get(url)
            assert await response.text() == "/plugins/??@deepseek-ai/test/client.js&rev=abc"


async def test_native_index_keeps_authentication_and_scopes_cookie_to_its_pane():
    observed = []

    async def index(request):
        observed.append(dict(request.headers))
        if request.query.get("token") == "private-launch-token":
            response = web.Response(status=303, headers={"Location": "./"})
            response.headers.add(
                "Set-Cookie", "dsh-auth-test=signed-cookie; Path=/; HttpOnly; SameSite=Strict"
            )
            return response
        if "dsh-auth-test=signed-cookie" not in request.headers.get("Cookie", ""):
            return web.Response(status=401)
        return web.Response(text="original Dsh index", content_type="text/html")

    upstream = web.Application()
    upstream.router.add_get("/", index)
    async with TestServer(upstream) as source, ClientSession() as client:
        runtime = SimpleNamespace(web_port=source.port, ui_token="private-launch-token")
        controller = SimpleNamespace(lanes={"official": SimpleNamespace(id="official", runtime=runtime)})
        app = web.Application()
        app.router.add_route("*", "/dsh/{lane}/{tail:.*}", NativeUI(controller, client).proxy)
        async with TestClient(TestServer(app)) as browser:
            response = await browser.get("/dsh/official/?arenaSession=arena-session", allow_redirects=False)
            assert response.status == 303
            assert response.headers["Location"] == "./?arenaSession=arena-session"
            assert "Path=/dsh/official/;" in response.headers["Set-Cookie"]
            assert "HttpOnly" in response.headers["Set-Cookie"]
            response = await browser.get("/dsh/official/?arenaSession=arena-session")
            assert await response.text() == "original Dsh index"
            assert all(headers["Host"] == observed[0]["Host"] for headers in observed)


async def test_native_rpc_body_and_origin_are_forwarded_unchanged():
    async def rpc(request):
        return web.json_response(
            {
                "body": await request.json(),
                "origin": request.headers["Origin"],
                "host": request.headers["Host"],
            }
        )

    upstream = web.Application()
    upstream.router.add_post("/api/call", rpc)
    async with TestServer(upstream) as source, ClientSession() as client:
        runtime = SimpleNamespace(web_port=source.port, ui_token="token")
        controller = SimpleNamespace(lanes={"engy": SimpleNamespace(id="engy", runtime=runtime)})
        app = web.Application()
        app.router.add_route("*", "/dsh/{lane}/{tail:.*}", NativeUI(controller, client).proxy)
        async with TestClient(TestServer(app)) as browser:
            response = await browser.post(
                "/dsh/engy/api/call",
                json={"session": "arena-session"},
                headers={"Origin": "http://127.0.0.1:8765"},
            )
            result = await response.json()
            assert result["body"] == {"session": "arena-session"}
            assert result["origin"] == "http://127.0.0.1:8765"
            assert result["host"] == browser.host + ":" + str(browser.port)


async def test_native_websocket_transports_text_and_binary_and_closes_both_ends():
    negotiated = []

    async def echo(request):
        socket = web.WebSocketResponse()
        await socket.prepare(request)
        negotiated.append(socket.compress)
        async for message in socket:
            if isinstance(message.data, str):
                await socket.send_str(message.data)
            elif isinstance(message.data, bytes):
                await socket.send_bytes(message.data)
        return socket

    upstream = web.Application()
    upstream.router.add_get("/socket", echo)
    async with TestServer(upstream) as source, ClientSession() as client:
        runtime = SimpleNamespace(web_port=source.port, ui_token="token")
        controller = SimpleNamespace(lanes={"engy": SimpleNamespace(id="engy", runtime=runtime)})
        app = web.Application()
        app.router.add_route("*", "/dsh/{lane}/{tail:.*}", NativeUI(controller, client).proxy)
        async with TestClient(TestServer(app)) as browser:
            async with browser.ws_connect("/dsh/engy/socket", compress=15) as socket:
                await socket.send_str("Dsh native")
                assert (await socket.receive()).data == "Dsh native"
                await socket.send_bytes(b"\x00\xff")
                assert (await socket.receive()).data == b"\x00\xff"
                history = "Dsh compressed history " * 10000
                await socket.send_str(history)
                assert (await socket.receive()).data == history
                assert negotiated == [15]
