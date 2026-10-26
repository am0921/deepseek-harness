"""Root-owned TCP grader. Contestant subprocesses run as an unprivileged, separate UID."""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import io
import json
import os
import random
import resource
import select
import signal
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import BinaryIO

try:
    from .reference import Error, Model, Reply, Simple, equivalent, request
except ImportError:
    from reference import Error, Model, Reply, Simple, equivalent, request

SUITE_VERSION = "mini-redis-v1"
SEED = 1017
COMMAND_TIMEOUT = 2.0


def read_reply(stream: BinaryIO) -> Reply:
    """Read one bounded scalar RESP response; malformed or short replies fail the case."""
    line = stream.readline(65537)
    if not line.endswith(b"\r\n") or len(line) > 65536:
        raise AssertionError(f"Malformed RESP response header: {line[:120]!r}")
    kind, data = line[:1], line[1:-2]
    if kind == b"+":
        return Simple(data.decode("utf-8"))
    if kind == b"-":
        return Error(data.decode("utf-8", "replace"))
    if kind == b":":
        return int(data)
    if kind != b"$":
        raise AssertionError(f"Unsupported RESP reply type: {kind!r}")
    size = int(data)
    if size == -1:
        return None
    if not 0 <= size <= 4 * 1024 * 1024:
        raise AssertionError(f"Invalid bulk length: {size}")
    value = stream.read(size)
    if len(value) != size or stream.read(2) != b"\r\n":
        raise AssertionError("Truncated bulk reply")
    return value


class Client:
    """One persistent, timeout-bounded TCP or Unix-domain client."""

    def __init__(self, port: int = 0, unix: str | None = None) -> None:
        if unix:
            self.socket = socket.socket(socket.AF_UNIX)
            self.socket.settimeout(COMMAND_TIMEOUT)
            self.socket.connect(unix)
        else:
            self.socket = socket.create_connection(("127.0.0.1", port), COMMAND_TIMEOUT)
        self.socket.settimeout(COMMAND_TIMEOUT)
        self.stream = self.socket.makefile("rb")

    def call(self, *parts: bytes) -> Reply:
        """Send one command and consume exactly one reply."""
        self.socket.sendall(request(list(parts)))
        return read_reply(self.stream)

    def close(self) -> None:
        """Release both the buffered file and its underlying socket."""
        self.stream.close()
        self.socket.close()

    def __enter__(self) -> Client:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


class Server:
    """Own one contestant process group and its persistence directory across restarts."""

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace
        self.temp = tempfile.TemporaryDirectory(prefix="arena-case-")
        self.directory = Path(self.temp.name)
        self.directory.chmod(0o777)
        self.data = self.directory / "database"
        self.process: subprocess.Popen[bytes] | None = None
        self.stderr: BinaryIO | None = None
        self.port = 0

    def start(self) -> None:
        """Wait for the announced ephemeral port, failing within five seconds."""
        self.stderr = tempfile.TemporaryFile()
        self.process = subprocess.Popen(
            [sys.executable, str(self.workspace / "server.py"), "--port", "0", "--data", str(self.data)],
            cwd=self.workspace,
            stdout=subprocess.PIPE,
            stderr=self.stderr,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            user=65534 if os.geteuid() == 0 else None,
            group=65534 if os.geteuid() == 0 else None,
            extra_groups=[] if os.geteuid() == 0 else None,
            env={
                "PATH": "/usr/local/bin:/usr/bin:/bin",
                "PYTHONUNBUFFERED": "1",
                "PYTHONDONTWRITEBYTECODE": "1",
                "HOME": str(self.directory),
            },
        )
        assert self.process.stdout is not None
        descriptor = self.process.stdout.fileno()
        deadline = time.monotonic() + 5
        output = bytearray()
        while time.monotonic() < deadline:
            ready, _, _ = select.select([descriptor], [], [], max(0, deadline - time.monotonic()))
            if not ready:
                break
            chunk = os.read(descriptor, 4096)
            if not chunk:
                break
            output.extend(chunk)
            if len(output) > 8192:
                raise AssertionError("Startup output exceeds 8 KiB")
            if b"\n" not in output:
                continue
            for line in output.splitlines():
                if line.startswith(b"LISTENING "):
                    self.port = int(line.removeprefix(b"LISTENING ").strip())
                    if not 1 <= self.port <= 65535:
                        raise AssertionError("Announced port is outside 1..65535")
                    return
        self.stderr.seek(0)
        diagnostic = self.stderr.read(2000).decode("utf-8", "replace")
        raise AssertionError(f"Server did not announce LISTENING <port>: {diagnostic}")

    def stop(self) -> None:
        """Deliver SIGTERM, wait, then reap all members of the owned process group."""
        process = self.process
        if process is None:
            return
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired as error:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
                raise AssertionError("Server did not stop within 3 seconds after SIGTERM") from error
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        if process.stdout:
            process.stdout.close()
        if self.stderr:
            self.stderr.close()
        self.process = None

    def __enter__(self) -> Server:
        try:
            self.start()
        except BaseException:
            self.stop()
            self.temp.cleanup()
            raise
        return self

    def __exit__(self, *_args: object) -> None:
        try:
            self.stop()
        finally:
            self.temp.cleanup()


