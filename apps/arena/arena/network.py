"""Per-lane internal networks with a fixed-destination model transport."""

from __future__ import annotations

import asyncio
import json

from . import docker
from .config import APP, Limits


class Relay:
    """Expose only the authenticated inference gateway to an otherwise offline agent."""

    def __init__(self, name: str, lane: int, limits: Limits, port: int, image: str) -> None:
        self.name = name + "-relay"
        self.network = name + "-network"
        self.lane = lane
        self.limits = limits
        self.port = port
        self.ui_port = 3080 if port != 3080 else 3081
        self.image = image
        self.network_created = False
        self.container_created = False
        self.process: asyncio.subprocess.Process | None = None
        self.facts: dict[str, object] | None = None

    async def _allocate(self, attribute: str, *arguments: str) -> None:
        """Set ownership after Docker acknowledges creation, including during cancellation."""
        creation = asyncio.create_task(docker.command(*arguments))
        try:
            await asyncio.shield(creation)
        except asyncio.CancelledError:
            try:
                await creation
            except Exception:
                pass
            else:
                setattr(self, attribute, True)
            raise
        setattr(self, attribute, True)

    async def prepare(self) -> None:
        """Start a bounded TCP relay, then wait for its observable listening acknowledgement."""
        await self._allocate("network_created", "network", "create", "--internal", self.network)
        cpus = ",".join(
            str(index) for index in range(self.lane * self.limits.cpus, (self.lane + 1) * self.limits.cpus)
        )
        await self._allocate(
            "container_created",
            "create",
            "--name",
            self.name,
            "--network",
            "bridge",
            "--publish",
            f"127.0.0.1::{self.ui_port}",
            "--read-only",
            "--cpus",
            "0.25",
            "--cpuset-cpus",
            cpus,
            "--memory",
            "64m",
            "--memory-swap",
            "64m",
            "--pids-limit",
            "32",
            "--ulimit",
            "nofile=128:128",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--sysctl",
            "net.ipv4.ip_forward=0",
            "--user",
            "1000:1000",
            "--entrypoint",
            "python3",
            "--mount",
            f"type=bind,source={APP / 'runtime/relay.py'},target=/opt/arena-relay.py,readonly",
            self.image,
            "-u",
            "/opt/arena-relay.py",
            str(self.port),
            str(self.ui_port),
        )
        await docker.command("network", "connect", "--alias", "arena-relay", self.network, self.name)
        self.process = await asyncio.create_subprocess_exec(
            "docker",
            "start",
            "--attach",
            self.name,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        assert self.process.stdout
        ready = await asyncio.wait_for(self.process.stdout.readline(), 15)
        if ready != f"RELAY READY {self.port} {self.ui_port}\n".encode():
            raise RuntimeError("The isolated model relay did not become ready")
        _, raw, _ = await docker.command("inspect", self.name)
        container = json.loads(raw)[0]
        settings = container["HostConfig"]
        _, raw_network, _ = await docker.command("network", "inspect", self.network)
        if not (
            json.loads(raw_network)[0]["Internal"]
            and settings["NanoCpus"] == 250000000
            and settings["Memory"] == settings["MemorySwap"] == 64 * 1024 * 1024
            and settings["CpusetCpus"] == cpus
            and settings["Sysctls"]["net.ipv4.ip_forward"] == "0"
        ):
            raise RuntimeError("Docker did not enforce model relay isolation")
        self.facts = {
            "internalNetwork": self.network,
            "imageId": container["Image"],
            "nanoCpus": settings["NanoCpus"],
            "memoryBytes": settings["Memory"],
            "cpuSet": cpus,
            "destinationPort": self.port,
            "webDestination": "arena-agent:3080",
            "ipForwarding": False,
        }

    async def close(self) -> None:
        """Reap the relay and remove only its owned network after the agent is gone."""
        if self.container_created:
            await docker.remove(self.name)
            self.container_created = False
        if self.process:
            try:
                await asyncio.wait_for(self.process.wait(), 5)
            except TimeoutError:
                self.process.kill()
                await self.process.wait()
        if self.network_created:
            await docker.command("network", "rm", self.network)
            self.network_created = False
