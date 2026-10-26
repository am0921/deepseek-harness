"""Paired experiment ownership, stable snapshots, and exact grader feedback."""

from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
import time
from collections import deque
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import docker
from .config import APP, LANES, STATE, Limits, Provider, canonical_body, digest, source_hashes
from .metrics import Metrics
from .runtime import Runtime
from .state import TERMINAL, LaneState


def utc_now() -> str:
    """Return an ISO timestamp for exported run records."""
    return datetime.now(UTC).isoformat()


def error_text(error: BaseException) -> str:
    """Expose preparation failures inside TaskGroups without dropping their actionable causes."""
    if isinstance(error, BaseExceptionGroup):
        return "; ".join(error_text(cause) for cause in error.exceptions)
    return str(error)


class PairGate:
    """Admit neither first inference request until both exact prompts are available."""

    def __init__(self) -> None:
        self.bodies: dict[str, bytes] = {}
        self.event = asyncio.Event()
        self.error: str | None = None

    async def arrive(self, lane: str, body: dict[str, object]) -> str:
        """Compare canonical payload bytes and release both requests in one event-loop turn."""
        if lane in self.bodies:
            raise RuntimeError("Duplicate first-request admission")
        self.bodies[lane] = canonical_body(body)
        if len(self.bodies) == 2:
            if self.bodies[LANES[0]] != self.bodies[LANES[1]]:
                self.error = "First model-visible requests differ; neither was sent to a provider."
            self.event.set()
        try:
            await asyncio.wait_for(self.event.wait(), 90)
        except TimeoutError:
            self.error = "The other lane did not reach the first-request barrier within 90 seconds."
            self.event.set()
        if self.error:
            raise RuntimeError(self.error)
        return hashlib.sha256(self.bodies[lane]).hexdigest()

    def abort(self, reason: str) -> None:
        """Release a waiting peer with an explicit error after preparation failure or cancellation."""
        if not self.event.is_set():
            self.error = reason
            self.event.set()


@dataclass
class Lane:
    """All mutable state for one owned lane; no workspace is shared with its peer."""

    id: str
    provider: Provider
    token: str = field(default_factory=lambda: secrets.token_urlsafe(32), repr=False)
    state: LaneState = field(default_factory=LaneState)
    metrics: Metrics = field(default_factory=Metrics)
    runtime: Runtime | None = None
    grade: dict[str, object] | None = None
    grades: list[dict[str, object]] = field(default_factory=list)
    first_hash: str | None = None
    first_sent: float | None = None
    finished_at: float | None = None
    message: str = ""

    def public(self, started: float | None) -> dict[str, object]:
        """Return display state without either real or synthetic API credentials."""
        return {
            "id": self.id,
            "provider": self.provider.public(),
            "phase": self.state.phase,
            "round": self.state.round,
            "metrics": self.metrics.public(),
            "grade": self.grade,
            "gradeCount": len(self.grades),
            "firstRequestSha256": self.first_hash,
            "message": self.message,
            "elapsedSeconds": None
            if started is None
            else (self.finished_at or time.perf_counter()) - started,
            "uiReady": self.runtime is not None and self.runtime.web_port is not None,
            "uiUrl": f"/dsh/{self.id}/?arenaSession=arena-session",
        }