def expect(actual: Reply, expected: Reply) -> None:
    """Require matching RESP types and values while allowing any error wording."""
    if not equivalent(actual, expected):
        raise AssertionError(f"Expected {expected!r}, received {actual!r}")


Case = tuple[str, Callable[[Server, Client], None]]
CASES: list[Case] = []


def case(name: str) -> Callable[[Callable[[Server, Client], None]], Callable[[Server, Client], None]]:
    """Register one independent black-box case in deterministic report order."""

    def register(function: Callable[[Server, Client], None]) -> Callable[[Server, Client], None]:
        CASES.append((name, function))
        return function

    return register


@case("ping-and-case-insensitive-commands")
def ping(_server: Server, client: Client) -> None:
    for command in (b"PING", b"ping", b"PiNg"):
        expect(client.call(command), Simple("PONG"))
    expect(client.call(b"sEt", b"key", b"v"), Simple("OK"))
    expect(client.call(b"gEt", b"key"), b"v")


@case("get-missing-is-null-bulk")
def missing(_server: Server, client: Client) -> None:
    expect(client.call(b"GET", b"missing"), None)


@case("set-overwrite-and-del")
def crud(_server: Server, client: Client) -> None:
    for value in (b"first", b"second"):
        expect(client.call(b"SET", b"key", value), Simple("OK"))
        expect(client.call(b"GET", b"key"), value)
    expect(client.call(b"DEL", b"key"), 1)
    expect(client.call(b"GET", b"key"), None)
    expect(client.call(b"DEL", b"key"), 0)


@case("binary-keys-values-and-byte-lengths")
def binary(_server: Server, client: Client) -> None:
    for key, value in (
        (b"", b""),
        (b"a\x00b\r\n\xff", bytes(range(256))),
        ("日本語".encode(), "🦆データ".encode()),
        (b"$*:\r\n", b"\xff\xfe\x80\x00"),
    ):
        expect(client.call(b"SET", key, value), Simple("OK"))
        expect(client.call(b"GET", key), value)


@case("one-megabyte-bulk-value")
def large_bulk(_server: Server, client: Client) -> None:
    value = bytes(range(256)) * 4096
    expect(client.call(b"SET", b"large", value), Simple("OK"))
    expect(client.call(b"GET", b"large"), value)


@case("fragmented-request-at-every-byte")
def fragments(_server: Server, client: Client) -> None:
    payload = request([b"SET", b"split\xff", b"value\r\n\x00"])
    for byte in payload:
        client.socket.sendall(bytes([byte]))
        time.sleep(0.002)
    expect(read_reply(client.stream), Simple("OK"))
    expect(client.call(b"GET", b"split\xff"), b"value\r\n\x00")


@case("pipeline-one-hundred-requests")
def pipeline(_server: Server, client: Client) -> None:
    commands = [[b"INCR", b"counter"] for _ in range(100)]
    client.socket.sendall(b"".join(request(command) for command in commands))
    for number in range(1, 101):
        expect(read_reply(client.stream), number)


@case("partial-next-request-does-not-block-first-reply")
def partial_pipeline(_server: Server, client: Client) -> None:
    second = request([b"GET", b"missing"])
    client.socket.sendall(request([b"PING"]) + second[:5])
    expect(read_reply(client.stream), Simple("PONG"))
    client.socket.sendall(second[5:])
    expect(read_reply(client.stream), None)


@case("errors-preserve-connection")
def errors(_server: Server, client: Client) -> None:
    commands = [
        [b"UNKNOWN"],
        [b"PING", b"extra"],
        [b"SET", b"key"],
        [b"GET"],
        [b"DEL"],
        [b"INCR", b"a", b"b"],
        [b"PEXPIRE", b"a"],
        [b"PTTL"],
    ]
    for command in commands:
        expect(client.call(*command), Error())
        expect(client.call(b"PING"), Simple("PONG"))


