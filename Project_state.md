# Project State: Mtaani Connect

> **Last updated:** 2026-11-06
> **Current phase:** build — Phase 2: 17–24 complete except 19 (geo search, next)
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
- [x] Prompt 08 — guides: local knowledge CRUD + safety warnings — Guide/
      GuideStep/GuideRevision/GuideConfirmation, safety-warning gate enforced in
      clean() on BOTH Guide and GuideRevision (MEDICAL/ELECTRICAL/CONSTRUCTION),
      every edit = new draft revision, published view = latest PUBLISHED revision,
      ward-coarse community confirmations, EN→SW title fallback always labelled,
      no user HTML (linebreaks only); 22 tests (105 total)
- [x] Prompt 09 — offline saved guides: OfflineSavedGuide + paged
      offline-manifest.json with content-only revision hashes (gsh1: sha256),
      50 MB quota estimate + warning, no_data honesty state; parent SW glue:
      guide cache on save, hash-only re-sync (dates never), manifest-authoritative
      sweep, offline list page cache-first; 18 tests (Prompt 09 total: 123)
- [x] Prompt 10 — posts & photos: ≤2,000-char posts, 4-image cap enforced
      in clean() + upload endpoint, typed guide reference + honest listing
      placeholder (17 pending), educational tag, short-form first-class; 23 tests (146)
- [x] Prompt 12 — Q&A + AI-answer labeling: is_machine boolean is the single
      data-driven badge source (no form/view path sets it; set_machine_answer
      service is Prompt 33's doorway), asker-accept with safety gate
      (MEDICAL/ELECTRICAL needs a human-verified community answer), verified
      votes; 33 tests (179)
- [x] Prompt 13 — follows & groups: asymmetric Follow (unique pair, no
      self-edge), idempotent services, follower counts hidden below 10
      (visible_follower_count, no count column to bypass), GroupKind groups
      with owner/member/moderator roles, feed hook member_group_ids contract
      test; 40 tests (219)
- [x] Prompt 15 — moderation v1: generic-FK reports (reason enum, allow-listed
      targets), revocable user blocks with sibling-usable visibility filters,
      immutable audit rows, two-actor suspension draft→confirm gate, report
      rate limit 10/rolling-hour (injected clock), removals only set
      ContentStatus.REMOVED (never hard-deleted); 30 tests (249)
- [x] Prompt 11 — utility feed: pure injected-clock rank() (max 9.0 scoring
      table in docstring), 10-item pages + explicit end-of-feed, read-only
      assembly over sibling public APIs; 11 tests
- [x] Prompt 14 — cross-entity search: SearchDocument maintained by Postgres
      triggers (other apps untouched), pg_trgm trusted extension, bilingual
      en+sw single vector, trigram typo tolerance, grouped results; 14 tests
- [x] Prompt 16 — trust signals: helpful votes (generic-FK posts/qa),
      read-time rollups (lifetime + 90d), admin-tunable tier thresholds,
      once-only badge events; parent plugs scorer into feed + badge partial
      on profiles/QA; 20 tests (295 total)
- [x] Prompt 17 — marketplace listings (goods): seedable categories,
      price/negotiable, condition grades, ≤6 images (three enforcement
      layers), ward-only location + coordinate-refusing clean(), phone
      detector → human review queue, /market/ live; 53 tests (348)
- [x] Prompt 20 — messaging v1: IDOR-safe threads (single for_participant
      gate, 404s for non-members), marketplace-enquiry origin, immutable
      messages, ≤4 images, honest read receipts with opt-out, unread badges,
      report→auto-mute, TLS-no-E2E privacy page; 36 tests (384)
- [x] Consolidation — one scam-detector (listings' canonical, +01XX/1XX),
      moderation allow-list joins listings+messaging, messaging phase-race
      fallback + listing resolver shim removed, posts listing previews LIVE
      (honest UNAVAILABLE when gone); 385 tests
- [x] Prompt 18 — services marketplace: separate ServiceListing, rate-type
      matrix in clean(), ≤8-image portfolio (three layers), provider page,
      reviews hook via contract doors; 44 tests
- [x] Prompt 21 — barter & cash-plus: versioned offer lineage (self-FK
      parents, ping-pong parties), strict model-enforced state machine with
      full illegal-move matrix, 7-day injected-clock expiry sweep, zero
      custody (stated cash only; MPesaAdapter stub documented); 33 tests
- [x] Prompt 22 — reviews & reputation: mutual DB-constrained reviews,
      sweep-only publication (pairs same tick / lone at 14d), 1-star dispute
      note gate, reputation_for() (0.6 stars + 0.4 trust), partials on
      profiles + services' display door + barter's review doors; 35 tests (497)
- [x] Prompt 23 — disputes & fraud: per-deal cases (one open each, root-
      normalized), id-only evidence (barter trail + enquiry messages),
      propose→different-human-confirm via moderation's public machinery,
      heuristics annotate only (duplicate text/new-account bulk/pay phrases),
      dispute_open_for_deal binds reviews' gate; 29 tests
- [x] Prompt 24 — listing lifecycle: ACTIVE/PAUSED/SOLD/EXPIRED second axis,
      30-day injected-clock expiry sweep + one-tap renew, 7-day bump cap +
      bump-aware ordering, category-edit gate (UNDER_REVIEW) with moderator
      approve_recategorize + audit; 47 tests (574)
- [x] Web research: Digital Matatus GTFS, KDPA 2019/ODPC, bundle economics,
      SACCO tracking landscape, competitors (`docs/research-notes.md`)
- [x] Stack decision documented (`docs/adr/0001-stack-decision.md`)
- [x] Build contract written: `PROMPTS.md` — 42 testable prompts, 6 phases,
      one-commit-per-prompt, parent-owned shared files, mock-first adapters
- [x] Repo scaffolded on GitHub (private) with remote `origin` set

### In Progress
- [ ] Prompt 19 — listing geo search (ST_DWithin rings) — closes Phase 2

### Not Started
- [ ] Phase 3 (25–31), Phase 4 (32–34), Phase 5 (36–42)
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

> **Next:** Prompt 19 (Listing geo search: ST_DWithin 5/10/25 km rings around
> a ward, geography index asserted via EXPLAIN, promoted-slot flag invisible
> for now) — then Phase 3 Transport begins (25 GTFS seeder → 26 → 27 …).
> Suite at 574.

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