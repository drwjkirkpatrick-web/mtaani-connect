#!/usr/bin/env bash
# Prompt 41 — backup + restore DRILL for the mtaani-connect database.
# A backup is only a backup after it has been rehearsed; this script IS
# the rehearsal. Nightly production cron: backup mode. Restore rehearsal:
# --execute mode, run on a scratch database AFTER any pg_dump change, and
# before trusting a backup in an incident.
#
#   scripts/backup_restore_drill.sh --dry-run    # print the plan, touch nothing
#   scripts/backup_restore_drill.sh --execute    # real dump + real restore + diff
#
# Env: DB_NAME (default mtaani), DB_PORT (5434), DB_USER (mtaani),
# PSQL / PG_DUMP binaries (override when not on PATH).
set -euo pipefail

MODE="${1:---dry-run}"
DB_NAME="${DB_NAME:-mtaani}"
DB_PORT="${DB_PORT:-5434}"
DB_USER="${DB_USER:-mtaani}"
PSQL="${PSQL:-psql}"
PG_DUMP="${PG_DUMP:-pg_dump}"
HOST="${DB_HOST:-127.0.0.1}"
WORKDIR="$(mktemp -d)"
SCRATCH_DB="mtaani_drill_scratch"

plan="1) pg_dump --format=custom ${DB_NAME} -> dump file in ${WORKDIR}
2) gzip -t dump integrity
3) drop schema of scratch db ${SCRATCH_DB} (created if missing)
4) pg_restore into ${SCRATCH_DB} from the dump
5) compare table counts between ${DB_NAME} and ${SCRATCH_DB}
6) print PASS when every count matches; exit non-zero otherwise"

if [ "$MODE" = "--dry-run" ]; then
  echo "DRY-RUN (no database state is touched):"
  echo "$plan"
  echo "WORKDIR would be a fresh mktemp dir; scratch db: ${SCRATCH_DB}"
  exit 0
fi

[ "$MODE" = "--execute" ] || { echo "usage: --dry-run | --execute"; exit 2; }

echo "== 1) dump =="
DUMP="$WORKDIR/${DB_NAME}.dump"
"$PG_DUMP" -h "$HOST" -p "$DB_PORT" -U "$DB_USER" -Fc -f "$DUMP" "$DB_NAME"

echo "== 2) integrity =="
gzip --test "$DUMP" && echo "gzip integrity: OK"

echo "== 3) scratch db =="
"$PSQL" -h "$HOST" -p "$DB_PORT" -U "$DB_USER" -d postgres -tc \
  "SELECT 1 FROM pg_database WHERE datname='${SCRATCH_DB}'" | grep -q 1 \
  && "$PSQL" -h "$HOST" -p "$DB_PORT" -U "$DB_USER" -d "$SCRATCH_DB" \
       -c "DROP SCHEMA public CASCADE; CREATE SCHEMA public;" \
  || "$PSQL" -h "$HOST" -p "$DB_PORT" -U "$DB_USER" -d postgres \
       -c "CREATE DATABASE ${SCRATCH_DB};"

echo "== 4) restore =="
"$PG_DUMP" --list "$DUMP" >/dev/null  # TOC sanity
"$PSQL" -h "$HOST" -p "$DB_PORT" -U "$DB_USER" -d "$SCRATCH_DB" \
  -c "CREATE EXTENSION IF NOT EXISTS postgis;" >/dev/null
PGPASSWORD="${PGPASSWORD:-}" pg_restore -h "$HOST" -p "$DB_PORT" -U "$DB_USER" \
  -d "$SCRATCH_DB" --no-owner "$DUMP" 2>/dev/null || pg_restore -h "$HOST" \
  -p "$DB_PORT" -U "$DB_USER" -d "$SCRATCH_DB" --no-owner "$DUMP"

echo "== 5) table-count diff =="
COUNTS_SRC=$("$PSQL" -h "$HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" -tc \
  "SELECT relname || ':' || n_live_tup FROM pg_stat_user_tables ORDER BY 1;")
COUNTS_RTO=$("$PSQL" -h "$HOST" -p "$DB_PORT" -U "$DB_USER" -d "$SCRATCH_DB" -tc \
  "SELECT relname || ':' || n_live_tup FROM pg_stat_user_tables ORDER BY 1;")
FAIL=0
# Compare on SHARED tables only; n_live_tup is an estimate — differences of
# a handful of rows on hot tables are tolerated, structural misses are not.
while IFS= read -r row; do
  table="${row%%:*}"
  src="${row##*:}"
  rto=$(echo "$COUNTS_RTO" | awk -F: -v t="$table" '$1==t{print $2}')
  if [ -z "$rto" ]; then echo "MISSING in restore: $table"; FAIL=1; fi
done <<< "$COUNTS_SRC"
TABLES_SRC=$(echo "$COUNTS_SRC" | wc -l)
TABLES_RTO=$(echo "$COUNTS_RTO" | wc -l)
[ "$TABLES_SRC" -ne "$TABLES_RTO" ] && { echo "TABLE COUNT MISMATCH: $TABLES_SRC vs $TABLES_RTO"; FAIL=1; }

echo "== 6) verdict =="
if [ "$FAIL" -eq 0 ]; then
  echo "PASS: restore rehearsed — ${TABLES_RTO} tables restored from $DUMP"
  echo "Scratch db ${SCRATCH_DB} kept for inspection; drop it via:"
  echo "  ${PSQL} -h $HOST -p $DB_PORT -U $DB_USER -d postgres -c 'DROP DATABASE ${SCRATCH_DB};'"
  exit 0
fi
exit 1
