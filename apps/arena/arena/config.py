"""Validated experiment settings and redacted provider configuration."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[3]
APP = Path(__file__).resolve().parents[1]
STATE = ROOT / ".arena"
LANES = ("official", "engy")


def digest(path: Path) -> str:
    """Return the SHA-256 of a file's exact bytes."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_hashes() -> dict[str, str]:
    """Fingerprint the proof, controller, task, runtime, native UI integration, and tests."""
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


@dataclass(frozen=True)
class Rates:
    """USD per million tokens, represented as decimal strings without rounding."""

    input: str
    output: str
    cached: str

    def __post_init__(self) -> None:
        for value in asdict(self).values():
            try:
                number = Decimal(value)
            except InvalidOperation as error:
                raise ValueError("Prices must be decimal numbers") from error
            if not number.is_finite() or not 0 <= number <= 1000:
                raise ValueError("Prices must be within 0..1000 USD per million tokens")


@dataclass
class Provider:
    """A single route; credentials are excluded from serialized public state."""

    name: str
    base_url: str
    model: str
    models_url: str
    rates: Rates
    key: str = field(default="", repr=False)

    def public(self) -> dict[str, object]:
        """Describe the configured route without exposing its key."""
        return {
            "name": self.name,
            "baseUrl": self.base_url,
            "model": self.model,
            "rates": asdict(self.rates),
            "hasKey": bool(self.key),
        }


@dataclass(frozen=True)
class Limits:
    """Equal per-lane admission and execution limits."""

    cpus: int = 2
    memory_mb: int = 1536
    verifier_memory_mb: int = 256
    pids: int = 128
    max_rounds: int = 6
    max_tokens: int = 32768
    max_output_tokens: int = 500000
    max_seconds: int = 3600
    request_timeout: int = 300
    grade_timeout: int = 180
    verifier_cpus: str = "0.5"

    def __post_init__(self) -> None:
        bounds = {
            "cpus": (1, 8),
            "memory_mb": (512, 8192),
            "verifier_memory_mb": (256, 8192),
            "pids": (32, 512),
            "max_rounds": (1, 20),
            "max_tokens": (1024, 131072),
            "max_output_tokens": (1024, 2000000),
            "max_seconds": (60, 14400),
            "request_timeout": (10, 1800),
            "grade_timeout": (10, 600),
        }
        for name, (lower, upper) in bounds.items():
            value = getattr(self, name)
            if type(value) is not int or not lower <= value <= upper:
                raise ValueError(f"{name} must be an integer within {lower}..{upper}")
        if not Decimal("0.25") <= Decimal(self.verifier_cpus) <= Decimal(self.cpus):
            raise ValueError("verifier_cpus must be within 0.25..cpus")


def load_environment() -> dict[str, str]:
    """Read simple local dotenv assignments, with exported variables taking precedence."""
    values: dict[str, str] = {}
    for path in (ROOT / ".env", STATE / "keys.env"):
        if path.exists():
            for line in path.read_text().splitlines():
                key, separator, value = line.removeprefix("export ").partition("=")
                if separator and key.strip() in {"DEEPSEEK_API_KEY", "ENGY_API_KEY"}:
                    values[key.strip()] = value.strip().strip("\"'")
    values.update(os.environ)
    return values


def providers() -> dict[str, Provider]:
    """Load credentials locally and initialize explicit, editable price assumptions."""
    env = load_environment()
    return {
        "official": Provider(
            "DeepSeek official",
            "https://api.deepseek.com/anthropic",
            "deepseek-flash",
            "https://api.deepseek.com/models",
            Rates("0.30", "1.20", "0.006"),
            env.get("DEEPSEEK_API_KEY", ""),
        ),
        "engy": Provider(
            "Engy",
            "https://api.engy.ai",
            "deepseek-v4.1-flash-1017",
            "https://api.engy.ai/v1/models",
            Rates("0.03", "0.12", "0.006"),
            env.get("ENGY_API_KEY", ""),
        ),
    }


def validate_endpoint(value: str) -> str:
    """Accept HTTPS API roots and loopback HTTP roots used by keyless integration tests."""
    parsed = urlsplit(value)
    local = parsed.hostname in {"127.0.0.1", "localhost", "host.docker.internal"}
    if parsed.username or parsed.password or parsed.query or parsed.fragment or not parsed.hostname:
        raise ValueError("API base URL must have no credentials, query, or fragment")
    if parsed.scheme != "https" and not (parsed.scheme == "http" and local):
        raise ValueError("API endpoints must use HTTPS")
    return value.rstrip("/")


def canonical_body(body: dict[str, object]) -> bytes:
    """Normalize only the wire model id; every other first-request field must match."""
    return json.dumps(
        {**body, "model": "deepseek-flash"}, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode()
