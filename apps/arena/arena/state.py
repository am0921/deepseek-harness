"""Runtime use of the finite transition relation checked by Lean."""

from __future__ import annotations

import json
from dataclasses import dataclass

from .config import APP

TRANSITIONS: dict[str, dict[str, str]] = json.loads((APP / "formal/transitions.json").read_text())
TERMINAL = frozenset({"passed", "error", "stopped", "limit"})


@dataclass
class LaneState:
    """A single owner mutates a lane through the checked transition table."""

    phase: str = "preparing"
    round: int = 0

    def advance(self, signal: str) -> str:
        """Reject any transition outside the formally checked relation."""
        try:
            self.phase = TRANSITIONS[self.phase][signal]
        except KeyError as error:
            raise RuntimeError(f"Invalid lane transition: {self.phase} / {signal}") from error
        return self.phase
