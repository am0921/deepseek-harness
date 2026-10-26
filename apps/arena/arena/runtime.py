"""Documented Dsh SDK JSON-RPC over an owned container's stdio; no custom agent loop."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qs, urlsplit

from . import docker
from .config import APP, Limits
from .network import Relay


class Runtime:
    """Own a single Dsh process, request correlation, and notification delivery."""

    def __init__(
        self,
        name: str,
        lane: int,
        limits: Limits,
        gateway: str,
        token: str,
        notification: Callable[[dict[str, Any]], None],
        diagnostic: Callable[[str], None],
        image: str = docker.AGENT_IMAGE,
    ) -> None:
        self.name = name
        self.lane = lane
        self.limits = limits
        self.gateway = gateway
        self.token = token
        self.notification = notification
        self.diagnostic = diagnostic
        self.image = image
        self.facts: dict[str, Any] | None = None
        self.workspace_volume: str | None = None
        self.process: asyncio.subprocess.Process | None = None
        self.pending: dict[int, asyncio.Future[Any]] = {}
        self.counter = 0
        self.readers: list[asyncio.Task[None]] = []
        self.session = "arena-session"
        self.last_reason: str | None = None
        self.web_port: int | None = None
        self.ui_token: str | None = None
        self.ui_ready = asyncio.Event()
        self.control_port = int(urlsplit(gateway).port) - 1
        self.public_url = f"http://127.0.0.1:{self.control_port}/dsh/{('official', 'engy')[lane]}/"
        self.relay = Relay(name, lane, limits, self.control_port + 1, image)

    async def prepare(self) -> None:
        """Boot the pinned profile and complete SDK initialization before the shared release."""
        await self.relay.prepare()
        self.workspace_volume = await docker.create_workspace()
        await docker.command(
            "create",
            "-i",
            *docker.isolation(self.name, self.lane, self.limits),
            "--network",
            self.relay.network,
            "--network-alias",
            "arena-agent",
            "--mount",
            f"type=bind,source={APP / 'runtime/ptc.yml'},target=/etc/arena/ptc.yml,readonly",
            "--mount",
            f"type=volume,source={self.workspace_volume},target=/workspace,volume-nocopy",
            "--env",
            f"DEEPSEEK_API_KEY={self.token}",
            "--env",
            f"DEEPSEEK_BASE_URL={self.gateway.replace('host.docker.internal', 'arena-relay')}",
            "--env",
            f"ARENA_PUBLIC_URL={self.public_url}",
            self.image,
        )
        _, inspection, _ = await docker.command("inspect", self.name)
        container = json.loads(inspection)[0]
        actual = container["HostConfig"]
        expected_cpus = ",".join(
            str(index) for index in range(self.lane * self.limits.cpus, (self.lane + 1) * self.limits.cpus)
        )
        expected_memory = self.limits.memory_mb * 1024 * 1024
        if not (
            actual["NanoCpus"] == self.limits.cpus * 1000000000
            and actual["CpusetCpus"] == expected_cpus
            and actual["Memory"] == actual["MemorySwap"] == expected_memory
            and actual["PidsLimit"] == self.limits.pids
            and actual["ReadonlyRootfs"]
            and set(container["NetworkSettings"]["Networks"]) == {self.relay.network}
        ):
            raise RuntimeError("Docker did not enforce the agreed per-lane resource settings")
        self.facts = {
            "imageId": container["Image"],
            "nanoCpus": actual["NanoCpus"],
            "cpuSet": actual["CpusetCpus"],
            "memoryBytes": actual["Memory"],
            "pids": actual["PidsLimit"],
            "readOnlyRoot": actual["ReadonlyRootfs"],
            "modelRelay": self.relay.facts,
        }
        self.process = await asyncio.create_subprocess_exec(
            "docker",
            "start",
            "--attach",
            "--interactive",
            self.name,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=8 * 1024 * 1024,
        )
        self.readers = [asyncio.create_task(self._read()), asyncio.create_task(self._stderr())]
        await self.request(
            "initialize",
            {
                "cwd": "/workspace",
                "provider": "deepseek-official",
                "model": "deepseek-flash",
                "reasoningEffort": "high",
                "maxTokens": self.limits.max_tokens,
            },
            timeout=90,
        )
        await asyncio.wait_for(self.ui_ready.wait(), 90)
        _, port, _ = await docker.command("port", self.relay.name, f"{self.relay.ui_port}/tcp")
        self.web_port = int(port.decode().strip().rsplit(":", 1)[1])

    async def _read(self) -> None:
        assert self.process and self.process.stdout
        failure: BaseException = RuntimeError("Dsh stdio closed")
        try:
            while raw := await self.process.stdout.readline():
                if raw.startswith(b"dsh web: http"):
                    url = raw.decode().strip().removeprefix("dsh web: ")
                    self.ui_token = parse_qs(urlsplit(url).query).get("token", [None])[0]
                    self.ui_ready.set()
                    continue
                message = json.loads(raw)
                if "id" in message and "method" not in message:
                    future = self.pending.pop(message["id"], None)
                    if future is None or future.done():
                        continue
                    if "error" in message:
                        future.set_exception(RuntimeError(json.dumps(message["error"], ensure_ascii=False)))
                    else:
                        future.set_result(message.get("result"))
                elif "method" in message and "id" not in message:
                    event = message.get("params", {}).get("event", {})
                    if event.get("type") == "turn/end":
                        self.last_reason = event.get("data", {}).get("reason", {}).get("kind")
                    self.notification(message)
                elif "method" in message and "id" in message:
                    await self._write(
                        {
                            "jsonrpc": "2.0",
                            "id": message["id"],
                            "error": {
                                "code": -32601,
                                "message": "No interactive host questions in this experiment",
                            },
                        }
                    )
        except BaseException as error:
            failure = error
        finally:
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(failure)
            self.pending.clear()

    async def _stderr(self) -> None:
        assert self.process and self.process.stderr
        while raw := await self.process.stderr.readline():
            self.diagnostic(raw.decode("utf-8", "replace").rstrip())

    async def _write(self, value: dict[str, Any]) -> None:
        assert self.process and self.process.stdin
        self.process.stdin.write(json.dumps(value, ensure_ascii=False).encode() + b"\n")
        await self.process.stdin.drain()

    async def request(self, method: str, params: object, *, timeout: float | None = None) -> Any:  # noqa: ASYNC109
        """Correlate one SDK response, rejecting pending calls on shutdown or malformed stdio."""
        self.counter += 1
        request_id = self.counter
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        try:
            await self._write({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
            return await asyncio.wait_for(future, timeout or self.limits.max_seconds)
        finally:
            self.pending.pop(request_id, None)

    async def turn(self, prompt: str) -> str | None:
        """Submit a prompt and wait for the SDK-owned run interval, rather than output silence."""
        self.last_reason = None
        await self.request(
            "session/prompt", {"sessionId": self.session, "contentBlocks": [{"type": "text", "text": prompt}]}
        )
        await self.request("session/wait", {"sessionId": self.session})
        return self.last_reason

    async def close(self) -> None:
        """Stop the container first, then settle and reap its transport tasks."""
        await docker.remove(self.name)
        if self.workspace_volume:
            await docker.command("volume", "rm", self.workspace_volume)
            self.workspace_volume = None
        if self.process:
            try:
                await asyncio.wait_for(self.process.wait(), 5)
            except TimeoutError:
                self.process.kill()
                await self.process.wait()
        for reader in self.readers:
            reader.cancel()
        await asyncio.gather(*self.readers, return_exceptions=True)
        await self.relay.close()
