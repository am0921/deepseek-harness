#!/bin/sh
set -eu
arena_bind_address=$(hostname -i | awk '{print $1}')
exec node /opt/dsh/apps/cli/lib/bin.js --profile web --patch /etc/arena/ptc.yml \
  --host "$arena_bind_address" --port 3080 --no-open \
  --public-url "$ARENA_PUBLIC_URL" --trusted-host 127.0.0.1 localhost
