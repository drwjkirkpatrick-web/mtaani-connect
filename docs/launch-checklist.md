# Launch checklist (Prompt 42) — a real list of unambiguous HUMAN tasks.

Ordered; every box has one owner and one "done" condition. Code-side
prerequisites (ODPC evidence pack, DSAR flows, DPIA) are BUILT — their
human steps live below and in `docs/odpc-registration-checklist.md`.

## 0. Evidence is fresh
- [ ] Full CI run green on `main` (workflow: migrations check, postgis
      proven present, full suite, budget suite, geo suites, pip-audit).
- [ ] Restore drill (`scripts/backup_restore_drill.sh --execute`) PASSED
      on the launch DB within the last 7 days; offsite copy verified.
- [ ] Data inventory: `apps/compliance` DataMap re-synced against the
      final model set; zero detector drift in tests.

## 1. Compliance (human, ODPC)
- [ ] ODPC registration submitted and CERTIFICATE NUMBER recorded in
      Project_state.md (prompt 40 checklist §portal).
- [ ] DPIA (docs/dpia-live-location.md) reviewed with a fresh pair of
      eyes at launch scope; residual-risks signatures recorded.
- [ ] Consent ledger export spot-checked for 3 sample accounts.

## 2. Pilot (before any public push)
- [ ] 3–5 SACCO operators onboarded: fleet registered (plates verified
      against KRA logbooks by the OPERATOR — the app validates shape only),
      crew seats approved by each sacco admin (two-party rule).
- [ ] One pilot route seeded from the Digital Matatus GTFS (seed-quality
      label visible; community corrections queue monitored).
- [ ] Weekly pilot review scheduled: alerts volume, crowd-report quality,
      moderator queue age (all three dashboards read in ops review).

## 3. Moderation & trust staffing
- [ ] Two moderators registered (moderation's two-actor rule needs real
      staff); suspension/audit flow walked in staging.
- [ ] Escalation path documented for disputes needing human judgment.

## 4. Money & the off-switches
- [ ] Monetization flags CONFIRMED off (flags dashboard: nothing enabled;
      audit rows only for deliberate flips).
- [ ] Free-tier quotas sanity: per-user AI quota (5/day), notification
      dedupe window, throttle rules — reviewed at pilot scale.

## 5. Infrastructure & go-live
- [ ] Domain + Caddy TLS live; HSTS 31536000 enabled only AFTER the cert
      is stable (sticky header — see docs/runbooks/security.md).
- [ ] SESSION_COOKIE_SECURE=1, SECURE_SSL_REDIRECT=1, HSTS set in prod env.
- [ ] Backups: nightly cron + offsite + restore drill scheduled.
- [ ] Cron wiring for the sweeps (the ops list in Project_state.md):
      barter expiry, listing expiry, review publication, session/ping
      prune, alert_scan, analytics rebuild_daily.
- [ ] Health endpoint monitored externally (/health/ 200 = ok).
- [ ] Rollback rehearsal: prior compose tag redeployed in staging once.

## 6. Launch-week comms
- [ ] "How the app protects you" page reviewed (privacy statements
      current: TLS in transit, server-stored messages, NO E2E in V1).
- [ ] Crash/feedback channel live for pilot users.
