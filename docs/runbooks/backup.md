# Backup runbook (Prompt 41)

Cadence: nightly `pg_dump -Fc` · keep 14 daily + 8 weekly · offsite copy
weekly. Owner: Walker. A backup is only a backup once it has been
RESTORED — the drill is part of the nightly job's own verification.

## Nightly backup (cron example, 03:30 EAT)

```
PSQL=/path/to/psql PG_DUMP=/path/to/pg_dump \
DB_NAME=mtaani scripts/backup_restore_drill.sh --execute >> /var/log/mtaani/restore_drill.log 2>&1
```

- `--execute` = dump → gzip integrity → restore into `mtaani_drill_scratch`
  → table presence diff → PASS line. A night without a PASS line is an
  incident, not a blip.
- Offsite: copy the dump to encrypted storage the same night
  (`gpg --encrypt -r <operator-key>` or `restic` on the dump directory);
  GFS retention 14d/8w.

## Encryption

- At rest: restic/gpg layer above (the dumps are compressed archives —
  encrypt before leaving the machine, verify by test-decrypting monthly).
- The encrypted bucket keys live in the operator's password manager, never
  in the repo (the secrets-sweep test would catch them — that is the point).

## Restore procedure (incident)

1. Stop the app (or set it read-only): `pkill -f gunicorn` / compose stop.
2. `backup_restore_drill.sh --execute` already rehearsed the mechanics; for
   a real restore point the scratch DB is the LIVER: `pg_restore` into a
   fresh `mtaani_recovered`, then swap connection strings in `.env`.
3. Verify: `manage.py check` + `manage.py migrate --check` (no drift), spot
   check `mtaani_drill_scratch`-style table counts against the audit
   dashboard (`/analytics/` aggregates as sanity), crew ingest 401 shape
   (keys survived), then restart.
4. Post-incident: note the RPO gap (max 24 h) in the incident record —
   location pings lost to RPO never matter beyond 72 h (retention sweep).

## What is NOT in the database backup

- `MEDIA_ROOT` files (post/listing images) — separate `restic` of
  `mediafiles/`, same nightly cadence; verify one random file restore monthly.
- `.env` secrets — from the password manager ONLY; the deploy runbook says
  where each key goes.
