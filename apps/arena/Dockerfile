# The builder compiles the fork, including its pinned dependency lockfile.
FROM node:24-bookworm-slim@sha256:55fb5f5a527ea2c5c4a95f963e48ef90aa38c7b346054fdc8ee9957a2b55d205 AS builder
RUN apt-get update && apt-get install -y --no-install-recommends python3 make g++ git ca-certificates && rm -rf /var/lib/apt/lists/*
WORKDIR /opt/dsh
COPY *.json *.yaml *.yml *.ts *.md LICENSE ./
COPY vendor vendor
COPY native native
COPY packages packages
COPY scripts scripts
COPY patches patches
COPY apps/cli apps/cli
COPY apps/desktop apps/desktop
COPY apps/desktop-host apps/desktop-host
COPY apps/plugin-market-generator apps/plugin-market-generator
COPY apps/web apps/web
COPY python python
COPY benchmarks benchmarks
COPY website website
COPY docs docs
COPY .agents .agents
RUN corepack enable && corepack prepare pnpm@11.28.5 --activate
RUN pnpm install --frozen-lockfile
ARG DSH_CLIENT_COMMIT_HASH
RUN DSH_CLIENT_COMMIT_HASH=${DSH_CLIENT_COMMIT_HASH} pnpm run build:artifacts
# Tests and reference implementations never enter the contestant image.
RUN rm -rf apps/arena && find . -type d -name tests -prune -exec rm -rf '{}' +

FROM node:24-bookworm-slim@sha256:55fb5f5a527ea2c5c4a95f963e48ef90aa38c7b346054fdc8ee9957a2b55d205 AS agent
RUN apt-get update && apt-get install -y --no-install-recommends python3 bash git tini ca-certificates && rm -rf /var/lib/apt/lists/*
COPY --from=builder /opt/dsh /opt/dsh
COPY apps/arena/runtime/ptc.yml /etc/arena/ptc.yml
COPY apps/arena/runtime/start.sh /etc/arena/start.sh
RUN mkdir -p /workspace /home/node/.dsh && chown -R node:node /workspace /home/node/.dsh
ENV DSH_HOME=/home/node/.dsh PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER node
WORKDIR /workspace
ENTRYPOINT ["tini", "--", "sh", "/etc/arena/start.sh"]

FROM node:24-bookworm-slim@sha256:55fb5f5a527ea2c5c4a95f963e48ef90aa38c7b346054fdc8ee9957a2b55d205 AS verifier
RUN apt-get update && apt-get install -y --no-install-recommends python3 redis-server tini && rm -rf /var/lib/apt/lists/*
COPY apps/arena/arena/grader.py apps/arena/arena/reference.py /opt/grader/
RUN chmod 700 /opt/grader && mkdir -p /workspace && chmod 755 /workspace
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /workspace
ENTRYPOINT ["tini", "--", "python3", "/opt/grader/grader.py"]
