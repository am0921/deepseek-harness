# Dsh Arena

English | [中文](README.zh.md)

## Summary

Run the official DeepSeek API and Engy side by side with the same Dsh PTC configuration and task. Each pane embeds the original Dsh Web UI. The surrounding controls show usage, speed, latency, estimated cost, and grading results. Read the [Japanese guide](GUIDE.ja.md) for the full operational contract.

## Table of Contents

- [Run locally](#run-locally)
- [Fairness and grading](#fairness-and-grading)
- [Verification scope](#verification-scope)

<a id="run-locally"></a>
## Run locally

Install `uv` and start Docker Desktop with at least 5 vCPUs and 4 GiB available to its VM.

```sh
./apps/arena/setup.sh
./apps/arena/run.sh --open
```

Open `http://127.0.0.1:8765`, enter the keys in connection settings, and press the simultaneous start button. The controller also reads `DEEPSEEK_API_KEY` and `ENGY_API_KEY` from the environment, the ignored root `.env`, or `.arena/keys.env`. Optional saved keys use mode 0600; agents receive only run-scoped gateway credentials.

The demo exercises real Dsh, PTC, Docker, grading, repair, and regrading through a deterministic local provider. Its API usage and prices are explicitly simulated.

```sh
./apps/arena/run.sh --port 8775 --demo
./apps/arena/verify.sh
```

Stop releases retained agent containers. Runs and reports are stored under `.arena/runs/`. After changing verified source, rerun verification and restart the controller before starting an experiment.

<a id="fairness-and-grading"></a>
## Fairness and grading

Both lanes use the same immutable agent image and PTC settings. A barrier forwards the initial requests only after their JSON bodies match apart from the model ID. The UI reports the actual dispatch difference; arrival times at separate providers cannot be synchronized.

Each agent has 2 vCPUs on disjoint CPU sets, 1536 MiB without swap, 128 processes, and a 256 MiB tmpfs workspace. Docker settings are checked after container creation. Provider hardware, cache state, and load remain outside this controller's control.

Each lane has a separate internal network. Its only outward transport is a fixed-destination relay to the authenticated model gateway, capped at 0.25 vCPU and 64 MiB with IP forwarding disabled. Agents cannot dial the host control API or the other lane directly. A real Docker test checks both successful model transport and blocked direct-IP access to host control.

At each completed SDK turn, the controller pauses the agent, snapshots its files, and runs a separate offline grader limited to 0.5 vCPU and 256 MiB. Candidate servers run under a different UID from the protected grader. The suite has 23 cases, including 1600 deterministic generated operations; four independent cases can run concurrently. The exact JSON report returns to the same Session, and repair continues within the configured limits. After a passing result, the agent acknowledges it and the controller verifies that the graded files remain unchanged.

Token/s uses API-reported output tokens and content-delta timing. Ping is authenticated model-list HTTP latency. Cached input and its percentage use reported cache-read tokens. Cost is an estimate from editable USD-per-million rates, initially the [official peak rates](https://api-docs.deepseek.com/quick_start/pricing/) and [Engy rates](https://engy.ai/pricing); incomplete usage remains marked.

<a id="verification-scope"></a>
## Verification scope

Lean 4.35.0 checks 18 model theorems covering release conditions, state transitions, resource partitioning, TTL and integer rules, and cache billing. Its enumerated transition table must match the Python runtime table, with no `sorry` or custom axioms. Property tests, real Redis oracle traces, a passing control, six rejected mutants, and real Docker snapshot checks validate the implementation separately.

These proofs cover the stated mathematical model. They do not prove the entire Python implementation, OS, TCP, Docker, upstream models, or correctness of arbitrary submissions. A finite suite is not a complete proof for every possible program. The verification manifest records source hashes and invalidates the start button when those sources change.
