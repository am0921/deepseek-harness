"""Check Lean, implementation properties, the real Redis oracle, and deliberate bad implementations."""

from __future__ import annotations

import asyncio
import io
import json
import os
import platform
import re
import secrets
import subprocess
import sys
import tarfile
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

from arena import docker
from arena.config import APP, ROOT, STATE, Limits, digest
from arena.coordinator import utc_now
from prepare_lean import RELEASES


def fixture_archive(mutation: str = "") -> docker.Snapshot:
    """Build a small, explicit positive or negative control without exposing it to live agents."""
    server = (APP / "tests/fixtures/mini_server.py").read_text()
    if mutation:
        server = server.replace('mutation = os.environ.get("MUTATION", "")', f"mutation = {mutation!r}")
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as writer:
        for name, contents in (
            ("server.py", server.encode()),
            ("reference.py", (APP / "arena/reference.py").read_bytes()),
        ):
            entry = tarfile.TarInfo(name)
            entry.size = len(contents)
            entry.mode = 0o644
            writer.addfile(entry, io.BytesIO(contents))
    return docker.validate_snapshot(output.getvalue())


async def docker_checks() -> dict[str, object]:
    """Require a passing real control and rejection of six independent implementation defects."""
    limits = Limits()
    suffix = secrets.token_hex(6)
    _, raw_image, _ = await docker.command("image", "inspect", docker.VERIFIER_IMAGE, "--format", "{{.Id}}")
    image = raw_image.decode().strip()
    _, output, _ = await docker.command(
        "run",
        "--rm",
        "--network",
        "none",
        "--cpus",
        "0.5",
        "--memory",
        "256m",
        image,
        "--self-check",
        timeout=30,
    )
    oracle = json.loads(output)
    if oracle != {"status": "passed", "operations": 1600}:
        raise RuntimeError(f"Redis oracle failed: {oracle}")
    print("Redis oracle: 1600 operations passed", flush=True)
    positive = await docker.grade(f"dsh-arena-verify-{suffix}-positive", 0, limits, fixture_archive(), image)
    if not positive["allPassed"]:
        raise RuntimeError("Positive control failed:\n" + json.dumps(positive, indent=2))
    print(
        f"Grader positive control: {positive['passed']}/{positive['total']}, "
        f"{positive['durationMs']} ms, CPU {positive['cpuSeconds']} s, "
        f"peak {positive['peakMemoryBytes']} bytes",
        flush=True,
    )
    defects = [
        "binary-corruption",
        "incr-clears-ttl",
        "set-keeps-ttl",
        "accept-leading-zero",
        "no-persistence",
        "resurrect",
    ]
    rejected = {}
    for mutation in defects:
        report = await docker.grade(
            f"dsh-arena-verify-{suffix}-{mutation}", 0, limits, fixture_archive(mutation), image
        )
        failures = [item["id"] for item in report["cases"] if item["status"] == "failed"]
        if report["allPassed"] or not failures:
            raise RuntimeError(f"Grader accepted known defective implementation: {mutation}")
        rejected[mutation] = failures
        print(f"Mutation rejected: {mutation} ({len(failures)} failing cases)", flush=True)
    return {"oracle": oracle, "positiveControl": positive, "mutationsRejected": rejected}


def source_hashes() -> dict[str, str]:
    """Fingerprint the exact proof, task, runtime, UI, and tests before and after checking."""
    files = sorted(
        {
            *APP.glob("arena/*.py"),
            *APP.glob("tests/**/*.py"),
            *APP.glob("formal/*"),
            *APP.glob("runtime/*"),
            *APP.glob("static/*"),
            *APP.glob("*.sh"),
            APP / "task.txt",
            APP / "verify.py",
            APP / "prepare_lean.py",
            APP / "Dockerfile",
            APP / "pyproject.toml",
            APP / "uv.lock",
            ROOT / "packages/client/ui-workspace/src/client/arena-session.ts",
            ROOT / "packages/client/ui-workspace/src/client/index.ts",
        }
    )
    return {os.path.relpath(path, APP): digest(path) for path in files if path.is_file()}


def main() -> None:
    """Publish verification evidence only after every required check succeeds."""
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    files = source_hashes()
    target = RELEASES[(platform.system(), platform.machine())][0]
    lean = Path(os.environ.get("ARENA_LEAN", ROOT / f".cache/arena/lean-4.35.0-{target}/bin/lean"))
    if not lean.is_file():
        raise RuntimeError("Lean 4.35.0 is missing. Run apps/arena/setup.sh or set ARENA_LEAN.")
    version = subprocess.check_output([str(lean), "--version"], text=True)
    if "version 4.35.0" not in version:
        raise RuntimeError(f"Expected pinned Lean 4.35.0; got {version.strip()}")
    proof = subprocess.run(
        [str(lean), "--run", str(APP / "formal/Arena.lean")],
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if (
        re.search(r"\b(sorry|admit|axiom)\b", (APP / "formal/Arena.lean").read_text())
        or "sorryAx" in proof.stdout
    ):
        raise RuntimeError("The proof source contains an unproved declaration")
    edges = {}
    for line in proof.stdout.splitlines():
        if line.startswith("EDGE "):
            _, phase, signal, target = line.split()
            edges.setdefault(phase, {})[signal] = target
    expected = json.loads((APP / "formal/transitions.json").read_text())
    if {phase: edges.get(phase, {}) for phase in expected} != expected:
        raise RuntimeError("Python transition table differs from the proved Lean relation")
    theorems = len(re.findall(r"^(?:'Arena\.)", proof.stdout, re.MULTILINE))
    if theorems != 18:
        raise RuntimeError(f"Expected 18 checked theorems; got {theorems}")
    (STATE / "lean-check.txt").write_text(proof.stdout)
    print("Lean: 18 theorems checked; runtime transition table matches", flush=True)
    xml = STATE / "unit-tests.xml"
    subprocess.run(
        [sys.executable, "-m", "pytest", "-q", f"--junitxml={xml}"],
        cwd=APP,
        check=True,
        timeout=120,
        env={**os.environ, "ARENA_DOCKER_TESTS": "1"},
    )
    tests = sum(int(suite.attrib.get("tests", "0")) for suite in ET.parse(xml).getroot().iter("testsuite"))
    checks = asyncio.run(docker_checks())
    if source_hashes() != files:
        raise RuntimeError("Source files changed during verification; rerun against a stable checkout")
    report = {
        "passed": True,
        "checkedAt": utc_now(),
        "leanVersion": version.strip(),
        "theorems": theorems,
        "tests": tests,
        **checks,
        "files": files,
        "scope": "The Lean definitions are proved; Python/Docker conformance is separately tested. "
        "No proof covers arbitrary contestant programs, the OS, or provider inference.",
    }
    destination = STATE / "verification.json"
    descriptor, name = tempfile.mkstemp(prefix="verification-", suffix=".tmp", dir=STATE)
    temporary = Path(name)
    with os.fdopen(descriptor, "w") as output:
        output.write(json.dumps(report, ensure_ascii=False, indent=2))
    temporary.replace(destination)
    print(f"Verification passed: {destination}", flush=True)


if __name__ == "__main__":
    main()
