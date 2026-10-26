"""Accounting, framing, paired admission, and isolation properties at process boundaries."""

import asyncio
import io
import json
import tarfile
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from arena.config import Limits, Rates, canonical_body
from arena.coordinator import PairGate
from arena.docker import isolation, validate_snapshot
from arena.grader import read_reply
from arena.metrics import SSE, Metrics, RequestMetrics, token_count
from arena.reference import Error, Model, Simple, encode, equivalent, request
from arena.state import TERMINAL, TRANSITIONS, LaneState


@given(st.binary(max_size=65536))
def test_bulk_reply_roundtrips_arbitrary_bytes(value):
    assert read_reply(io.BytesIO(encode(value))) == value


@given(st.lists(st.binary(max_size=300), min_size=1, max_size=15))
def test_command_lengths_are_utf8_independent(parts):
    stream = io.BytesIO(request(parts))
    assert stream.readline() == f"*{len(parts)}\r\n".encode()
    assert [read_reply(stream) for _ in parts] == parts
    assert stream.read() == b""


@given(st.binary(max_size=300), st.binary(max_size=1000))
def test_reference_preserves_binary_keys_and_values(key, value):
    model = Model()
    assert model.execute([b"SET", key, value], 0) == Simple("OK")
    assert model.execute([b"GET", key], 1) == value


@given(st.integers(min_value=0, max_value=10**9), st.integers(min_value=1, max_value=10**6))
def test_expiry_boundary_and_absence_are_consistent(now, duration):
    model = Model()
    model.execute([b"SET", b"k", b"5"], now)
    model.execute([b"PEXPIRE", b"k", str(duration).encode()], now)
    assert model.execute([b"GET", b"k"], now + duration - 1) == b"5"
    assert model.execute([b"GET", b"k"], now + duration) is None
    assert model.execute([b"PTTL", b"k"], now + duration) == -2
    assert model.execute([b"INCR", b"k"], now + duration) == 1


@pytest.mark.parametrize("value", [b"01", b"+1", b"-0", b"-01", b"", b"1.2", b"\xff", b"9223372036854775807"])
def test_rejected_integer_mutation_preserves_value_and_deadline(value):
    model = Model()
    model.execute([b"SET", b"k", value], 0)
    model.execute([b"PEXPIRE", b"k", b"1000"], 0)
    assert isinstance(model.execute([b"INCR", b"k"], 100), Error)
    assert model.execute([b"GET", b"k"], 100) == value
    assert model.execute([b"PTTL", b"k"], 100) == 900


def test_set_clears_and_incr_preserves_expiration():
    model = Model()
    model.execute([b"SET", b"k", b"1"], 0)
    model.execute([b"PEXPIRE", b"k", b"1000"], 0)
    assert model.execute([b"INCR", b"k"], 50) == 2
    assert model.execute([b"PTTL", b"k"], 50) == 950
    model.execute([b"SET", b"k", b"2"], 50)
    assert model.execute([b"PTTL", b"k"], 50) == -1


def test_sse_decodes_every_utf8_byte_and_multiline_data():
    payload = {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "日本語🦆"}}
    text = (
        "event: content_block_delta\r\n"
        + "".join(
            "data: " + line + "\r\n"
            for line in json.dumps(payload, ensure_ascii=False, indent=2).splitlines()
        )
        + "\r\n: heartbeat\n\ndata: [DONE]\n\n"
    )
    parser = SSE()
    events = []
    for byte in text.encode():
        events.extend(parser.feed(bytes([byte])))
    events.extend(parser.feed(b"", final=True))
    assert events == [payload]


def test_sse_preserves_final_event_without_blank_line():
    parser = SSE()
    assert parser.feed(b'data: {"type":"message_stop"}') == []
    assert parser.feed(b"", final=True) == [{"type": "message_stop"}]


def test_cumulative_usage_updates_never_double_count_cache_or_output():
    metrics = RequestMetrics(10, Rates("0.30", "1.20", "0.006"))
    metrics.observe(
        {
            "type": "message_start",
            "message": {"usage": {"input_tokens": 25, "cache_read_input_tokens": 75, "output_tokens": 0}},
        },
        11,
    )
    metrics.observe({"type": "content_block_delta", "delta": {"text": "one"}}, 12)
    metrics.observe({"type": "message_delta", "usage": {"output_tokens": 10}}, 13)
    metrics.observe({"type": "message_delta", "usage": {"output_tokens": 20}}, 14)
    metrics.observe({"type": "content_block_delta", "delta": {"text": "two"}}, 15)
    metrics.observe({"type": "message_stop"}, 16)
    metrics.ended = 16
    totals = Metrics([metrics]).public()
    assert totals["input"] == 100
    assert totals["output"] == 20
    assert totals["cached"] == 75
    assert totals["cachePercent"] == 75
    assert Decimal(totals["cost"]) == Decimal("0.00003195")
    assert totals["tokensPerSecond"] == 19 / 3
    assert totals["ttftMs"] == 2000
    assert totals["incompleteRequests"] == 0


