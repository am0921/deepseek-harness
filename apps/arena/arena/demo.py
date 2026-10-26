"""Keyless, explicitly labeled Messages fixture that exercises real Dsh and grading containers."""

from __future__ import annotations

import asyncio
import json
from collections import defaultdict

from aiohttp import web

from .config import APP


def solution_command(broken: bool) -> str:
    """Return the fixture's file-writing command; this never runs in a paid experiment."""
    reference = (APP / "arena/reference.py").read_text()
    server = (APP / "tests/fixtures/mini_server.py").read_text()
    if broken:
        server = server.replace('mutation = os.environ.get("MUTATION", "")', "mutation = 'incr-clears-ttl'")
    program = (
        "from pathlib import Path\n"
        + f"Path('reference.py').write_text({reference!r})\n"
        + f"Path('server.py').write_text({server!r})\n"
        + "Path('README.md').write_text('Demo fixture. Run python3 server.py --port 0 --data /tmp/data.')\n"
    )
    return "python3 - <<'ARENA_DEMO'\n" + program + "ARENA_DEMO\n"


class Demo:
    """A deterministic local provider, separate from all real provider credentials."""

    def __init__(self) -> None:
        self.calls: dict[str, int] = defaultdict(int)

    async def models(self, _request: web.Request) -> web.Response:
        return web.json_response({"object": "list", "data": [{"id": "deepseek-flash", "object": "model"}]})

    async def messages(self, request: web.Request) -> web.StreamResponse:
        body = await request.json()
        key = request.headers.get("x-api-key", "demo")
        call = self.calls[key]
        self.calls[key] += 1
        tool = call in (0, 2)
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)

        async def event(value):
            raw = f"event: {value['type']}\ndata: {json.dumps(value, ensure_ascii=False)}\n\n".encode()
            await response.write(raw)
            await asyncio.sleep(0.004)

        await event(
            {
                "type": "message_start",
                "message": {
                    "id": f"demo-{call}",
                    "type": "message",
                    "role": "assistant",
                    "model": body["model"],
                    "content": [],
                    "stop_reason": None,
                    "usage": {
                        "input_tokens": 100,
                        "cache_read_input_tokens": 300 if call else 0,
                        "output_tokens": 0,
                    },
                },
            }
        )
        if tool:
            arguments = {
                "code": "console.log(await tools.bash("
                + json.dumps(
                    {"command": solution_command(broken=call == 0), "description": "Write demo fixture"}
                )
                + "));",
                "description": "Write the demo implementation" if call == 0 else "Repair TTL preservation",
            }
            await event(
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {
                        "type": "tool_use",
                        "id": f"demo-tool-{call}",
                        "name": "run_code",
                        "input": {},
                    },
                }
            )
            encoded = json.dumps(arguments)
            for offset in range(0, len(encoded), 500):
                await event(
                    {
                        "type": "content_block_delta",
                        "index": 0,
                        "delta": {"type": "input_json_delta", "partial_json": encoded[offset : offset + 500]},
                    }
                )
        else:
            text = (
                "初期実装を完了しました。外部採点を待ちます。"
                if call == 1
                else "TTLを保持するよう修正しました。再採点してください。"
                if call == 3
                else "全件合格を確認しました。ファイルは変更していません。"
            )
            await event(
                {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}
            )
            for offset in range(0, len(text), 4):
                await event(
                    {
                        "type": "content_block_delta",
                        "index": 0,
                        "delta": {"type": "text_delta", "text": text[offset : offset + 4]},
                    }
                )
        await event({"type": "content_block_stop", "index": 0})
        await event(
            {
                "type": "message_delta",
                "delta": {"stop_reason": "tool_use" if tool else "end_turn", "stop_sequence": None},
                "usage": {"output_tokens": 1000 if tool else 30},
            }
        )
        await event({"type": "message_stop"})
        await response.write_eof()
        return response