@case("incr-missing-negative-and-zero")
def integer_basic(_server: Server, client: Client) -> None:
    expect(client.call(b"INCR", b"new"), 1)
    for value, expected in ((b"0", 1), (b"-2", -1), (b"-1", 0)):
        expect(client.call(b"SET", b"n", value), Simple("OK"))
        expect(client.call(b"INCR", b"n"), expected)
        expect(client.call(b"GET", b"n"), str(expected).encode())


@case("incr-rejects-noncanonical-integers-without-mutation")
def integer_invalid(_server: Server, client: Client) -> None:
    for value in (b"", b"+1", b"01", b"-0", b"-01", b" 1", b"1 ", b"1.0", b"hello", b"\xff"):
        expect(client.call(b"SET", b"n", value), Simple("OK"))
        expect(client.call(b"INCR", b"n"), Error())
        expect(client.call(b"GET", b"n"), value)


@case("incr-signed-64-bit-boundaries")
def integer_boundaries(_server: Server, client: Client) -> None:
    for value in (str(2**63 - 1).encode(), str(2**63).encode(), str(-(2**63) - 1).encode()):
        expect(client.call(b"SET", b"n", value), Simple("OK"))
        expect(client.call(b"INCR", b"n"), Error())
        expect(client.call(b"GET", b"n"), value)
    expect(client.call(b"SET", b"n", str(-(2**63)).encode()), Simple("OK"))
    expect(client.call(b"INCR", b"n"), -(2**63) + 1)


@case("pttl-sentinel-values")
def ttl_sentinels(_server: Server, client: Client) -> None:
    expect(client.call(b"PTTL", b"missing"), -2)
    expect(client.call(b"SET", b"key", b"v"), Simple("OK"))
    expect(client.call(b"PTTL", b"key"), -1)
    expect(client.call(b"PEXPIRE", b"missing", b"2000"), 0)


@case("expiry-hides-values-and-restarts-incr-from-zero")
def expiry(_server: Server, client: Client) -> None:
    client.call(b"SET", b"key", b"50")
    expect(client.call(b"PEXPIRE", b"key", b"500"), 1)
    ttl = client.call(b"PTTL", b"key")
    if type(ttl) is not int or not 100 <= ttl <= 500:
        raise AssertionError(f"Expected remaining TTL within 100..500 ms; received {ttl!r}")
    time.sleep(0.8)
    expect(client.call(b"GET", b"key"), None)
    expect(client.call(b"PTTL", b"key"), -2)
    expect(client.call(b"DEL", b"key"), 0)
    expect(client.call(b"INCR", b"key"), 1)


@case("nonpositive-pexpire-deletes-immediately")
def immediate_expiry(_server: Server, client: Client) -> None:
    for duration in (b"0", b"-1"):
        client.call(b"SET", b"key", b"v")
        expect(client.call(b"PEXPIRE", b"key", duration), 1)
        expect(client.call(b"GET", b"key"), None)
        expect(client.call(b"PTTL", b"key"), -2)


@case("set-clears-existing-ttl")
def set_clears_ttl(_server: Server, client: Client) -> None:
    client.call(b"SET", b"key", b"1")
    client.call(b"PEXPIRE", b"key", b"5000")
    expect(client.call(b"SET", b"key", b"2"), Simple("OK"))
    expect(client.call(b"PTTL", b"key"), -1)


@case("incr-preserves-existing-ttl")
def incr_preserves_ttl(_server: Server, client: Client) -> None:
    client.call(b"SET", b"key", b"1")
    client.call(b"PEXPIRE", b"key", b"5000")
    expect(client.call(b"INCR", b"key"), 2)
    ttl = client.call(b"PTTL", b"key")
    if type(ttl) is not int or not 4000 <= ttl <= 5000:
        raise AssertionError(f"INCR did not preserve a 5-second TTL: {ttl!r}")


@case("eight-concurrent-clients-increment-atomically")
def concurrent_incr(server: Server, _client: Client) -> None:
    def worker(_index: int) -> list[int]:
        with Client(server.port) as client:
            values = [client.call(b"INCR", b"shared") for _ in range(40)]
            if any(type(value) is not int for value in values):
                raise AssertionError("INCR returned a non-integer reply")
            return values  # type: ignore[return-value]

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        values = [value for group in pool.map(worker, range(8)) for value in group]
    if sorted(values) != list(range(1, 321)):
        raise AssertionError("Concurrent INCR lost, duplicated, or reordered a committed counter value")
    with Client(server.port) as check:
        expect(check.call(b"GET", b"shared"), b"320")


