# Project State: Mtaani Connect

> **Last updated:** 2026-10-25
> **Current phase:** build — Phase 0 (Foundation) COMPLETE (01–07); Phase 1 (Community) starts next
> **Overall health:** green

---

## 1. Goal (1–2 sentences)

Build the Kenyan community super-app from `docs/product-brief.md`: matatu
tracking, community guides, utility-first social, marketplace, barter, and
messaging — working on low-cost Android over 3G, honest about data gaps,
KDPA-compliant from day one.

## 2. Current Status

### Done
- [x] Product brief ingested into repo (`docs/product-brief.md`)
- [x] Prompt 01 — scaffold: Django 6.1.2 venv, settings split, compose (portable) + conda Postgres 18/PostGIS on :5434 (this host), 3 smoke tests
- [x] Prompt 02 — core vocabulary: 9 enums + TimestampedModel + testing utils, 15 tests
- [x] Prompt 03 — geo: haversine cross-checked against PostGIS, 2 km privacy grid, ward seed, PostGIS migration + system check, 7 tests
- [x] Prompt 04 — auth: mock-first comms adapters, E.164 phone validation, OTP flows (hashed/single-use/expiring/rate-limited), KDPA consent events, recovery tokens, login throttle, 19 tests
- [x] Prompt 05 — PWA shell: mobile-first base template (bottom tab bar), 2.9 KB hand-written CSS (15 KB budget), SW with GET-only/no-auth/never-stale gates + offline page, manifest + generated icons, EN/SW i18n (LocaleMiddleware + polib-compiled catalogs, 20 strings), 15 tests + real-server smoke (12 routes 200, lang round-trip)
- [x] Prompt 06 — image pipeline: pure core/images.py (EXIF+GPS-IFD stripped and tested, WebP budgets std≤1280px/40KB + thumb≤320px/15KB, no-upscale policy), apps/media MediaRef + upload endpoint; conftest now registers shared fixtures; 12 tests
- [x] Prompt 07 — profiles: allow-list public_data (strict key-set test = the privacy guarantee), location granularity ladder (ward/city/country), per-field-group visibility (public/members/private), precise-location consent stored but unconsumed, staff-only seller verification gate, avatar via pipeline; 12 tests
- [x] Web research: Digital Matatus GTFS, KDPA 2019/ODPC, bundle economics,
      SACCO tracking landscape, competitors (`docs/research-notes.md`)
- [x] Stack decision documented (`docs/adr/0001-stack-decision.md`)
- [x] Build contract written: `PROMPTS.md` — 42 testable prompts, 6 phases,
      one-commit-per-prompt, parent-owned shared files, mock-first adapters
- [x] Repo scaffolded on GitHub (private) with remote `origin` set

### In Progress
- [ ] Prompt 08 — guides: local knowledge CRUD + safety warnings (Phase 1 begins)

### Not Started
- [ ] Prompts 04–42 execution
- [ ] User research (15–20 Nairobi interviews, brief §10) — may flip Phases 1/3
- [ ] SACCO pilot conversations (3–5 operators)
- [ ] ODPC registration (human task, launch blocker — see Prompt 40)

## 3. Architecture & Key Decisions

| Decision | Rationale | Date |
|---|---|---|
| Django 6 + PostgreSQL/PostGIS modular monolith | Brief mandates modular monolith + PostGIS; auth/i18n/migrations batteries | 2026-10-25 |
| Server-rendered + light JS + Leaflet, no SPA | 50 KB page budgets for 3G / 70 MB daily bundles | 2026-10-25 |
| PWA first, native later | Android-first on low-cost phones without two codebases | 2026-10-25 |
| One Django app per prompt under `apps/` | Enforces one-agent-per-file ownership | 2026-10-25 |
| Route DB seeded from Digital Matatus GTFS (stale 2018 data = seed only) | Open data, community-correction queue | 2026-10-25 |
| No money custody in V1; M-Pesa = future adapter | Brief §3 Module 6 | 2026-10-25 |

## 4. Blockers & Risks

- **Blocker:** none technical. Human tasks before launch: ODPC registration,
  pilot SACCOs, user research (all tracked in Prompt 40 + launch checklist).
- **Risk:** Phase ordering (community-first) is an assumption → mitigation:
  phases 1/3 designed swappable (PROMPTS.md swap note).
- **Risk:** crew won't share location if data/battery cost is too high →
  mitigation: Prompt 29's low-data flow + visible data counter; validate in pilot.

## 5. Next Step (only ONE)

> **Next:** Execute Prompt 08 — Guides (WikiHow for Kenya): `apps/guides/`
> — categories, ordered steps, safety warnings enforced in clean() for
> MEDICAL/ELECTRICAL/CONSTRUCTION, revisions, community confirmations.
> Suite at 83 tests (Phase 0 complete).

## 6. Environment & Tooling Notes

- Runtime / language: Python 3.14 available on Jetson host; Django 6 supports it
- Database (THIS host): walker-owned PostgreSQL 18.6 + PostGIS 3.6, micromamba env `pg`,
  127.0.0.1:5434 — `make db-start` / `make db-stop` (docker daemon unusable: missing
  xt_addrtype kernel module; conda path needs no root)
- Database (generic hosts): `docker compose up` → postgis/postgis:16
- Secrets location: `.env` (from `.env.example`), never committed
- Hermes skills used: software-development-workflows (prompts-first pattern),
  project-state-management, remedy_personality_picker (Phosphorus for README)
- GitHub: `drwjkirkpatrick-web/mtaani-connect` (private), branch `main`
- Convention: Walker reviews and pushes to `origin main` after commits

## 7. Recent Session Log

- 2026-10-25: Contract, research, ADR, repo scaffold (see git root-commit).
- 2026-10-25: Provisioned no-root Postgres 18/PostGIS via micromamba after docker
  daemon failure; executed Prompts 01–03 one-commit-per-prompt, 25 tests green.
- 2026-10-25: Prompt 04 landed (commit 5d3a73b): auth/verification/recovery/throttle,
  19 new tests, suite at 44 green.
- 2026-10-26: Repo public + pushed; 'Mtaani' spelling confirmed (mtaa + locative -ni);
  hero emoji 🚐 set (README + repo description/topics); Prompt 05 landed, 59 tests green.
- 2026-10-26: Prompts 06 (image pipeline: EXIF/GPS strip verified, WebP budgets,
  no-upscale fix) + 07 (profiles: allow-list serializer, privacy ladder, staff gate)
  landed — PHASE 0 COMPLETE, suite at 83 tests green.

## 8. References

- Build contract: `PROMPTS.md`
- Brief: `docs/product-brief.md`
- Research: `docs/research-notes.md`
- ADRs: `docs/adr/`