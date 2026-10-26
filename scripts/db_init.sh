#!/usr/bin/env bash
# One-time: initialise the walker-owned Postgres 16 + PostGIS dev database.
# WHY: docker daemon is unavailable on the Jetson dev host, so the DB runs
# from a conda env under $HOME - no root needed anywhere.
set -euo pipefail
PGBIN="$HOME/micromamba/envs/pg/bin"
PGDATA="$(cd "$(dirname "$0")/.." && pwd)/.pgdata"
PORT="${DB_PORT:-5434}"

if [ -d "$PGDATA" ]; then echo "pgdata already initialised at $PGDATA"; exit 0; fi

"$PGBIN/initdb" -D "$PGDATA" -U mtaani --auth=trust -E UTF8
"$PGBIN/pg_ctl" -D "$PGDATA" -l "$PGDATA/postgres.log"     -o "-p $PORT -c listen_addresses=127.0.0.1" start
sleep 1
"$PGBIN/createdb" -h 127.0.0.1 -p "$PORT" -U mtaani mtaani
"$PGBIN/psql" -h 127.0.0.1 -p "$PORT" -U mtaani -d mtaani \
    -c "CREATE EXTENSION IF NOT EXISTS postgis;" \
    -c "CREATE EXTENSION IF NOT EXISTS pg_trgm;"
echo "DB ready on 127.0.0.1:$PORT (user mtaani, db mtaani, postgis + pg_trgm installed)"
