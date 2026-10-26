"""Real archive regression: ordinary container tmpfs is not visible to Docker cp."""

import io
import os
import secrets
import tarfile

import pytest

from arena import docker
from arena.config import Limits


@pytest.mark.skipif(
    os.environ.get("ARENA_DOCKER_TESTS") != "1", reason="Enable the real Docker snapshot check explicitly"
)
async def test_paused_tmpfs_volume_snapshot_contains_the_actual_written_file():
    name = "dsh-arena-snapshot-test-" + secrets.token_hex(6)
    volume = await docker.create_workspace()
    try:
        await docker.command(
            "run",
            "--detach",
            *docker.isolation(name, 0, Limits()),
            "--network",
            "none",
            "--mount",
            f"type=volume,source={volume},target=/workspace,volume-nocopy",
            "--entrypoint",
            "sleep",
            docker.AGENT_IMAGE,
            "120",
        )
        await docker.command(
            "exec",
            name,
            "python3",
            "-c",
            "from pathlib import Path; Path('/workspace/server.py').write_bytes(b'actual bytes\\x00')",
        )
        result = await docker.snapshot(name)
        with tarfile.open(fileobj=io.BytesIO(result.archive)) as archive:
            entries = [entry for entry in archive if entry.isfile()]
            assert len(entries) == 1
            assert entries[0].name.removeprefix("./") == "server.py"
            assert archive.extractfile(entries[0]).read() == b"actual bytes\x00"
        assert result.files == 1
    finally:
        await docker.remove(name)
        await docker.command("volume", "rm", volume)
