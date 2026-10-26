#!/bin/sh
set -eu
cd "$(dirname "$0")/../.."
mkdir -p .cache/arena
uv sync --project apps/arena --frozen
uv run --directory apps/arena python prepare_lean.py
docker info --format 'Docker {{.ServerVersion}} / {{.NCPU}} vCPUs / {{.MemTotal}} bytes'
arena_commit=$(git rev-parse HEAD)
docker build --build-arg "DSH_CLIENT_COMMIT_HASH=$arena_commit" --target agent -t dsh-arena-agent:local -f apps/arena/Dockerfile .
docker build --target verifier -t dsh-arena-verifier:local -f apps/arena/Dockerfile .
exec ./apps/arena/verify.sh