class Coordinator:
    """Own run admission, both runtimes, grading, feedback, and bounded UI subscribers."""

    def __init__(self, providers: dict[str, Provider], gateway_port: int) -> None:
        self.providers = providers
        self.gateway_port = gateway_port
        self.limits = Limits()
        self.run_id: str | None = None
        self.active = False
        self.started: float | None = None
        self.started_at: str | None = None
        self.lanes: dict[str, Lane] = {}
        self.gate = PairGate()
        self.fairness: dict[str, object] | None = None
        self.error: str | None = None
        self.task: asyncio.Task[None] | None = None
        self.history: deque[dict[str, Any]] = deque(maxlen=4000)
        self.subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self.sequence = 0
        self.directory: Path | None = None
        self.log = None
        self.control_lock = asyncio.Lock()
        self.running_files = source_hashes()

    def emit(self, kind: str, lane: str | None = None, **data: Any) -> None:
        """Append durable events and disconnect slow subscribers rather than delaying the experiment."""
        self.sequence += 1
        value = {"seq": self.sequence, "at": utc_now(), "kind": kind, "lane": lane, **data}
        encoded = json.dumps(value, ensure_ascii=False)
        for provider in self.providers.values():
            if provider.key:
                encoded = encoded.replace(provider.key, "[redacted]")
        value = json.loads(encoded)
        if self.log:
            self.log.write(encoded + "\n")
            self.log.flush()
        self.history.append(value)
        for queue in tuple(self.subscribers):
            try:
                queue.put_nowait(value)
            except asyncio.QueueFull:
                self.subscribers.remove(queue)
                while not queue.empty():
                    queue.get_nowait()
                queue.put_nowait({"kind": "reconnect"})

    def proof_status(self) -> dict[str, object]:
        """Require fresh machine-check and conformance evidence for all recorded source files."""
        path = STATE / "verification.json"
        if not path.exists():
            return {"ready": False, "reason": "Run ./apps/arena/verify.sh to verify this checkout."}
        try:
            report = json.loads(path.read_text())
            if report["files"] != self.running_files:
                return {
                    "ready": False,
                    "reason": "Verified source differs from this running controller. Restart run.sh.",
                }
            stale = [
                name
                for name, expected in report["files"].items()
                if not (APP / name).is_file() or digest(APP / name) != expected
            ]
            if stale:
                return {"ready": False, "reason": "Verification is stale: " + ", ".join(stale)}
            return {
                "ready": report["passed"],
                "checkedAt": report["checkedAt"],
                "theorems": report["theorems"],
                "tests": report["tests"],
                "scope": "Lean model proofs plus separately tested implementation conformance.",
            }
        except (ValueError, KeyError, OSError) as error:
            return {"ready": False, "reason": f"Cannot read verification evidence: {error}"}

    def public(self) -> dict[str, object]:
        """Expose the current snapshot used by both fresh and reconnecting clients."""
        sent = [lane.first_sent for lane in self.lanes.values() if lane.first_sent is not None]
        return {
            "runId": self.run_id,
            "active": self.active,
            "startedAt": self.started_at,
            "providers": {name: item.public() for name, item in self.providers.items()},
            "limits": asdict(self.limits),
            "lanes": {name: item.public(self.started) for name, item in self.lanes.items()},
            "fairness": self.fairness,
            "error": self.error,
            "verification": self.proof_status(),
            "dispatchSkewMs": (max(sent) - min(sent)) * 1000 if len(sent) == 2 else None,
            "firstRequestsEqual": len(self.gate.bodies) == 2 and self.gate.error is None,
            "events": list(self.history),
            "task": (APP / "task.txt").read_text(),
        }

    async def start(self) -> str:
        """Admit one run atomically; expensive setup happens in its owned background task."""
        async with self.control_lock:
            return await self._start()

    async def _start(self) -> str:
        """Replace the previous run only while the start/stop control lock is held."""
        if self.active:
            raise ValueError("An experiment is already running")
        if not all(provider.key for provider in self.providers.values()):
            raise ValueError("Configure both API keys before starting")
        status = self.proof_status()
        if not status["ready"]:
            raise ValueError(str(status.get("reason", "Verification did not pass")))
        self.active = True
        try:
            await self.close_runtimes()
        except BaseException:
            self.active = False
            raise
        self.error = None
        self.history.clear()
        self.sequence = 0
        self.gate = PairGate()
        self.fairness = None
        self.started = None
        self.started_at = utc_now()
        self.run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + "-" + secrets.token_hex(3)
        self.directory = STATE / "runs" / self.run_id
        self.directory.mkdir(parents=True, mode=0o700)
        self.log = (self.directory / "events.jsonl").open("a", encoding="utf-8")
        self.lanes = {name: Lane(name, replace(provider)) for name, provider in self.providers.items()}
        self.task = asyncio.create_task(self._run())
        return self.run_id

    def transition(self, lane: Lane, signal: str, message: str = "") -> None:
        """Apply one checked transition and record its externally visible meaning."""
        phase = lane.state.advance(signal)
        lane.message = message
        if phase in TERMINAL:
            lane.finished_at = time.perf_counter()
        self.emit("phase", lane.id, phase=phase, round=lane.state.round, message=message)

    def notification(self, lane: Lane, value: dict[str, Any]) -> None:
        """Persist full SDK facts and project bounded tool output into the live panes."""
        self.emit("sdk", lane.id, notification=value)
        event = value.get("params", {}).get("event", {})
        kind = event.get("type", "")
        if kind.startswith("tool/") or kind == "turn/end":
            text = json.dumps(event.get("data", {}), ensure_ascii=False)
            self.emit(
                "output",
                lane.id,
                channel="result" if kind.endswith("result") else "event",
                text=f"{kind}\n{text[:16000]}" + ("\n[Full event in run log]" if len(text) > 16000 else ""),
            )

    async def _run(self) -> None:
        assert self.run_id and self.directory
        try:
            self.fairness = await docker.preflight(self.limits)
            self.fairness["taskSha256"] = digest(APP / "task.txt")
            self.fairness["ptcSha256"] = digest(APP / "runtime/ptc.yml")
            self.fairness["verification"] = json.loads((STATE / "verification.json").read_text())
            (self.directory / "manifest.json").write_text(json.dumps(self.fairness, indent=2))
            for index, name in enumerate(LANES):
                lane = self.lanes[name]
                endpoint = f"http://host.docker.internal:{self.gateway_port}/gateway/{self.run_id}/{name}"
                lane.runtime = Runtime(
                    f"dsh-arena-{self.run_id}-{name}",
                    index,
                    self.limits,
                    endpoint,
                    lane.token,
                    lambda value, lane=lane: self.notification(lane, value),
                    lambda text, lane=lane: self.emit("output", lane.id, channel="diagnostic", text=text),
                    image=self.fairness["images"][docker.AGENT_IMAGE],
                )

            async def prepare(lane: Lane) -> None:
                assert lane.runtime
                await lane.runtime.prepare()
                self.emit("container-settings", lane.id, settings=lane.runtime.facts)
                self.transition(lane, "prepared", "Dsh PTC ready; waiting for peer")

            async with asyncio.TaskGroup() as preparations:
                for lane in self.lanes.values():
                    preparations.create_task(prepare(lane))
            self.fairness["runtimeSettings"] = {lane.id: lane.runtime.facts for lane in self.lanes.values()}
            (self.directory / "manifest.json").write_text(json.dumps(self.fairness, indent=2))
            self.started = time.perf_counter()
            self.emit("released", message="Both Dsh runtimes prepared; releasing identical task prompts")
            for lane in self.lanes.values():
                self.transition(lane, "start")
            await asyncio.wait_for(
                asyncio.gather(*(self._lane(index, self.lanes[name]) for index, name in enumerate(LANES))),
                self.limits.max_seconds,
            )
        except asyncio.CancelledError:
            self.gate.abort("Experiment stopped")
            for lane in self.lanes.values():
                if lane.state.phase not in TERMINAL:
                    self.transition(lane, "stop", "Stopped by user")
        except Exception as error:
            self.error = error_text(error)
            self.gate.abort(self.error)
            self.emit("error", message=self.error)
            for lane in self.lanes.values():
                if lane.state.phase not in TERMINAL:
                    self.transition(lane, "error", self.error)
        finally:
            if self.started is None or any(lane.state.phase == "stopped" for lane in self.lanes.values()):
                await self.close_runtimes()
            self.active = False
            self.emit("finished")
            (self.directory / "summary.json").write_text(
                json.dumps(self.public(), ensure_ascii=False, indent=2)
            )
            if self.log:
                self.log.close()
                self.log = None

    async def _lane(self, index: int, lane: Lane) -> None:
        assert lane.runtime and self.directory
        prompt = (APP / "task.txt").read_text()
        passed_snapshot: str | None = None
        try:
            for iteration in range(self.limits.max_rounds + 1):
                lane.state.round = iteration + 1
                reason = await lane.runtime.turn(prompt)
                if reason not in {"completed", "max-tokens"}:
                    raise RuntimeError(
                        f"Dsh turn did not complete: {reason}. No contestant score was assigned."
                    )
                self.transition(lane, "idle", f"Dsh run ended: {reason}; taking a frozen snapshot")
                workspace = await docker.snapshot(lane.runtime.name)
                (self.directory / f"{lane.id}-round-{iteration + 1}.tar").write_bytes(workspace.archive)
                if passed_snapshot == workspace.sha256:
                    await docker.command("unpause", lane.runtime.name)
                    self.transition(
                        lane, "pass", "Agent received the passing report; graded files are unchanged"
                    )
                    return
                grader_name = f"dsh-arena-{self.run_id}-{lane.id}-grader"
                report = await docker.grade(
                    grader_name,
                    index,
                    self.limits,
                    workspace,
                    image=self.fairness["images"][docker.VERIFIER_IMAGE],
                )
                if report.get("graderSha256") != digest(APP / "arena/grader.py"):
                    raise RuntimeError("Verifier image grader differs from the verified checkout; rebuild it")
                if report.get("referenceSha256") != digest(APP / "arena/reference.py"):
                    raise RuntimeError(
                        "Verifier image reference model differs from the verified checkout; rebuild it"
                    )
                lane.grade = report
                lane.grades.append(report)
                raw = json.dumps(report, ensure_ascii=False, indent=2)
                report_file = self.directory / f"{lane.id}-grade-{len(lane.grades)}.json"
                report_file.write_text(raw)
                self.emit("grade", lane.id, report=report)
                await docker.command("unpause", lane.runtime.name)
                self.transition(
                    lane, "feedback", "Returning the exact external report to the same Dsh session"
                )
                all_passed = report["allPassed"] is True
                prompt = (
                    "外部採点器の結果です。以下のJSONは採点結果そのものです。\n\n"
                    + raw
                    + (
                        "\n\n全件合格です。ファイルを変更せず、結果を短く確認して終了してください。"
                        if all_passed
                        else "\n\n失敗した項目の原因を調べて実装を修正し、テストして作業を完了してください。"
                    )
                )
                self.emit(
                    "feedback", lane.id, reportSha256=hashlib.sha256(raw.encode()).hexdigest(), text=prompt
                )
                passed_snapshot = workspace.sha256 if all_passed else None
                if iteration >= self.limits.max_rounds - 1 and not all_passed:
                    self.transition(lane, "limit", "Repair-round limit reached; final report is saved")
                    await lane.runtime.request(
                        "session/prompt",
                        {
                            "sessionId": lane.runtime.session,
                            "contentBlocks": [{"type": "text", "text": prompt}],
                        },
                        timeout=5,
                    )
                    return
                self.transition(lane, "resume")
            self.transition(lane, "limit", "Confirmation-round limit reached")
        except asyncio.CancelledError:
            raise
        except Exception as error:
            if lane.state.phase not in TERMINAL:
                self.transition(lane, "error", str(error))
            self.emit("error", lane.id, message=str(error))
            self.gate.abort(str(error))

    async def stop(self) -> None:
        """Cancel the owned run and wait until all of its containers have been reaped."""
        async with self.control_lock:
            if self.task and not self.task.done():
                self.task.cancel()
                try:
                    await self.task
                except asyncio.CancelledError:
                    pass
            await self.close_runtimes()
            self.active = False
            if self.log:
                self.log.close()
                self.log = None

    async def close_runtimes(self) -> None:
        """Release retained native Web views when the user stops, starts again, or exits."""
        retained = [lane for lane in self.lanes.values() if lane.runtime]
        results = await asyncio.gather(*(lane.runtime.close() for lane in retained), return_exceptions=True)
        failures = []
        for lane, result in zip(retained, results, strict=True):
            if isinstance(result, BaseException):
                failures.append(f"{lane.id}: {result}")
            else:
                lane.runtime.web_port = None
        if failures:
            raise RuntimeError("Could not release owned Dsh runtime: " + "; ".join(failures))
