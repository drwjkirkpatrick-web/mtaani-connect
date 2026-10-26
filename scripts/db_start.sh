#!/usr/bin/env bash
# Start (or report running) the local dev Postgres. Idempotent.
set -euo pipefail
PGBIN="$HOME/micromamba/envs/pg/bin"
PGDATA="$(cd "$(dirname "$0")/.." && pwd)/.pgdata"
PORT="${DB_PORT:-5434}"

if "$PGBIN/pg_ctl" -D "$PGDATA" status >/dev/null 2>&1; then
  echo "postgres already running on port $PORT"
  exit 0
fi
"$PGBIN/pg_ctl" -D "$PGDATA" -l "$PGDATA/postgres.log"     -o "-p $PORT -c listen_addresses=127.0.0.1" start
