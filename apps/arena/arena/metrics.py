"""Provider-reported token accounting and incremental, byte-safe SSE observation."""

from __future__ import annotations

import codecs
import json
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from .config import Rates


class SSE:
    """Decode SSE across arbitrary UTF-8/chunk boundaries and join repeated data lines."""

    def __init__(self) -> None:
        self.decoder = codecs.getincrementaldecoder("utf-8")()
        self.buffer = ""
        self.data: list[str] = []

    def feed(self, chunk: bytes, final: bool = False) -> list[dict[str, Any]]:
        """Return complete JSON events; comments and empty-data events produce no value."""
        self.buffer += self.decoder.decode(chunk, final)
        if len(self.buffer) > 4 * 1024 * 1024:
            raise ValueError("SSE line exceeds the 4 MiB telemetry limit")
        if final and self.buffer and not self.buffer.endswith("\n"):
            self.buffer += "\n"
        events = []
        while "\n" in self.buffer:
            line, self.buffer = self.buffer.split("\n", 1)
            line = line.removesuffix("\r")
            if line.startswith("data:"):
                self.data.append(line[5:].removeprefix(" "))
            elif not line and self.data:
                payload = "\n".join(self.data)
                self.data.clear()
                if payload != "[DONE]":
                    value = json.loads(payload)
                    if not isinstance(value, dict):
                        raise ValueError("SSE JSON event must be an object")
                    events.append(value)
        if final and self.data:
            payload = "\n".join(self.data)
            self.data.clear()
            if payload != "[DONE]":
                value = json.loads(payload)
                if not isinstance(value, dict):
                    raise ValueError("SSE JSON event must be an object")
                events.append(value)
        return events


def token_count(value: object) -> int:
    """Reject missing, negative, fractional, or boolean token counts."""
    if type(value) is not int or value < 0:
        raise ValueError(f"Invalid provider token count: {value!r}")
    return value


@dataclass
class RequestMetrics:
    """One cumulative provider usage record; SSE updates replace rather than add totals."""

    started: float
    rates: Rates
    first_token: float | None = None
    last_token: float | None = None
    ended: float | None = None
    uncached: int | None = None
    cached: int = 0
    cache_write: int = 0
    output: int | None = None
    stopped: bool = False
    http_status: int | None = None
    error: str | None = None

    def observe(self, event: dict[str, Any], now: float) -> list[tuple[str, str]]:
        """Observe Messages events without altering bytes delivered to Dsh."""
        kind = event.get("type")
        usage = None
        visible: list[tuple[str, str]] = []
        if kind == "message_start":
            usage = event.get("message", {}).get("usage")
        elif kind == "message_delta":
            usage = event.get("usage")
        elif kind == "message_stop":
            self.stopped = True
        elif kind == "error":
            self.error = json.dumps(event.get("error", {}), ensure_ascii=False)
        elif kind == "content_block_start":
            block = event.get("content_block", {})
            if block.get("type") == "tool_use":
                visible.append(("tool", str(block.get("name", ""))))
            for field_name, channel in (("text", "text"), ("thinking", "reasoning")):
                if block.get(field_name):
                    visible.append((channel, block[field_name]))
        elif kind == "content_block_delta":
            delta = event.get("delta", {})
            for field_name, channel in (
                ("text", "text"),
                ("thinking", "reasoning"),
                ("partial_json", "code"),
            ):
                if delta.get(field_name):
                    visible.append((channel, delta[field_name]))
        if visible:
            self.first_token = now if self.first_token is None else self.first_token
            self.last_token = now
        if usage is not None:
            fields = {
                "input_tokens": "uncached",
                "cache_read_input_tokens": "cached",
                "cache_creation_input_tokens": "cache_write",
                "output_tokens": "output",
            }
            for remote, local in fields.items():
                if remote in usage:
                    setattr(self, local, token_count(usage[remote]))
        return visible

    @property
    def input(self) -> int:
        """Total prompt tokens, including cache reads and cache writes exactly once."""
        return (self.uncached or 0) + self.cached + self.cache_write

    @property
    def cost(self) -> Decimal:
        """Estimated USD under the request's frozen price assumptions, never an invoice."""
        return (
            Decimal(self.input - self.cached) * Decimal(self.rates.input)
            + Decimal(self.cached) * Decimal(self.rates.cached)
            + Decimal(self.output or 0) * Decimal(self.rates.output)
        ) / Decimal(1000000)

    def public(self) -> dict[str, object]:
        """Expose measured timing and whether final provider accounting was complete."""
        duration = None
        if self.first_token is not None and self.last_token is not None:
            duration = self.last_token - self.first_token
        speed = (self.output - 1) / duration if self.output and duration and self.output > 1 else None
        return {
            "input": self.input,
            "uncached": self.uncached,
            "cached": self.cached,
            "cacheWrite": self.cache_write,
            "output": self.output,
            "cost": str(self.cost),
            "ttftMs": None if self.first_token is None else (self.first_token - self.started) * 1000,
            "tokensPerSecond": speed,
            "generationSeconds": duration,
            "complete": self.stopped
            and self.uncached is not None
            and self.output is not None
            and self.error is None,
            "httpStatus": self.http_status,
            "error": self.error,
        }


@dataclass
class Metrics:
    """Per-lane totals built from distinct request records, including failed attempts."""

    requests: list[RequestMetrics] = field(default_factory=list)
    ping_ms: float | None = None
    ping_at: str | None = None
    ping_error: str | None = None

    def public(self) -> dict[str, object]:
        """Return accumulated usage plus the most recent measured generation throughput."""
        input_tokens = sum(item.input for item in self.requests)
        cached = sum(item.cached for item in self.requests)
        speeds = [item.public() for item in self.requests if item.ended is not None]
        last = speeds[-1] if speeds else {}
        return {
            "input": input_tokens,
            "output": sum(item.output or 0 for item in self.requests),
            "cached": cached,
            "cachePercent": 100 * cached / input_tokens if input_tokens else 0,
            "cost": str(sum((item.cost for item in self.requests), Decimal(0))),
            "requestCount": len(self.requests),
            "incompleteRequests": sum(not item.public()["complete"] for item in self.requests),
            "tokensPerSecond": last.get("tokensPerSecond"),
            "ttftMs": last.get("ttftMs"),
            "pingMs": self.ping_ms,
            "pingAt": self.ping_at,
            "pingError": self.ping_error,
            "streaming": bool(self.requests and self.requests[-1].ended is None),
        }
