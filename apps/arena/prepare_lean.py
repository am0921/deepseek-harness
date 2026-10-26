"""Install the pinned, checksum-verified official Lean release into this checkout's cache."""

import hashlib
import platform
import subprocess
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RELEASES = {
    ("Darwin", "arm64"): (
        "darwin_aarch64",
        "c0c6f34f35ba1dcc5c4a5fd106fefab9eb5e8469712624dd2c868d51b63177c2",
    ),
    ("Linux", "aarch64"): (
        "linux_aarch64",
        "c8f0f8a3920e839718b1343e6781fd80a3e9b1024fbcd7078eb30f54e806b028",
    ),
    ("Linux", "x86_64"): ("linux", "7289597a44543246c9227b8cae501767e095ce17f6d448b95fc4a8dc97268f6d"),
}


def main():
    """Print the pinned executable path after verifying or installing its release."""
    target, expected = RELEASES[(platform.system(), platform.machine())]
    cache = ROOT / ".cache/arena"
    cache.mkdir(parents=True, exist_ok=True)
    executable = cache / f"lean-4.35.0-{target}/bin/lean"
    if not executable.exists():
        archive = cache / "lean.tar.zst"
        if not archive.exists():
            url = (
                f"https://github.com/leanprover/lean4/releases/download/v4.35.0/lean-4.35.0-{target}.tar.zst"
            )
            print("Downloading official Lean 4.35.0…", flush=True)
            with urllib.request.urlopen(url, timeout=60) as source, archive.open("wb") as destination:
                while chunk := source.read(1024 * 1024):
                    destination.write(chunk)
        with archive.open("rb") as source:
            actual = hashlib.file_digest(source, "sha256").hexdigest()
        if actual != expected:
            raise RuntimeError(f"Lean archive checksum mismatch: {actual}")
        subprocess.run(["tar", "-xf", str(archive), "-C", str(cache)], check=True)
    print(executable)


if __name__ == "__main__":
    main()
