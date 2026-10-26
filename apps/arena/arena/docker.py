"""Equal Docker limits, owned process cleanup, and validated workspace snapshots."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import tarfile
from dataclasses import asdict, dataclass
from pathlib import PurePosixPath

from .config import Limits

AGENT_IMAGE = "dsh-arena-agent:local"
VERIFIER_IMAGE = "dsh-arena-verifier:local"
MAX_SNAPSHOT = 64 * 1024 * 1024


async def command(
    *args: str,
    timeout: float = 30,  # noqa: ASYNC109
    data: bytes | None = None,
    check: bool = True,  # noqa: ASYNC109
) -> tuple[int, bytes, bytes]:
    """Run one Docker command and bound its CLI lifetime; return stdout and stderr separately."""
    process = await asyncio.create_subprocess_exec(
        "docker",
        *args,
        stdin=asyncio.subprocess.PIPE if data is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(data), timeout)
    except BaseException:
        if process.returncode is None:
            process.kill()
        await process.wait()
        raise
    code = process.returncode
    assert code is not None
    if check and code:
        raise RuntimeError(f"docker {args[0]} failed ({code}): {stderr.decode('utf-8', 'replace')[-3000:]}")
    return code, stdout, stderr


@dataclass(frozen=True)
class Snapshot:
    """Validated tar bytes and a canonical digest of paths, executable bits, and file contents."""

    archive: bytes
    sha256: str
    files: int
    bytes: int


def validate_snapshot(archive: bytes) -> Snapshot:
    """Reject links, devices, traversal, oversized files, and ambiguous duplicate archive paths."""
    if len(archive) > MAX_SNAPSHOT + 4 * 1024 * 1024:
        raise ValueError("Workspace archive exceeds 68 MiB")
    ledger = []
    seen: set[str] = set()
    total = 0
    with tarfile.open(fileobj=io.BytesIO(archive)) as reader:
        for entry in reader:
            path = PurePosixPath(entry.name)
            if path.is_absolute() or ".." in path.parts:
                raise ValueError("Workspace contains an unsafe archive path")
            if entry.isdir():
                continue
            if not entry.isfile() or entry.issym() or entry.islnk():
                raise ValueError("Workspace contains a link or non-regular file")
            name = str(path)
            if name in seen:
                raise ValueError("Workspace contains duplicate archive paths")
            seen.add(name)
            total += entry.size
            if total > MAX_SNAPSHOT or len(seen) > 10000:
                raise ValueError("Workspace exceeds the 64 MiB / 10000-file grading limit")
            stream = reader.extractfile(entry)
            assert stream is not None
            value = stream.read()
            if len(value) != entry.size:
                raise ValueError("Workspace archive contains a truncated file")
            ledger.append((name, entry.mode & 0o111, hashlib.sha256(value).hexdigest()))
    encoded = json.dumps(sorted(ledger), separators=(",", ":")).encode()
    return Snapshot(archive, hashlib.sha256(encoded).hexdigest(), len(seen), total)


def isolation(name: str, lane: int, limits: Limits, *, verifier: bool = False) -> list[str]:
    """Return identical per-lane limits with disjoint, equal-size guest CPU sets."""
    cpus = ",".join(str(index) for index in range(lane * limits.cpus, (lane + 1) * limits.cpus))
    memory = limits.verifier_memory_mb if verifier else limits.memory_mb
    cpu_quota = limits.verifier_cpus if verifier else str(limits.cpus)
    options = [
        "--name",
        name,
        "--read-only",
        "--cpus",
        cpu_quota,
        "--cpuset-cpus",
        cpus,
        "--memory",
        f"{memory}m",
        "--memory-swap",
        f"{memory}m",
        "--pids-limit",
        str(limits.pids),
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--tmpfs",
        "/tmp:rw,exec,nosuid,size=256m,mode=1777",
    ]
    if verifier:
        options += [
            "--network",
            "none",
            "--cap-add",
            "SETUID",
            "--cap-add",
            "SETGID",
            "--cap-add",
            "KILL",
            "--tmpfs",
            "/workspace:rw,exec,nosuid,size=128m,mode=755",
        ]
    else:
        options += [
            "--tmpfs",
            "/home/node/.dsh:rw,nosuid,size=128m,uid=1000,gid=1000,mode=700",
        ]
    return options


async def create_workspace() -> str:
    """Allocate a bounded tmpfs volume that Docker's archive API can snapshot while paused."""
    creation = asyncio.create_task(
        command(
            "volume",
            "create",
            "--driver",
            "local",
            "--label",
            "dsh-arena.managed=true",
            "--opt",
            "type=tmpfs",
            "--opt",
            "device=tmpfs",
            "--opt",
            "o=size=256m,uid=1000,gid=1000,mode=0755",
        )
    )
    try:
        _, name, _ = await asyncio.shield(creation)
    except asyncio.CancelledError:
        try:
            _, name, _ = await creation
        except Exception:
            pass
        else:
            await command("volume", "rm", name.decode().strip())
        raise
    return name.decode().strip()


