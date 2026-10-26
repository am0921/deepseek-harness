"""Positive-control implementation, used only by verifier tests and the visibly labeled demo."""

import argparse
import asyncio
import base64
import json
import os
import signal
import time
from pathlib import Path

from reference import Error, Model, encode


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--data", type=Path, required=True)
    args = parser.parse_args()
    model = Model()
    mutation = os.environ.get("MUTATION", "")
    if args.data.exists() and mutation != "no-persistence":
        saved = json.loads(args.data.read_text())
        model.values = {base64.b64decode(key): base64.b64decode(value) for key, value in saved["values"]}
        model.deadlines = {base64.b64decode(key): value for key, value in saved["deadlines"]}
        if mutation == "resurrect":
            model.deadlines.clear()

    async def handle(reader, writer):
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                if not line.startswith(b"*"):
                    break
                parts = []
                for _ in range(int(line[1:-2])):
                    header = await reader.readline()
                    length = int(header[1:-2])
                    value = await reader.readexactly(length + 2)
                    parts.append(value[:-2])
                command = parts[0].upper() if parts else b""
                key = parts[1] if len(parts) > 1 else b""
                old_deadline = model.deadlines.get(key)
                if mutation == "case-sensitive" and parts and parts[0] != command:
                    result = Error()
                elif mutation == "accept-leading-zero" and command == b"INCR":
                    try:
                        number = int(model.values.get(key, b"0")) + 1
                        model.values[key] = str(number).encode()
                        result = number
                    except ValueError:
                        result = Error()
                else:
                    result = model.execute(parts, int(time.time() * 1000))
                if mutation == "incr-clears-ttl" and command == b"INCR":
                    model.deadlines.pop(key, None)
                if mutation == "set-keeps-ttl" and command == b"SET" and old_deadline is not None:
                    model.deadlines[key] = old_deadline
                if mutation == "binary-corruption" and command == b"GET" and isinstance(result, bytes):
                    result = result.decode("utf-8", "ignore").encode()
                writer.write(encode(result))
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError, ValueError):
            pass
        finally:
            writer.close()
            await writer.wait_closed()

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGTERM, stop.set)
    loop.add_signal_handler(signal.SIGINT, stop.set)
    server = await asyncio.start_server(handle, "127.0.0.1", args.port, limit=4 * 1024 * 1024)
    print(f"LISTENING {server.sockets[0].getsockname()[1]}", flush=True)
    async with server:
        await stop.wait()
    if mutation != "no-persistence":

        def pairs(values):
            return [[base64.b64encode(key).decode(), value] for key, value in values.items()]

        args.data.write_text(
            json.dumps(
                {
                    "values": pairs(
                        {key: base64.b64encode(value).decode() for key, value in model.values.items()}
                    ),
                    "deadlines": pairs(model.deadlines),
                }
            )
        )


if __name__ == "__main__":
    asyncio.run(main())