@given(st.integers(0, 10**8), st.integers(0, 10**8), st.integers(0, 10**8))
def test_price_partition_charges_each_input_token_once(uncached, cached, output):
    metrics = RequestMetrics(0, Rates("1", "1", "1"), uncached=uncached, cached=cached, output=output)
    assert metrics.cost == Decimal(uncached + cached + output) / Decimal(1000000)


def test_cache_creation_is_counted_as_input_without_becoming_a_cache_hit():
    metrics = RequestMetrics(0, Rates("1", "2", "0.1"), uncached=2, cached=3, cache_write=5, output=7)
    assert metrics.input == 10
    assert metrics.cost == Decimal("0.0000213")


@pytest.mark.parametrize("bad", [None, -1, 0.5, True, "10"])
def test_usage_rejects_non_token_counts(bad):
    with pytest.raises(ValueError):
        token_count(bad)


def test_unreported_usage_is_explicitly_incomplete():
    assert Metrics([RequestMetrics(0, Rates("1", "1", "1"))]).public()["incompleteRequests"] == 1


async def test_first_pair_is_released_only_after_both_arrive():
    gate = PairGate()
    body = {"model": "deepseek-flash", "messages": [{"role": "user", "content": "same"}]}
    left, right = await asyncio.gather(gate.arrive("official", body), gate.arrive("engy", body))
    assert left == right
    assert len(left) == 64


async def test_mismatched_first_prompts_reject_both_sides():
    gate = PairGate()
    results = await asyncio.gather(
        gate.arrive("official", {"messages": ["left"]}),
        gate.arrive("engy", {"messages": ["right"]}),
        return_exceptions=True,
    )
    assert all(isinstance(result, RuntimeError) for result in results)


async def test_preparation_failure_releases_peer_without_sending_anything():
    gate = PairGate()
    gate.abort("peer failed")
    with pytest.raises(RuntimeError, match="peer failed"):
        await gate.arrive("official", {})


def test_only_model_id_is_normalized_in_payload_comparison():
    assert canonical_body({"model": "a", "temperature": 1}) == canonical_body(
        {"model": "b", "temperature": 1}
    )
    assert canonical_body({"temperature": 1}) != canonical_body({"temperature": 0})


@pytest.mark.parametrize("phase", sorted(TERMINAL))
def test_terminal_states_cannot_restart(phase):
    state = LaneState(phase)
    for action in {action for actions in TRANSITIONS.values() for action in actions}:
        with pytest.raises(RuntimeError):
            state.advance(action)


def test_output_silence_is_not_a_grading_transition():
    state = LaneState("running")
    with pytest.raises(RuntimeError):
        state.advance("silence")
    assert state.advance("idle") == "grading"


def test_grader_has_a_smaller_equal_resource_budget_and_no_network():
    left = isolation("left", 0, Limits(), verifier=True)
    right = isolation("right", 1, Limits(), verifier=True)
    for arguments in (left, right):
        assert arguments[arguments.index("--cpus") + 1] == "0.5"
        assert arguments[arguments.index("--memory") + 1] == "256m"
        assert arguments[arguments.index("--network") + 1] == "none"
        assert arguments[arguments.index("--memory-swap") + 1] == "256m"
    assert set(left[left.index("--cpuset-cpus") + 1].split(",")).isdisjoint(
        right[right.index("--cpuset-cpus") + 1].split(",")
    )


def archive(entries):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as writer:
        for name, value, kind in entries:
            entry = tarfile.TarInfo(name)
            entry.type = kind
            entry.size = len(value) if kind == tarfile.REGTYPE else 0
            if kind == tarfile.SYMTYPE:
                entry.linkname = "/etc/passwd"
            writer.addfile(entry, io.BytesIO(value) if entry.size else None)
    return output.getvalue()


@pytest.mark.parametrize(
    "entries",
    [
        [("../escape", b"x", tarfile.REGTYPE)],
        [("/absolute", b"x", tarfile.REGTYPE)],
        [("link", b"", tarfile.SYMTYPE)],
        [("device", b"", tarfile.CHRTYPE)],
        [("same", b"1", tarfile.REGTYPE), ("same", b"2", tarfile.REGTYPE)],
    ],
)
def test_workspace_rejects_unsafe_or_ambiguous_archives(entries):
    with pytest.raises(ValueError):
        validate_snapshot(archive(entries))


def test_workspace_digest_is_content_based_and_independent_of_tar_order():
    entries = [("server.py", b"print('ok')", tarfile.REGTYPE), ("README", b"hello", tarfile.REGTYPE)]
    first = validate_snapshot(archive(entries))
    second = validate_snapshot(archive(list(reversed(entries))))
    changed = validate_snapshot(archive([(name, value + b"!", kind) for name, value, kind in entries]))
    assert first.sha256 == second.sha256
    assert changed.sha256 != first.sha256
    assert first.files == 2


def test_resp_error_wording_is_not_an_acceptance_requirement():
    assert equivalent(Error("wrong number of arguments"), Error("ERR arity"))
    assert not equivalent(Simple("OK"), b"OK")
