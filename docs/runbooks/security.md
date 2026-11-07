# Security runbook (Prompt 41)

Owner: Walker · Review: quarterly + after any incident.

## Hardening status (what the suite enforces — tests/test_security.py)

- **Headers on every response** (SecurityHeadersMiddleware, registered after
  X-Frame-Options): CSP (settings.CONTENT_SECURITY_POLICY), `nosniff`,
  `Referrer-Policy`, `Cross-Origin-Opener-Policy`, `X-Frame-Options: DENY`.
- **Cookies**: `HttpOnly` + `SameSite=Lax` on session and CSRF;
  `SESSION_COOKIE_SECURE`/`CSRF_COOKIE_SECURE` MUST be `1` in prod (set in
  `.env`; the deploy runbook's env table pins it).
- **Transport**: `SECURE_PROXY_SSL_HEADER` preset for Caddy's forwarded
  proto; set `SECURE_SSL_REDIRECT=1` and `SECURE_HSTS_SECONDS=31536000`
  at first stable HTTPS launch (not before — HSTS is sticky).
- **Throttles** (Prompt 36): per-user/per-IP buckets, progressive lockouts;
  rules mirror the DB doors 1:1 (test-pinned equalities).
- **Dependency audit**: CI runs `pip-audit` (Prompt 42's workflow); locally:
  `.venv/bin/pip-audit --strict` before any dependency bump.

## CSP — the map-pitfall decision record

Leaflet 1.9.4 comes from `unpkg.com` (SRI-pinned in the map template) and
raster tiles from `*.tile.openstreetmap.org`; both hosts are the CSP's
only third-party allowances. The crew page ships a small INLINE script, so
`script-src` currently carries `'unsafe-inline'` — a deliberate, flagged
opening, NOT a silent one. Next pass: give the crew page a nonce
(`request.csp_nonce` pattern in the middleware), drop `'unsafe-inline'`,
and pin that in the security tests. Same for the inline `style` attributes
(keep them nonce-free until the inline-script move proves the pattern).

## Secrets sweep (suite-enforced)

`test_no_credential_looking_strings_in_tracked_files` fails the build if any
tracked file matches known key shapes (AWS `AKIA…`, `ghp_…`, `sk-…`,
`xox…`, PEM private-key blocks) outside the documented allow-list. New
credential-like fixtures must join the ALLOWLIST with a comment, or better:
never commit them shape at all — config through `.env` only.

## Incident quick-ref

1. **Suspected token/credential leak** → rotate (tracking sessions kill raw
   tokens on rotate; session cookies rotate via logoutall), then sweep logs.
2. **Database incident** → restore drill (backup_restore_drill.sh) from the
   last verified dump; KDPA reporting timelines for location-data breaches
   (docs/dpia-live-location.md §6).
3. **Dependency CVE** → pin, `pip-audit` clean, deploy, note in this file.

## Backup & restore

Nightly: `scripts/backup_restore_drill.sh` runs in BACKUP cron as
`pg_dump -Fc` (see docs/runbooks/backup.md for encryption + offsite copy).
The restore drill IS the script's `--execute` mode — rehearsed on a scratch
database, never on the live one; a backup is only a backup after the drill
has passed on it.
