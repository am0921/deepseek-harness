"""Bounded byte transport to one fixed gateway port; no general HTTP or CONNECT proxy."""

import asyncio
import contextlib
import sys


async def main(port: int, ui_port: int) -> None:
    slots = asyncio.Semaphore(16)

    async def copy(reader, writer):
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()

    async def accept(reader, writer, target_host, target_port):
        upstream_writer = None
        tasks = []
        try:
            async with slots:
                upstream_reader, upstream_writer = await asyncio.wait_for(
                    asyncio.open_connection(target_host, target_port), 10
                )
                tasks = [
                    asyncio.create_task(copy(reader, upstream_writer)),
                    asyncio.create_task(copy(upstream_reader, writer)),
                ]
                await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        except (OSError, TimeoutError):
            pass
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            for connection in (writer, upstream_writer):
                if connection is not None:
                    connection.close()
                    with contextlib.suppress(OSError):
                        await connection.wait_closed()

    async def model(reader, writer):
        await accept(reader, writer, "host.docker.internal", port)

    async def native_ui(reader, writer):
        await accept(reader, writer, "arena-agent", 3080)

    models = await asyncio.start_server(model, "0.0.0.0", port, limit=65536)
    interface = await asyncio.start_server(native_ui, "0.0.0.0", ui_port, limit=65536)
    print(f"RELAY READY {port} {ui_port}", flush=True)
    async with models, interface:
        await asyncio.gather(models.serve_forever(), interface.serve_forever())


if __name__ == "__main__":
    asyncio.run(main(int(sys.argv[1]), int(sys.argv[2])))
