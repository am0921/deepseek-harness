"""Executable Redis subset used to generate expected replies at an explicit clock."""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Simple:
    """A RESP simple string, distinct from a bulk byte string."""

    value: str


@dataclass(frozen=True)
class Error:
    """A RESP error; comparisons deliberately ignore its human-readable wording."""

    value: str = "ERR"


Reply = Simple | Error | bytes | int | None


def encode(value: Reply) -> bytes:
    """Encode one scalar RESP2 reply without interpreting bulk bytes as text."""
    if isinstance(value, Simple):
        return b"+" + value.value.encode() + b"\r\n"
    if isinstance(value, Error):
        return b"-" + value.value.encode() + b"\r\n"
    if value is None:
        return b"$-1\r\n"
    if isinstance(value, int):
        return b":" + str(value).encode() + b"\r\n"
    return b"$" + str(len(value)).encode() + b"\r\n" + value + b"\r\n"


def request(parts: list[bytes]) -> bytes:
    """Encode a command as a bulk-string array, including empty and non-UTF-8 bytes."""
    return b"*" + str(len(parts)).encode() + b"\r\n" + b"".join(encode(p) for p in parts)


def equivalent(actual: Reply, expected: Reply) -> bool:
    """Compare wire types and values, treating all RESP errors as equivalent."""
    if isinstance(actual, Error) and isinstance(expected, Error):
        return True
    return type(actual) is type(expected) and actual == expected


class Model:
    """A deterministic KV machine; all expiry decisions use the caller's millisecond clock."""

    def __init__(self) -> None:
        self.values: dict[bytes, bytes] = {}
        self.deadlines: dict[bytes, int] = {}

    def execute(self, parts: list[bytes], now: int) -> Reply:
        """Apply one command, preserving state on rejected integer mutations."""
        if not parts:
            return Error()
        command = parts[0].upper()
        counts = {b"PING": 1, b"SET": 3, b"GET": 2, b"DEL": 2, b"INCR": 2, b"PEXPIRE": 3, b"PTTL": 2}
        if command not in counts or len(parts) != counts[command]:
            return Error()
        if command == b"PING":
            return Simple("PONG")
        key = parts[1]
        if key in self.deadlines and self.deadlines[key] <= now:
            self.values.pop(key, None)
            self.deadlines.pop(key, None)
        if command == b"SET":
            self.values[key] = parts[2]
            self.deadlines.pop(key, None)
            return Simple("OK")
        if command == b"GET":
            return self.values.get(key)
        if command == b"DEL":
            existed = key in self.values
            self.values.pop(key, None)
            self.deadlines.pop(key, None)
            return int(existed)
        if command == b"PTTL":
            if key not in self.values:
                return -2
            return self.deadlines[key] - now if key in self.deadlines else -1
        if command == b"PEXPIRE":
            try:
                duration = int(parts[2])
            except ValueError:
                return Error()
            if key not in self.values:
                return 0
            if duration <= 0:
                self.values.pop(key)
                self.deadlines.pop(key, None)
            else:
                self.deadlines[key] = now + duration
            return 1
        value = self.values.get(key, b"0")
        if not re.fullmatch(rb"(?:0|[1-9][0-9]*|-[1-9][0-9]*)", value) or len(value) > 20:
            return Error()
        number = int(value)
        if not -(2**63) <= number < 2**63 - 1:
            return Error()
        self.values[key] = str(number + 1).encode()
        return number + 1