@case("idle-and-disconnected-clients-do-not-block-others")
def idle_clients(server: Server, client: Client) -> None:
    with Client(server.port) as idle:
        idle.socket.sendall(b"*2\r\n$3\r\nGET\r\n$100\r\npartial")
        expect(client.call(b"PING"), Simple("PONG"))
    expect(client.call(b"PING"), Simple("PONG"))


@case("restart-restores-values-and-deletions")
def persistence(server: Server, client: Client) -> None:
    client.call(b"SET", b"binary\xff", bytes(range(256)))
    client.call(b"SET", b"deleted", b"x")
    client.call(b"DEL", b"deleted")
    server.stop()
    server.start()
    with Client(server.port) as restored:
        expect(restored.call(b"GET", b"binary\xff"), bytes(range(256)))
        expect(restored.call(b"GET", b"deleted"), None)


@case("restart-preserves-deadline-without-extending-ttl")
def persistence_ttl(server: Server, client: Client) -> None:
    client.call(b"SET", b"key", b"v")
    client.call(b"PEXPIRE", b"key", b"4000")
    started = time.monotonic()
    server.stop()
    time.sleep(1.0)
    server.start()
    with Client(server.port) as restored:
        ttl = restored.call(b"PTTL", b"key")
        expected = 4000 - int((time.monotonic() - started) * 1000)
        if type(ttl) is not int or not max(1, expected - 300) <= ttl <= expected + 300:
            raise AssertionError(
                f"Persisted deadline changed: expected approximately {expected} ms, got {ttl!r}"
            )


@case("restart-does-not-resurrect-expired-keys")
def persistence_expired(server: Server, client: Client) -> None:
    client.call(b"SET", b"key", b"v")
    client.call(b"PEXPIRE", b"key", b"400")
    server.stop()
    time.sleep(0.7)
    server.start()
    with Client(server.port) as restored:
        expect(restored.call(b"GET", b"key"), None)
        expect(restored.call(b"PTTL", b"key"), -2)


def generated_commands(seed: int, count: int = 200) -> list[list[bytes]]:
    """Generate repeatable non-temporal cases shared with the independent Redis oracle."""
    rng = random.Random(seed)
    keys = [b"", b"k", b"\xff\x00", "日本語".encode(), b"line\r\nbreak"]
    values = [
        b"",
        b"0",
        b"-2",
        b"01",
        b"+1",
        b"-0",
        b"NaN",
        b"\xff\x00",
        str(2**63 - 1).encode(),
        str(-(2**63)).encode(),
    ]
    commands = []
    for _ in range(count):
        operation = rng.choice([b"SET", b"GET", b"DEL", b"INCR", b"PTTL", b"PING", b"PEXPIRE"])
        key = rng.choice(keys)
        if operation == b"SET":
            commands.append([operation, key, rng.choice(values)])
        elif operation == b"PING":
            commands.append([operation])
        elif operation == b"PEXPIRE":
            commands.append([operation, key, rng.choice([b"0", b"-100"])])
        else:
            commands.append([operation, key])
    return commands


def generated_case(seed: int) -> Callable[[Server, Client], None]:
    """Evaluate one generated trace against the pure reference machine."""

    def run(_server: Server, client: Client) -> None:
        model = Model()
        for index, command in enumerate(generated_commands(seed)):
            actual = client.call(*command)
            expected = model.execute(command, 0)
            if not equivalent(actual, expected):
                raise AssertionError(
                    f"Seed {seed}, operation {index}, {command!r}: expected {expected!r}, got {actual!r}"
                )

    return run


@case("generated-traces-1600-operations")
def generated_traces(server: Server, client: Client) -> None:
    """Reuse one process for independent generated traces, resetting the finite tested key set."""
    for trace_seed in range(SEED, SEED + 8):
        for key in (b"", b"k", b"\xff\x00", "日本語".encode(), b"line\r\nbreak"):
            client.call(b"DEL", key)
        generated_case(trace_seed)(server, client)


