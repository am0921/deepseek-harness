"""Start/stop races must settle the newly admitted run and retained native views."""

import asyncio
import json
from types import SimpleNamespace

import pytest

import arena.coordinator as module
from arena.config import APP, digest, providers
from arena.coordinator import Coordinator


def coordinator(monkeypatch, tmp_path):
    monkeypatch.setattr(module, "STATE", tmp_path)
    current = Coordinator(providers(), 8766)
    for provider in current.providers.values():
        provider.key = "local-test-only"
    monkeypatch.setattr(current, "proof_status", lambda: {"ready": True})

    async def run():
        try:
            await asyncio.Event().wait()
        finally:
            current.active = False
            if current.log:
                current.log.close()
                current.log = None

    monkeypatch.setattr(current, "_run", run)
    return current


async def test_stop_immediately_after_start_reaps_even_a_task_that_has_not_started(monkeypatch, tmp_path):
    current = coordinator(monkeypatch, tmp_path)
    try:
        await current.start()
        await current.stop()
        assert current.task.done()
        assert current.active is False
    finally:
        await current.stop()
        if current.log:
            current.log.close()


async def test_stop_during_retained_view_shutdown_also_stops_the_new_run(monkeypatch, tmp_path):
    current = coordinator(monkeypatch, tmp_path)
    stop_entered = asyncio.Event()
    stop_task = None

    async def stop():
        stop_entered.set()
        await current.stop()

    async def close():
        nonlocal stop_task
        stop_task = asyncio.create_task(stop())
        await stop_entered.wait()

    runtime = SimpleNamespace(close=close, web_port=12345)
    current.lanes = {"official": SimpleNamespace(id="official", runtime=runtime)}
    try:
        await asyncio.wait_for(current.start(), 2)
        await asyncio.wait_for(stop_task, 2)
        assert current.task.done()
        assert current.active is False
        assert runtime.web_port is None
    finally:
        if stop_task and not stop_task.done():
            stop_task.cancel()
            await asyncio.gather(stop_task, return_exceptions=True)
        await current.stop()
        if current.log:
            current.log.close()


async def test_cleanup_failure_is_reported_and_remains_retryable(monkeypatch, tmp_path):
    current = coordinator(monkeypatch, tmp_path)

    async def close():
        raise RuntimeError("Docker unavailable")

    runtime = SimpleNamespace(close=close, web_port=12345)
    current.lanes = {"official": SimpleNamespace(id="official", runtime=runtime)}
    with pytest.raises(RuntimeError, match="Docker unavailable"):
        await current.stop()
    assert runtime.web_port == 12345


@pytest.mark.parametrize("loaded_hash", ["current", "previous-code"])
def test_fresh_verification_cannot_authorize_a_controller_running_older_code(
    monkeypatch, tmp_path, loaded_hash
):
    actual = digest(APP / "task.txt")
    monkeypatch.setattr(module, "STATE", tmp_path)
    monkeypatch.setattr(
        module, "source_hashes", lambda: {"task.txt": actual if loaded_hash == "current" else loaded_hash}
    )
    current = Coordinator(providers(), 8766)
    (tmp_path / "verification.json").write_text(
        json.dumps(
            {
                "files": {"task.txt": actual},
                "passed": True,
                "checkedAt": "test",
                "theorems": 18,
                "tests": 1,
            }
        )
    )
    status = current.proof_status()
    assert status["ready"] is (loaded_hash == "current")
    if loaded_hash != "current":
        assert "Restart" in status["reason"]