async def preflight(limits: Limits) -> dict[str, object]:
    """Refuse a run unless both images and enough equal, disjoint CPU/memory allocations exist."""
    _, data, _ = await command("info", "--format", "{{json .}}")
    info = json.loads(data)
    needed_cpus = 2 * limits.cpus + 1
    needed_memory = 2 * (limits.memory_mb + limits.verifier_memory_mb) + 512
    if info["NCPU"] < needed_cpus:
        raise ValueError(f"Docker needs at least {needed_cpus} vCPUs; configured: {info['NCPU']}")
    if info["MemTotal"] < needed_memory * 1024 * 1024:
        raise ValueError(f"Docker needs at least {needed_memory} MiB for two lanes and graders")
    images = {}
    for name in (AGENT_IMAGE, VERIFIER_IMAGE):
        _, result, _ = await command("image", "inspect", name, "--format", "{{.Id}}")
        images[name] = result.decode().strip()
    return {
        "dockerVersion": info["ServerVersion"],
        "dockerCpus": info["NCPU"],
        "dockerMemoryBytes": info["MemTotal"],
        "images": images,
        "limits": asdict(limits),
        "cpuSets": [list(range(0, limits.cpus)), list(range(limits.cpus, 2 * limits.cpus))],
        "remoteInferenceResources": "Provider-controlled; not equalized by this experiment.",
    }


async def snapshot(name: str) -> Snapshot:
    """Pause the entire contestant cgroup before taking a stable workspace archive."""
    await command("pause", name)
    try:
        _, archive, _ = await command("cp", f"{name}:/workspace/.", "-", timeout=30)
        return validate_snapshot(archive)
    except BaseException:
        await command("unpause", name, check=False)
        raise


async def remove(name: str) -> None:
    """Reap an owned container, including a paused one, without touching other containers."""
    await command("unpause", name, check=False)
    code, _, error = await command("rm", "--force", name, check=False)
    if code and b"No such container" not in error:
        raise RuntimeError(f"Could not remove owned container {name}: {error.decode('utf-8', 'replace')}")


async def grade(
    name: str, lane: int, limits: Limits, workspace: Snapshot, image: str = VERIFIER_IMAGE
) -> dict[str, object]:
    """Run the protected grader with a frozen snapshot on that lane's dedicated CPU set."""
    try:
        code, stdout, stderr = await command(
            "run",
            "--rm",
            "-i",
            *isolation(name, lane, limits, verifier=True),
            image,
            "--archive-stdin",
            data=workspace.archive,
            timeout=limits.grade_timeout,
            check=False,
        )
        if len(stdout) > 2 * 1024 * 1024:
            raise RuntimeError("Grader report exceeds 2 MiB")
        try:
            report = json.loads(stdout)
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise RuntimeError(
                f"Grader produced no valid report (exit {code}): {stderr[-2000:]!r}"
            ) from error
        if code not in (0, 1) or not isinstance(report, dict):
            raise RuntimeError(f"Grader failed (exit {code}): {stderr[-2000:]!r}")
        if (code == 0) != report.get("allPassed"):
            raise RuntimeError("Grader process exit and report disagree")
        return {**report, "snapshotSha256": workspace.sha256, "verifierImageId": image}
    finally:
        await remove(name)
