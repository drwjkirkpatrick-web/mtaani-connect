# mtaani-connect operator README (Prompt 42)

The clone → CI → deploy path, plus the keys it needs and how to know it is
healthy. The LAUNCH gating list is `docs/launch-checklist.md`.

## Deploy path

1. CI green on `main` (the workflow proves PostGIS presence, migrations
   completeness, the full suite, the budget suite, and pip-audit).
2. Provision the server with Docker + the `deploy/` compose:
   - `cp .env.example .env` style: set `DB_PASSWORD`, `SECRET_KEY`,
     `ALLOWED_HOSTS` (comma-separated). Prod transport: `SESSION_COOKIE_
     SECURE=1`, `SECURE_SSL_REDIRECT=1`, `SECURE_HSTS_SECONDS=31536000`
     (HSTS only after TLS is stable — it is sticky).
   - `docker compose -f deploy/docker-compose.prod.yml up -d`
   - Migrate: `docker compose -f deploy/docker-compose.prod.yml exec app
     python manage.py migrate`
   - Point Caddy at the real domain in `deploy/Caddyfile`; Caddy issues
     the certificate automatically.
3. Verify health: `curl https://<domain>/health/` → `{"ok": true}` (DB-free
   by design, so the proxy check always answers).
4. Smoke: landing 200; market list 200; login round-trip maps a session;
   `/admin/` staff login.

## Environment keys (all through `.env` on the server, never the repo)

| Key | Purpose | Notes |
|-----|---------|-------|
| `DB_PASSWORD` | postgres superuser pair | compose requires it (no silent default) |
| `SECRET_KEY` | Django signing | 50+ random chars; rotate via the security runbook |
| `ALLOWED_HOSTS` | host header allow-list | prod only |
| `SESSION_COOKIE_SECURE` / `SECURE_SSL_REDIRECT` / `SECURE_HSTS_SECONDS` | transport hardening | see Prompt 41 runbook |
| `REDIS_URL` | throttle store (Prompt 36) | wire once at boot; restart-loss acceptable |
| `SMS_ADAPTER` / `EMAIL_ADAPTER` | comms adapters (Prompt 04) | mock-first; real adapters swap via env only |

## Backup & failover

- Nightly: `scripts/backup_restore_drill.sh --execute` (docs/runbooks/backup.md).
- Rollback: deploy the previous compose build tag, `.env` untouched, verify
  `/health/` + smoke; the DB schema is additive by migration policy —
  rollbacks are app-image swaps, not schema reversals.

## Where things live

- Ops dashboards: `/analytics/` (staff), queue (`/analytics/queue/`), SACCO
  hours (`/analytics/sacco/`).
- Compliance: `/compliance/` (DSAR queue, data map, consent exports).
- Monetization: `/monetize/flags/` (all off at launch — by design).
- Cron list (all sweeps): Project_state.md §ops.