def check_redis_oracle() -> dict[str, object]:
    """Check all generated reference traces against the installed real Redis server."""
    with tempfile.TemporaryDirectory(prefix="arena-oracle-") as directory:
        path = str(Path(directory) / "redis.sock")
        process = subprocess.Popen(
            [
                "redis-server",
                "--port",
                "0",
                "--unixsocket",
                path,
                "--save",
                "",
                "--appendonly",
                "no",
                "--loglevel",
                "warning",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            deadline = time.monotonic() + 5
            while not Path(path).exists():
                if process.poll() is not None or time.monotonic() >= deadline:
                    raise RuntimeError("Independent Redis oracle did not start")
                time.sleep(0.02)
            checked = 0
            with Client(unix=path) as client:
                for seed in range(SEED, SEED + 8):
                    client.call(b"FLUSHDB")
                    model = Model()
                    for command in generated_commands(seed):
                        expect(client.call(*command), model.execute(command, 0))
                        checked += 1
            return {"status": "passed", "operations": checked}
        finally:
            process.terminate()
            process.wait(timeout=5)


def evaluate(workspace: Path) -> dict[str, object]:
    """Run fixed cases independently and preserve every result verbatim in the report."""
    wall_start = time.monotonic()

    def run_case(item: Case) -> dict[str, object]:
        name, function = item
        started = time.monotonic()
        try:
            with Server(workspace) as server, Client(server.port) as client:
                function(server, client)
            result = {"id": name, "status": "passed"}
        except Exception as error:
            result = {"id": name, "status": "failed", "error": f"{type(error).__name__}: {error}"[:1500]}
        result["durationMs"] = round((time.monotonic() - started) * 1000, 2)
        return result

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(run_case, CASES))
    passed = sum(result["status"] == "passed" for result in results)
    return {
        "suite": SUITE_VERSION,
        "seed": SEED,
        "passed": passed,
        "total": len(results),
        "allPassed": passed == len(results),
        "cases": results,
        "durationMs": round((time.monotonic() - wall_start) * 1000, 2),
        "workers": 4,
    }


def main() -> None:
    """Emit one machine-readable report and return nonzero on failed contestant cases."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", type=Path, default=Path("/workspace"))
    parser.add_argument("--self-check", action="store_true")
    parser.add_argument("--archive-stdin", action="store_true")
    args = parser.parse_args()
    resource.setrlimit(resource.RLIMIT_FSIZE, (32 * 1024 * 1024, 32 * 1024 * 1024))
    resource.setrlimit(resource.RLIMIT_NOFILE, (256, 256))
    if args.self_check:
        report = check_redis_oracle()
        print(json.dumps(report, ensure_ascii=False))
        return
    if args.archive_stdin:
        archive = sys.stdin.buffer.read(68 * 1024 * 1024 + 1)
        if len(archive) > 68 * 1024 * 1024:
            raise ValueError("Snapshot exceeds 68 MiB")
        total = 0
        seen = set()
        with tarfile.open(fileobj=io.BytesIO(archive)) as source:
            for member in source:
                name = Path(member.name)
                if name.is_absolute() or ".." in name.parts:
                    raise ValueError("Unsafe snapshot path")
                target = args.workspace / name
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    target.chmod(0o755)
                    continue
                if not member.isfile() or str(name) in seen:
                    raise ValueError("Snapshot contains a link, duplicate, or special file")
                seen.add(str(name))
                total += member.size
                if total > 64 * 1024 * 1024 or len(seen) > 10000:
                    raise ValueError("Snapshot exceeds the grading file limit")
                target.parent.mkdir(parents=True, exist_ok=True)
                contents = source.extractfile(member)
                assert contents is not None
                target.write_bytes(contents.read())
                target.chmod(0o755 if member.mode & 0o111 else 0o644)
    report = evaluate(args.workspace.resolve())
    report["graderSha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    report["referenceSha256"] = hashlib.sha256(
        Path(__file__).with_name("reference.py").read_bytes()
    ).hexdigest()
    own_usage = resource.getrusage(resource.RUSAGE_SELF)
    child_usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    report["cpuSeconds"] = round(
        own_usage.ru_utime + own_usage.ru_stime + child_usage.ru_utime + child_usage.ru_stime, 4
    )
    peak = Path("/sys/fs/cgroup/memory.peak")
    report["peakMemoryBytes"] = int(peak.read_text()) if peak.exists() else None
    print(json.dumps(report, ensure_ascii=False, indent=2))
    sys.exit(0 if report["allPassed"] else 1)


if __name__ == "__main__":
    main()
