# MTAANI CONNECT — 42 Build Prompts

A single Kenyan super-app: transport intelligence, community knowledge, social
networking, and local commerce — matatu tracking, guides, marketplace, barter,
and messaging that work on the phones and data Kenyans already have.

This file is the build contract: 42 numbered, testable prompts organized into
6 phases, mapped one-to-one onto the product roadmap (docs/product-brief.md §7).
Each prompt is self-contained — a cold-start subagent can execute it with no
other context than this file, the in-repo product brief, and the files it
names. Build in phase order; within a phase, prompts are parallelizable unless
the dependency table says otherwise. One commit per prompt.

**Research grounding** (sources in docs/research-notes.md): the route/stage
database is seeded from the open Digital Matatus GTFS (135+ routes, ~3,000
Nairobi stops); payload budgets reflect real Kenyan bundle economics
(~KES 10 buys 70 MB daily on Airtel, ~KES 50–100 buys 1 GB); privacy prompts
implement the Kenya Data Protection Act 2019 (ODPC registration, logged
consent, data minimization, subject access/deletion rights).

---

## Hard Rules for Every Prompt

- **One agent per file.** Each prompt names the files it owns — no other agent
  touches them. Shared files (`mtaani/settings/*`, root `urls.py`,
  `templates/base.html`, `docker-compose.yml`, `apps/core/types.py`) are
  PARENT-OWNED: prompts list their integration needs under "Parent
  integration:", and the parent applies them sequentially after the prompt lands.
- **One Django app per prompt**, under `apps/<name>/` — the prompt owns that
  directory exclusively. Cross-app reads go through the other app's public
  query functions, never its internal tables.
- **Every module ships with its tests in the same prompt.** Acceptance means
  `pytest` green for the prompt's test files.
- **Mock mode first.** SMS, push notifications, LLM providers, and map tiles all
  get deterministic mock adapters behind the same interface as real backends.
  No prompt may require network access or paid API keys to pass its tests.
- **PostgreSQL 16 + PostGIS** via `docker compose` — never SQLite (geo queries
  need PostGIS). Every schema change is a migration. Store UTC (`USE_TZ=True`),
  display in `Africa/Nairobi`.
- **Mobile-first budgets, enforced by tests:** authenticated HTML pages ≤ 50 KB
  (excluding cached assets); compressed images ≤ 40 KB standard variant,
  avatars ≤ 15 KB; API list responses ≤ 20 items. Assert with
  `apps/core/testing.py::assert_under_budget`.
- **i18n from day one:** every user-facing string through `gettext`; `en` +
  `sw` locale dirs exist from Prompt 05. Store enum `.value`, never labels.
- **Privacy by design (KDPA 2019):** collect only what a feature needs; user
  location is ward/neighbourhood-coarse by default, precise location only with
  explicit consent; EXIF (incl. embedded GPS) stripped from every upload;
  consent events are logged with text version and timestamp; no phone numbers
  or precise coordinates in public fields or public API responses; live
  vehicle positions are public ONLY during active authorized tracking sessions.
- **No fabricated data.** Every transport location carries a
  `Confidence` source enum (`LIVE_GPS`, `VEHICLE_REPORT`, `COMMUNITY_REPORT`,
  `ESTIMATE`, `NO_DATA`). When data is missing, the UI says so — never invent
  a plausible position, ETA, or fare.
- **Human review gates:** account suspension, dispute resolution, and
  monetization actions are DRAFTED by the system and completed by a human
  moderator/admin. Nothing money- or safety-adjacent fires unattended.
- **Teaching notes:** `NOTE:` / `WHY:` comments so a non-coder collaborator can
  follow the logic.
- **Secrets in `.env`** (never committed); `.env.example` documents every key.
- **Repo hygiene:** one commit per prompt (`Prompt NN: <title>`); README and
  Project_state.md updated at phase boundaries.

---

## Phase Map (product brief §7)

| Phase | Prompts | Brief roadmap | Goal |
|-------|---------|---------------|------|
| 0 — Foundation | 01–07 | Phase 1 | Auth, profiles, design system, geo layer, PWA shell |
| 1 — Community | 08–16 | Phase 2 | Guides, posts, Q&A, feed, search, moderation |
| 2 — Commerce | 17–24 | Phase 3 | Listings, barter, messaging, reviews, disputes |
| 3 — Transport | 25–31 | Phase 4 | Route DB, authorized tracking, live map, alerts |
| 4 — Intelligence | 32–35 | Phase 5 | Route planning, AI adaptation, Kiswahili completion |
| 5 — Scale & Trust | 36–42 | Phase 6 | Abuse prevention, analytics, monetization, KDPA, deploy |

**Swap note (brief §7):** if user research shows transport is more urgent than
community, Phases 1 and 3 can be swapped. Their shared dependencies (02 types,
06 images, 15 moderation, 20 messaging) have no community-specific logic; pull
them forward unchanged.

---

## Phase 0 — Foundation

| #  | Module | Files | Depends On |
|----|--------|-------|------------|
| 01 | Repo scaffold & dev environment | project root, `mtaani/` | — |
| 02 | Core types, enums & budget utils | `apps/core/types.py`, `apps/core/testing.py` | 01 |
| 03 | Settings, PostGIS & geo utilities | `mtaani/settings/`, `apps/core/geo.py` | 01 |
| 04 | Auth, verification & recovery | `apps/accounts/`, `apps/core/comms.py` | 02, 03 |
| 05 | Design system, i18n shell & PWA | `templates/`, `static/`, `apps/core/templatetags/` | 03 |
| 06 | Image compression pipeline | `apps/core/images.py`, `apps/media/` | 03 |
| 07 | Profiles & privacy controls | `apps/profiles/` | 04, 06 |

### Prompt 01 — Repo Scaffold & Dev Environment
**Build:** `pyproject.toml`, `docker-compose.yml`, `Makefile`, `.env.example`,
`mtaani/` (settings package, root `urls.py`, `wsgi.py`), `apps/core/__init__.py`,
`apps/accounts/__init__.py` + `apps/accounts/models.py` (User skeleton ONLY),
`conftest.py`, `tests/test_smoke.py`
**Depends on:** —

Django 6 project `mtaani`, dependency groups `[dev]` = pytest, pytest-django.
`docker-compose.yml`: `postgis/postgis:16` (db `mtaani`, creds via env) + app
service stub. `Makefile`: `up`, `down`, `test` (runs `pytest -q` through the
project venv — create `.venv/` and install deps; never use the system
interpreter). Settings read from `.env` (django-environ), `DEBUG` off in tests.
The `accounts` app contains ONLY a custom `AbstractUser` subclass
(`AUTH_USER_MODEL="accounts.User"`) plus `phone` (nullable, E.164) — nothing
else. **WHY (critical pitfall):** the custom user model must exist before the
first `migrate`; swapping `AUTH_USER_MODEL` after the auth tables exist is
effectively unrecoverable. Prompt 04 owns every other accounts file.
Smoke test: fresh DB `migrate` + `manage.py check` + client GET `/` returns 200
from a placeholder template. **Acceptance:** `make up` healthy, `make test`
green on a fresh clone with only `cp .env.example .env` required.

---

### Prompt 02 — Core Types, Enums & Budget Utils
**Build:** `apps/core/types.py`, `apps/core/testing.py` + `tests/test_types.py`
**Depends on:** 01

The shared vocabulary every app imports: `TextChoices` enums `Confidence`
(LIVE_GPS, VEHICLE_REPORT, COMMUNITY_REPORT, ESTIMATE, NO_DATA),
`LocationVisibility` (WARD, NEIGHBOURHOOD, CITY), `BarterMode` (CASH, BARTER,
CASH_PLUS), `OfferStatus` (DRAFT, OFFERED, COUNTERED, ACCEPTED, COMPLETED,
CANCELLED, EXPIRED), `ReportReason` (SPAM, SCAM, HARASSMENT, MISINFORMATION,
OFF_TOPIC, SAFETY), `SafetyCategory` (MEDICAL, ELECTRICAL, CONSTRUCTION, NONE),
`ContentStatus` (DRAFT, PUBLISHED, UNDER_REVIEW, REMOVED), `ListingKind`
(GOODS, SERVICE), `GroupKind` (INTEREST, NEIGHBOURHOOD, PROFESSION). Abstract
`TimestampedModel` (created/updated UTC). `testing.py`: pytest fixtures
(`fresh_user`, `client_auth`), `assert_under_budget(html, kb)` and
`mock_now()` helpers. **Pitfall:** never persist enum labels — translations
change; store `.value`. **Pitfall:** enum class docstrings in plain English;
labels may use gettext lazily but tests must compare against `.value`.
Tests: every enum round-trips value→member→value; `assert_under_budget`
correctly fails oversized HTML and passes a small page.
**Acceptance:** `pytest tests/test_types.py` green; later apps can import
these with zero circular imports.

---

### Prompt 03 — Settings, PostGIS & Geo Utilities
**Build:** `mtaani/settings/base.py|dev.py|test.py` (completing 01's stub),
`apps/core/geo.py`, `apps/core/migrations/0001_initial.py` + `tests/test_geo.py`
**Depends on:** 01

`geo.py`: pure helpers over PostGIS — `distance_km(a, b)`,
`within_radius(point, km)` (uses `ST_DWithin` on `geography`, meters),
`coarsen(point)` (snaps coordinates to a ~2 km grid — the ONLY location
granularity ever exposed for users without explicit precise-location consent),
`ward_label(point)` (maps to a seeded ward name list — structure, not a full
Kenya gazetteer; admin can extend). A migration enables the `postgis`
extension. Settings: `USE_TZ=True`, `TIME_DISPLAY="Africa/Nairobi"`, DB engine
check fails fast with a readable message if PostGIS is missing.
**Pitfall:** `geometry` vs `geography` — use `geography` for all
distance-in-meters queries; `geometry` silently returns degree units.
Tests: PostGIS available (skip with a loud message if extension absent),
distance math against known Nairobi coordinate pairs (e.g., CBD↔Westlands),
`coarsen` never returns sub-km precision, radius query returns exactly the
fixture points inside. **Acceptance:** geo suite green against the Docker DB.

---

### Prompt 04 — Auth, Verification & Account Recovery
**Build:** `apps/accounts/` (views, services, forms, templates, remaining
models), `apps/core/comms.py` + `tests/test_accounts.py`, `tests/test_comms.py`
**Depends on:** 02, 03

Completes the accounts app: signup (email required, phone optional), email
verification, phone verification via OTP, account recovery. `core/comms.py`
defines `SMSAdapter` / `EmailAdapter` interfaces with `MockSMS`
(deterministic: code = f(phone, date) hash, so tests need no network) and
`MockEmail`; real Africa's Talking adapter is a documented stub — never
implemented in V1 prompts. OTPs: hashed at rest, single-use, 10-min expiry,
rate-limited send (3/hour). Login throttle: 5 failures → 15-minute lock
per user+IP. Recovery tokens: hashed, single-use, 1-hour expiry. KDPA: a
`ConsentEvent` model (user, purpose key, document version, timestamp, source
IP) records signup consent to Terms + Privacy — the controller bears the
burden of proof (DPA §32), so consent must be reconstructable.
**Pitfall:** never log OTPs, tokens, or password hashes. **Pitfall:** phone
stored E.164; verify format at the form layer before anything else touches it.
Tests: OTP determinism, single-use enforcement, lockout math, recovery flow,
consent events created exactly once per signup, no OTP in any log capture.
**Acceptance:** signup→verify→login→recover round-trip green end-to-end
with mocks only.

---

### Prompt 05 — Design System, i18n Shell & PWA Skeleton
**Build:** `templates/base.html`, `templates/offline.html`,
`static/css/mtaani.css`, `static/js/app.js`, `static/sw.js`,
`static/manifest.webmanifest`, `locale/en/`, `locale/sw/`,
`apps/core/templatetags/mtaani.py` + `tests/test_design_system.py`
**Depends on:** 03

Mobile-first base template: bottom tab-bar nav (Home, Guides, Market,
Chats, Transport), `mtaani.css` as one hand-written sheet (~15 KB budget — no
framework), progressive-enhancement `app.js` (offline indicator via
`navigator.onLine` + fetch-fail listener, lazy-load images via
`loading="lazy"`), language toggle (en/sw) persisted per user. PWA: manifest +
service worker caching static assets with explicit cache-version constant;
`offline.html` served from SW fallback on failed navigations. Kiswahili
locale dir ships with nav + core UI strings translated; the rest accrue over
phases (completed in Prompt 35).
**Pitfall:** SW cache busting — bump the cache-name constant on every static
asset change or users see stale pages for days. **Pitfall:** never cache
POSTs or authenticated API JSON in the SW.
Tests: base template renders under 50 KB with realistic nav, manifest served
200, SW registers (moto: fetch-fallback test via mocked fetch), offline page
reachable, all nav strings present in both locale files.
**Acceptance:** the app is installable on low-end Android Chrome with EN/SW
toggle working.

---

### Prompt 06 — Image Compression Pipeline (shared service)
**Build:** `apps/core/images.py`, `apps/media/` (upload handling app) +
`tests/test_images.py`
**Depends on:** 03

One service every image feature calls (profiles, posts, listings, messaging,
guides): `save_upload(user_file, owner, purpose)` → validates
(Pillow `Image.verify`), **strips ALL EXIF (embedded GPS is a privacy leak —
KDPA)**, converts to WebP, emits `standard` (≤1280 px, ≤40 KB) and `thumb`
(≤320 px, ≤15 KB) variants, stores under `MEDIA_ROOT/images/<purpose>/<uuid>.webp`,
returns a `MediaRef` (uuid, purpose, variants, bytes, dims). Never trust
client-supplied dimensions or filenames — UUID names only. Upload endpoint
accepts one image ≤ 10 MB in, and rejects anything Pillow can't open with a
friendly bilingual error.
**Pitfall:** re-compress an already-compressed WebP is fine, but always
re-strip EXIF — transcoding preserves metadata in some Pillow versions.
Tests: EXIF/GPS stripped (assert metadata empty on output), size budgets
enforced for a large fixture photo, corrupt upload rejected, variant
dimensions honored, media refs are per-owner.
**Acceptance:** every later image feature has one well-tested path in.

---

### Prompt 07 — Profiles & Privacy Controls
**Build:** `apps/profiles/` + `tests/test_profiles.py`
**Depends on:** 04, 06

Profile model: display name, bio, skills (tag list), interests, ward-level
location (`coarsen`-grid or ward name — never raw coordinates), optional
avatar via Prompt 06, `LocationVisibility` setting, "verified seller" flag
(default off — optional verification per brief §5, NOT mandatory). Settings
page: profile visibility per field group (public / members-only / private),
precise-location consent is a SEPARATE explicit toggle that nothing in V1
actually requires (transport passengers never share GPS in V1).
**Pitfall:** public profile API serializer must be allow-list based — a
serializer that dumps the model leaks private fields as they're added later.
Tests: allow-list serializer never includes phone/email/precise location even
after new fields are added to the model, visibility settings enforced on
rendered profile pages, avatar flows through the image service.
**Acceptance:** profiles visible at ward-level granularity only; privacy
controls testable and enforced.

---

## Phase 1 — Community

| #  | Module | Files | Depends On |
|----|--------|-------|------------|
| 08 | Guides CRUD & safety warnings | `apps/guides/` | 02, 07 |
| 09 | Offline saved guides | `apps/guides/offline.py`, `static/sw.js` (parent) | 08 |
| 10 | Posts & photos | `apps/posts/` | 06, 07 |
| 11 | Utility feed ranking | `apps/feed/` | 10, 13 |
| 12 | Q&A with AI-answer labeling | `apps/qa/` | 08 |
| 13 | Follows & groups | `apps/follows/`, `apps/groups/` | 07 |
| 14 | Cross-entity search | `apps/search/` | 08, 10, 12 |
| 15 | Moderation v1 | `apps/moderation/` | 02, 07 |
| 16 | Trust signals & helpfulness | `apps/trust/` | 10, 12 |

### Prompt 08 — Guides: Local Knowledge (WikiHow for Kenya)
**Build:** `apps/guides/` + `tests/test_guides.py`
**Depends on:** 02, 07

Guide model: title (en + optional sw), category (cooking, farming, repairs,
business, health-education, crafts, transport — seedable list, admin-extendable),
skill level (beginner/intermediate/advanced), ordered `GuideStep`s (text,
optional image via Prompt 06), `SafetyCategory` flag. Guides with
MEDICAL/ELECTRICAL/CONSTRUCTION safety flags MUST carry a `safety_warning`
text — enforce in `clean()`, not just the form. Versioning: edits create a new
`GuideRevision`; published view shows latest approved revision. Community
verification: "This worked for me in [ward]" confirmations.
**Pitfall:** never render user HTML — step text is plain text + line breaks;
any rich formatting comes from a fixed allow-list of template-side transforms.
Tests: CRUD round-trip, revision history, safety-warning enforcement, category
filtering, i18n title fallback (sw missing → en shown + labeled).
**Acceptance:** a guide with steps, safety warning, and confirmations is
creatable, searchable by category, and revision-safe.

---

### Prompt 09 — Offline Saved Guides
**Build:** `apps/guides/offline.py`, `apps/guides/templates/guides/offline.html`
+ `tests/test_offline_guides.py` (sw.js changes listed under Parent integration)
**Depends on:** 08

"Save for offline" adds the guide to the user's offline list; a
`/guides/offline-manifest.json` endpoint lists saved guides with revision
hashes; the service worker (parent edits, spec'd here) caches the manifest
and guide pages on save, and re-syncs only when a revision hash changes.
Storage quota: warn at 50 MB used. The offline list page works with zero
connectivity (it is itself cached).
**Pitfall:** revision-hash comparison, not date comparison — clocks lie and
re-downloading identical content burns bundles.
Tests: manifest lists saved guides with correct hashes, save/unsave reflected,
stale detection triggers refetch only on hash change, offline list page
renders from cache in a simulated offline fetch.
**Acceptance:** a saved guide opens in airplane mode with steps and images.

---

### Prompt 10 — Posts & Photos
**Build:** `apps/posts/` + `tests/test_posts.py`
**Depends on:** 06, 07

Posts: text (≤2,000 chars), up to 4 images via the image service, optional
link to a guide or listing (typed references, not raw URLs — preview cards
render server-side). Educational content tag. Word-of-mouth culture support:
short-form posts must feel first-class, not truncated long-form.
**Pitfall:** image count limit enforced in model `clean()` AND the upload
endpoint — client-side checks are decoration.
Tests: post CRUD, 4-image cap, reference previews render, feed-item page
under 50 KB with 3 posts, educational tag filterable.
**Acceptance:** posts with compressed photos render mobile-fast.

---

### Prompt 11 — Utility Feed Ranking (not engagement bait)
**Build:** `apps/feed/` + `tests/test_feed.py`
**Depends on:** 10, 13

The utility-first feed (brief §3 Module 4): score = followed-authors weight +
local relevance (same ward/neighbourhood boost) + usefulness signals
(helpful-vote ratio, guide/Q&A linkage) — with a deterministic, unit-testable
`rank()` pure function. NO infinite-scroll bottomless pit: paginated pages of
10 with an explicit "end of feed" state. Recency applies WITHIN tiers, never as
the primary sort — a genuinely useful local post from yesterday beats rage-bait
from 5 minutes ago. Feed assembly is a read-only view over posts/follows —
no denormalized state to drift.
**Pitfall:** ranking functions with `now()` inside them are untestable —
inject the clock.
Tests: rank ordering for 6 hand-built scenarios (local-useful beats
recent-unrelated, followed beats stranger at equal usefulness, etc.), page
size and end-of-feed marker, injected-clock determinism, budget compliance.
**Acceptance:** feed ordering is fully explainable from the written scoring
table in the module docstring.

---

### Prompt 12 — Q&A with AI-Answer Labeling
**Build:** `apps/qa/` + `tests/test_qa.py`
**Depends on:** 08

Questions (en/sw), multiple answers, asker-accepted answer, community
verification votes per answer. Any machine-generated answer MUST render with
an "AI answer — verify before relying on it" badge; the flag is set by the
AI layer (Prompt 33) and is not editable by users. Safety categories mirror
guides: medical/electrical answers carry the same warnings and can never be
the accepted answer without a human-verified community answer alongside.
**Pitfall:** AI badge must be data-driven (boolean on the row), not template
convention — future surfaces must not be able to forget it.
Tests: accept-flow, verification votes, AI-badge persistence through any
serializer, safety gating on flagged categories.
**Acceptance:** Q&A threads with accepted + verified answers render with
correct provenance labels.

---

### Prompt 13 — Follows & Groups
**Build:** `apps/follows/`, `apps/groups/` + `tests/test_follows.py`,
`tests/test_groups.py`
**Depends on:** 07

Follow users (asymmetric, no approval); groups by interest / neighbourhood /
profession (`GroupKind`): membership (join/leave), group posts surface via
the feed's local-relevance tier, group owner + moderator roles reuse the
moderation role hooks from `accounts`. No follower counts shown publicly
below 10 (chills pile-on culture and makes bought-follower counts pointless).
Tests: follow/unfollow idempotency, group membership lifecycle, role
permissions on group content, feed tier integration (via Prompt 11's
interface, mocked if 11 not yet built).
**Acceptance:** groups work standalone; feed integration is one function call.

---

### Prompt 14 — Cross-Entity Search
**Build:** `apps/search/` + `tests/test_search.py`
**Depends on:** 08, 10, 12

One search box across guides, posts, Q&A, and people: Postgres full-text
(`SearchVector` on a maintained column, not computed per query) + `pg_trgm`
for typo tolerance ("sukuma wiki" finds "sukumawiki"). Bilingual matching:
search `sw` fields alongside `en`. Results grouped by entity type with counts;
each group links to its entity's own filtered list.
**Pitfall:** maintain the `SearchVector` in a migration/trigger or on-save
hook — computing it inline means a table scan per keystroke.
Tests: bilingual match, trigram typo tolerance, grouping counts, search
page under budget with 20-result fixture, empty-state copy in both languages.
**Acceptance:** search works for Kiswahili terms with typos, cheaply.

---

### Prompt 15 — Moderation v1 (report, block, queue)
**Build:** `apps/moderation/` + `tests/test_moderation.py`
**Depends on:** 02, 07

Report anything (posts, guides, Q&A, listings, profiles, messages) with
`ReportReason`; block users (hides their content app-wide for the blocker,
revocable); moderation queue with role-gated tools (moderator/admin roles on
`accounts`). Every moderation action writes an immutable audit row (actor,
action, target, reason, timestamp). Removals set `ContentStatus.REMOVED` —
content is never hard-deleted by moderators (evidence retention for disputes).
**Human review gate:** suspending an account or resolving a report is a
two-step action (draft → confirm by a different human role where possible).
**Pitfall:** the report endpoint must be rate-limited (abuse of the abuse
system is real) — 10 reports/hour/user.
Tests: report lifecycle, blocking visibility effects, role gates, audit
completeness, rate limit, REMOVED content excluded from feeds/search but
preserved in DB.
**Acceptance:** a reported post flows to queue → moderator action → audit
row, with roles enforced.

---

### Prompt 16 — Trust Signals & Helpfulness
**Build:** `apps/trust/` + `tests/test_trust.py`
**Depends on:** 10, 12

Helpful votes on posts/answers (one per user per item, revocable), per-user
helpfulness rollup (lifetime + last-90-days), community expert tags earned by
category helpfulness thresholds (badge levels: rising/known/trusted —
structure with admin-tunable thresholds, no hardcoded persona content).
Trust signals display on profiles and next to answers; they feed Prompt 11's
ranking and later marketplace/barter reputation (Prompt 22).
Tests: vote idempotency/revocation, rollup math, threshold crossing emits a
badge event, tie-breaking determinism.
**Acceptance:** a helpful user visibly accumulates category trust that other
modules can consume.

---

## Phase 2 — Commerce

| #  | Module | Files | Depends On |
|----|--------|-------|------------|
| 17 | Marketplace listings (goods) | `apps/listings/` | 02, 06, 07 |
| 18 | Service listings & portfolios | `apps/services/` | 17 |
| 19 | Listing search & geo filters | `apps/listings/search.py` | 17, 14 |
| 20 | Messaging v1 | `apps/messaging/` | 04, 15 |
| 21 | Barter offers & negotiation | `apps/barter/` | 17, 20 |
| 22 | Reviews & reputation | `apps/reviews/` | 17, 16 |
| 23 | Disputes & fraud flags | `apps/disputes/` | 15, 21 |
| 24 | Listing lifecycle | `apps/listings/lifecycle.py` | 17 |

### Prompt 17 — Marketplace Listings (Goods)
**Build:** `apps/listings/` + `tests/test_listings.py`
**Depends on:** 02, 06, 07

Goods listings: kind (new, second-hand, handmade, farm produce, electronics,
furniture, tools — seedable category list), title/description (en + optional
sw), price or "negotiable", condition grade, up to 6 images via the image
service, location = ward/neighborhood ONLY (never exact address — public),
contact happens through in-app messaging (Prompt 20), never exposed phone
numbers. Listing status reuses `ContentStatus`.
**Pitfall:** phone-number-in-description is the #1 scam vector — add a
detector that flags `07xx`/`+2547xx` patterns in public text fields for
moderator review (not auto-block: people legitimately discuss phone-shaped
things; queue it).
Tests: CRUD, category filters, budget compliance with 6 images, phone-pattern
flagging, ward-level location exposure.
**Acceptance:** listings render fast with images and never leak precise
location or contact info.

---

### Prompt 18 — Service Listings & Portfolios
**Build:** `apps/services/` + `tests/test_services.py`
**Depends on:** 17

Skills marketplace (brief §3 Module 5): tutoring, repairs, coding,
tailoring, catering, photography, farming services — service listing model
separate from goods (rate type: hourly/fixed/negotiable; availability notes;
sample/portfolio images via the image service). Reuses listings categories
where sensible via `ListingKind.SERVICE`.
Tests: service CRUD distinct from goods, rate-type validation, portfolio
image caps, cross-linking to reviews (Prompt 22 interface mocked).
**Acceptance:** a service provider profile shows portfolio + rates without
goods-listing noise.

---

### Prompt 19 — Listing Search & Geo Filters
**Build:** `apps/listings/search.py` + `tests/test_listing_search.py`
**Depends on:** 17, 14

PostGIS-backed discovery: filter by category, price band, condition, and
distance rings around a ward/neighborhood (5/10/25 km via
`ST_DWithin` on geography — user picks an area, never shares their own
position). Sorted by recency within relevance; promoted listings (Prompt 38)
will slot in later behind an explicit flag, invisible for now.
**Pitfall:** distance filters need an index on the geography column — add it
in the migration or every search is a sequential scan.
Tests: distance-ring correctness against synthetic fixture coordinates,
filter combinations, index present (assert via EXPLAIN plan type), budget
compliance on result pages.
**Acceptance:** "tailoring within 10 km of Kawangware" returns the right
fixtures, fast.

---

### Prompt 20 — Messaging v1
**Build:** `apps/messaging/` + `tests/test_messaging.py`
**Depends on:** 04, 15

Threads: 1-to-1, marketplace-enquiry origin (thread carries a typed reference
to the listing), later barter-negotiation origin (Prompt 21). Text + photos
(image service, ≤4 per message), read receipts (opt-out per user), unread
counts, spam controls (report message → Prompt 15 queue; auto-mute thread on
report). Phone-number sharing through messages is allowed ONLY as explicit
user choice — but the phone-pattern detector (17) still flags it in
moderation analytics for scam-pattern tracking.
**Pitfall:** thread access control — every message read/write must verify
thread membership; guessing thread IDs is the classic IDOR hole.
**Pitfall:** E2E encryption is out of scope for V1 (evaluated later per brief);
document this honestly in the privacy policy text, don't imply more than TLS.
Tests: membership enforcement (IDOR attempts fail), read receipts with
opt-out, origin-reference integrity, unread counts, image caps, report flow.
**Acceptance:** buyer→seller enquiry thread round-trips with no contact info
exposure and no IDOR.

---

### Prompt 21 — Barter & Cash-Plus-Exchange (key differentiator)
**Build:** `apps/barter/` + `tests/test_barter.py`
**Depends on:** 17, 20

Structured barter per brief §3 Module 6: offer model with `BarterMode`
(CASH / BARTER / CASH_PLUS), multi-item sides ("I give: 2kg maize + sewing
hour; I want: solar lamp + KES 300"), counteroffers as versioned offer
threads (each counter links to its parent; lineage walks to the root), and a
STRICT state machine: DRAFT→OFFERED→COUNTERED→ACCEPTED→COMPLETED, with
CANCELLED/EXPIRED terminal states. State transitions validated in the model —
no view-level state edits. Offers expire after 7 days of inactivity
(configurable). The app NEVER holds money or items (brief: no custody, no
escrow in V1); the "cash component" is a stated amount the parties settle
via M-Pesa themselves — the UI says so plainly. M-Pesa integration is a
documented future extension point (adapter interface stub, never built here).
**Pitfall:** the state machine is the product — test EVERY legal transition
and every illegal one (esp. COUNTERED after ACCEPTED, edits to ACCEPTED
offers, double-accept).
Tests: full happy path with 2 counters, every illegal transition rejected,
lineage depth, expiry sweep job, multi-item math (each side's items
independently editable in DRAFT only).
**Acceptance:** two users can negotiate a mixed cash-plus-barter swap
entirely in-app with a clean audit trail.

---

### Prompt 22 — Reviews & Reputation
**Build:** `apps/reviews/` + `tests/test_reviews.py`
**Depends on:** 17, 16

Mutual reviews after a completed barter/marketplace deal (both sides review;
reviews publish simultaneously when both submitted or after 14 days — no
retaliation window), listing-level reviews for goods/services, per-user
reputation rollup combining trust signals (16) and deal reviews. Review
content: 1–5 stars + text; a 1-star review REQUIREs a report reason when the
deal has a dispute open (connects to 23).
**Pitfall:** publish-both-at-once requires a two-phase publish job — write it
as a sweep, not a trigger.
Tests: two-phase publish, unilateral publish after 14 days, rollup math,
star + text validation, dispute-linked review gating.
**Acceptance:** reputation is symmetric, non-retaliatory, and feeds profile
display.

---

### Prompt 23 — Disputes & Fraud Prevention
**Build:** `apps/disputes/` + `tests/test_disputes.py`
**Depends on:** 15, 21

Dispute case per deal (barter or sale): structured claim (what was promised /
what happened), evidence attachments (messages auto-linked from the
negotiation thread, images), resolution workflow with HUMAN REVIEW GATE:
system proposes outcomes (warn / suspend / remove listing / mark review), a
human admin confirms. Fraud heuristics run as flags, never auto-actions:
duplicate listing text across accounts, new-account bulk listings, off-platform
payment-request phrases ("send M-Pesa to…"). Timeouts and case aging:
unresolved cases escalate in the admin dashboard, never silently close.
**Pitfall:** auto-suspending on heuristics will ban legitimate farmers selling
similar produce — heuristics ONLY annotate; humans decide.
Tests: case lifecycle, evidence linkage, heuristic flagging on fixture
patterns (and no false flag on a benign duplicate-title case), admin confirm
flow, aging/escalation.
**Acceptance:** a fake-deal dispute reaches a human decision with evidence
intact.

---

### Prompt 24 — Listing Lifecycle
**Build:** `apps/listings/lifecycle.py` + `tests/test_lifecycle.py`
**Depends on:** 17

Statuses: ACTIVE → PAUSED / SOLD / EXPIRED; auto-expiry after 30 days with
one-tap renew (identical content, fresh timestamp — no re-typing), bump
frequency capped (once per 7 days — utility-first means no pay-to-shout),
edit rules: price/description edits allowed while ACTIVE; category edits
require moderator re-approval (bait-and-switch prevention).
Tests: transition matrix, expiry sweep, renewal semantics, bump caps,
category-edit gate.
**Acceptance:** listings age, expire, and renew without moderator load.

---

## Phase 3 — Transport Pilot (flagship)

| #  | Module | Files | Depends On |
|----|--------|-------|------------|
| 25 | Route & stage database + GTFS seeder | `apps/routes/` | 02, 03 |
| 26 | Vehicle, SACCO & crew registration | `apps/routes/crew.py`, `apps/fleet/` | 25, 04 |
| 27 | Authorized tracking sessions | `apps/tracking/` | 26 |
| 28 | Location ingestion & live map | `apps/tracking/ingest.py`, `apps/map/` | 27 |
| 29 | Driver/conductor low-data flow | `apps/crew/` | 27 |
| 30 | Passenger crowd reports | `apps/crowd/` | 25 |
| 31 | Arrival estimates & follow alerts | `apps/alerts/` | 28, 13 |

### Prompt 25 — Route & Stage Database + GTFS Seeder
**Build:** `apps/routes/` + `tests/test_routes.py`
**Depends on:** 02, 03

Models: `Route` (name en/sw, SACCO(s), corridor color), `Stage` (name, aliases
— "Ambassadeur" also matches "Ambassador"; PostGIS point, continuous-stop
awareness flag), `RouteStage` (ordered sequence), `Fare` (typical range with
`Confidence` — fares are variable in reality; store observed ranges, never
a single "official" number), `Sacco` (name, contact, registration status).
GTFS seeder: import a Digital Matatus-format GTFS zip (routes.txt,
stops.txt, shapes.txt — modified Nairobi GTFS; probe the actual file at load
time, don't assume standard fields). **The 2018 GTFS is STALE — treat it as
seed data**: every seeded route/stage is editable in admin, and a community
correction queue (report wrong stage position / missing route) feeds
moderators. If the zip is absent, seeder degrades: `available() → False`,
manual admin entry still works. Search by nickname: stage/route alias table
with trigram matching.
**Pitfall:** never fabricate missing shapes — a route without shape data
renders as stage-to-stage polyline, labeled as such.
Tests: GTFS fixture import (small synthetic zip), alias/nickname search
("Manyanga"-style queries match aliased entries), fare range display,
graceful no-zip degradation, correction-queue flow.
**Acceptance:** Nairobi route network searchable by nickname/SACCO/destination
from seed data, honestly labeled as seed-quality.

---

### Prompt 26 — Vehicle, SACCO & Crew Registration
**Build:** `apps/fleet/` + `tests/test_fleet.py`
**Depends on:** 25, 04

Vehicle registry: plate (format-validated), nickname (the beloved matatu
names — searchable), route assignment (many vehicles ↔ routes), capacity,
SACCO ownership. Crew: driver/conductor `accounts` users linked to a vehicle
with a crew role, onboarding requiring SACCO admin approval (human gate —
nobody broadcasts position authority without an operator saying yes).
Operator dashboard scaffolding: SACCO admin sees THEIR vehicles only
(multi-tenant isolation at the query layer, tested).
**Pitfall:** crew accounts must not inherit ordinary-user moderation blind
spots — a crew role adds authority, never removes accountability.
Tests: plate validation, crew approval gate, tenant isolation (SACCO A admin
cannot list SACCO B vehicles), nickname search.
**Acceptance:** a SACCO can register vehicles and crew with enforced
isolation.

---

### Prompt 27 — Authorized Tracking Sessions
**Build:** `apps/tracking/` (session model, authorization, privacy guardrails)
+ `tests/test_tracking_sessions.py`
**Depends on:** 26

The privacy core of the flagship (brief §5, non-negotiable): a
`TrackingSession` = (vehicle, authorized by SACCO/crew, starts, ends, token).
Rules IN CODE, not policy docs: (1) positions are queryable ONLY while the
session is active; (2) session auto-expires (crew forgets to stop → stops
publishing — default 12 h cap, configurable shorter); (3) pings are pruned
after 72 h; (4) the public map can NEVER reconstruct driver off-trip movement:
first/last ping of a session suppressed within a 1 km privacy radius of any
session boundary; (5) session tokens are scoped, rotated, and never expose
crew identity. DPIA note in code comments: live location is high-risk
processing under the DPA — these guardrails are the mitigation.
**Pitfall:** suppression rules belong in the query layer, not the template —
any future API consumer inherits them automatically.
Tests: active-session-only visibility, auto-expiry, ping pruning job,
boundary suppression cases, token scoping, crew identity absence from all
public serializations.
**Acceptance:** a tracking session is visible while authorized and utterly
invisible after — provably, from tests.

---

### Prompt 28 — Location Ingestion & Live Map
**Build:** `apps/tracking/ingest.py`, `apps/map/` + `tests/test_ingest.py`,
`tests/test_map.py`
**Depends on:** 27

Ingestion API: batched ping upload (≤50 pings per request — crew devices
flush every 30 s–2 min, not per-fix: battery + data), session-token auth,
dedupe by (session, timestamp), plausibility checks (speed > 150 km/h →
`Confidence` downgrade, not deletion). Map view: Leaflet with OSM raster
tiles (no Google dependency in V1), vehicle markers showing `Confidence`
visibly (filled dot = LIVE_GPS, hollow = report, dashed ring = estimate), and
an explicit "no live data for this route" empty state — never a spun-up
placeholder marker.
**Pitfall:** tile choice matters on 3G — lazy-load tiles, cap zoom levels,
and cache them in the SW; a map that burns 2 MB of tiles has failed the brief.
Tests: batch ingest, dedupe, plausibility downgrade, map API serializer
carries Confidence, empty-state rendering, tile budget stub.
**Acceptance:** live positions flow batch→map with honest confidence labels
and a truthful no-data state.

---

### Prompt 29 — Driver/Conductor Low-Data Flow
**Build:** `apps/crew/` + `tests/test_crew_flow.py`
**Depends on:** 27

The crew-facing experience (the brief's key assumption to validate: crew
will share location IF battery/data cost is low): a single lightweight page
(start trip → pick route/direction → live "publishing" indicator with
data-usage counter → end trip). Geolocation API with `maximumAge` tuned for
coarse efficiency, batch flush (28's API), offline queue (pings buffer in
localStorage and flush when connectivity returns), visible battery/data
impact so crew stays in control. No heavy JS: this page must render < 20 KB.
**Pitfall:** geolocation on Android Chrome kills battery with
`enableHighAccuracy` always-on — poll at intervals, degrade gracefully.
Tests: flush/queue cycle with mocked geolocation, data-counter math, page
budget, offline buffer integrity, session start/end calls 27's API correctly.
**Acceptance:** a crew phone publishes a trip on a simulated 2G crawl, cheaply.

---

### Prompt 30 — Passenger Crowd Reports
**Build:** `apps/crowd/` + `tests/test_crowd.py`
**Depends on:** 25

Crowd-sourced transport truth: reports typed to a route/stage (delay,
rerouting, stage closure, flooding, accident, "matatu here now"),
`Confidence.COMMUNITY_REPORT`, weighted by reporter trust (16) and recency
(decay after 45 min), duplicate suppression (same type+stage within 10 min
merges with a count bump), and honest display ("3 riders report flooding on
Ruiru Rd"). Reports never emit synthetic positions — they are events at
known stages, never "the matatu is at X" fabrications.
**Pitfall:** weighting must be pure + clock-injected (testable), same as 11.
Tests: dedupe/merge, decay ordering, trust weighting, type validation, feed
integration into map overlays (via 28's serializer interface, mocked).
**Acceptance:** three riders reporting the same delay become ONE honest,
attributed, useful signal.

---

### Prompt 31 — Arrival Estimates & Follow Alerts
**Build:** `apps/alerts/` + `tests/test_alerts.py`
**Depends on:** 28, 13

Follow a matatu (by nickname) or a route; "approaching stage" alerts when a
followed vehicle's live ping is within `configurable radius` (default 2 km)
of the user's chosen stage. ETA v1 is honest arithmetic, not ML: rolling
speed from recent pings × remaining route distance, labeled
`Confidence.ESTIMATE`, with a visible range ("5–12 min") and silence when
history is insufficient ("not enough live data to estimate"). Notification
service: `NotifyAdapter` interface + `MockPush` (deterministic), Web Push as a
documented later adapter; notifications opt-in per followed route and
mutable in one tap.
**Pitfall:** never let an estimate render without its ESTIMATE label; never
let a missing estimate render as infinity or 0.
Tests: radius trigger math, ETA range computation from fixture pings,
insufficient-data silence, follow CRUD, MockPush determinism, opt-out flow.
**Acceptance:** "my matatu is coming" works — or honestly says it can't tell
yet.

---

## Phase 4 — Intelligence

| #  | Module | Files | Depends On |
|----|--------|-------|------------|
| 32 | Traffic & route planning | `apps/traffic/` | 25, 30 |
| 33 | AI abstraction layer | `apps/ai/` | 02 |
| 34 | Local-adaptation assistant | `apps/ai/assistant.py` | 33, 08 |
| 35 | Multilingual completion | `locale/`, `apps/core/i18n.py` | 05, all |

### Prompt 32 — Traffic & Route Planning
**Build:** `apps/traffic/` + `tests/test_traffic.py`
**Depends on:** 25, 30

Journey planning estate→estate over the stage graph (Dijkstra over
`RouteStage` edges; transfer penalties at shared stages; walking legs
labeled with realistic minutes). Alternates during disruption: crowd
reports (30) annotate edges with delay weights and the planner surfaces
"usual route + reported flooding; alternative via X". Source separation is
visual: official/seeded route data vs community-reported conditions — every
disruption line cites its reports. Fare ranges shown per leg (from 25).
**Pitfall:** the graph is small (hundreds of stages) — no need for external
routing engines; keep it pure Python and testable.
Tests: shortest path fixtures, transfer penalty effect, disruption rerouting,
fare aggregation, planner handles disconnected graphs without crashing.
**Acceptance:** "Kawangware → CBD" plans with honest alternatives and fares.

---

### Prompt 33 — AI Abstraction Layer
**Build:** `apps/ai/` + `tests/test_ai_layer.py`
**Depends on:** 02

Provider-agnostic AI access per brief §6: `LLMProvider` interface
(`complete(system, messages, max_tokens) -> LLMResult`), `MockLLM`
(deterministic, fixture-keyed for tests), adapter registry with
documented extension points for cloud APIs and local models (Ollama on the
Jetson qualifies as local), per-call cost logging (tokens in/out, provider,
model) with a daily budget guard that degrades to "AI temporarily
unavailable" rather than overspending. Prompts live in versioned files, not
inline strings. AI outputs NEVER auto-publish: every surface that shows AI
content requires the labeling contract from Prompt 12.
**Pitfall:** no module may import an SDK directly — everything through the
interface, or the abstraction is fake.
Tests: MockLLM determinism, registry selection, cost ledger accrual, budget
tripwire, prompt-version pinning.
**Acceptance:** swapping AI providers changes config, not code.

---

### Prompt 34 — Local-Adaptation Assistant (AI for local reality)
**Build:** `apps/ai/assistant.py` + `tests/test_assistant.py`
**Depends on:** 33, 08

The brief's "AI that adapts to locally available resources": a guided intake
("What do you have? What can you get at the duka?") → the assistant adapts
guide steps with substitutions grounded in the guides corpus (retrieval over
08's indexed content — answers cite the guides they adapt), every output
badged as AI (12's contract), community verification prompt shown after
("Did this work for you?"), and safety gates: MEDICAL/ELECTRICAL/CONSTRUCTION
categories route through strict prompt guardrails and always append the
standard safety-warning block; the assistant never overrides a guide's
safety_warning. Cost note: adaptation runs are logged against 33's ledger —
per-user daily AI quota keeps the free tier sustainable (brief §8).
Tests: adaptation output cites guides, badge contract, safety-gate append,
quota enforcement, retrieval determinism with fixture corpus.
**Acceptance:** "I have a sufuria, a jiko, and ughali flour" produces an
adapted, cited, safety-labeled plan — labeled as AI.

---

### Prompt 35 — Multilingual Completion
**Build:** `locale/` completion, `apps/core/i18n.py` +
`tests/test_i18n.py`
**Depends on:** 05, all content prompts

Complete the Kiswahili UI: all user-facing strings from Phases 0–3
translated (community-reviewable translation workflow: a `sw` string with a
"needs review" flag), content-language preference (users read content in sw,
en, or both — mixed feeds show both with language chips), Sheng-ready
content fields (aliases and nicknames already accept it from 25; document
that UI chrome stays en/sw while content is free-form). Language toggle
persists; no page re-load required beyond the request.
**Pitfall:** translation files drift when English strings change — a test
enumerates msgids present in code but missing from `sw` (fails the suite, so
drift can't land silently).
Tests: missing-translation detector, content-language preference, mixed-feed
chips, toggle persistence.
**Acceptance:** the full app is usable in Kiswahili, honestly partial where
content is user-generated.

---

## Phase 5 — Scale, Trust & Launch

| #  | Module | Files | Depends On |
|----|--------|-------|------------|
| 36 | Rate limiting & abuse prevention | `mtaani/settings/`, `apps/core/throttle.py` | 04, 15 |
| 37 | Privacy-safe analytics & admin | `apps/analytics/` | 15, 27 |
| 38 | Monetization hooks (off by default) | `apps/monetize/` | 19, 24 |
| 39 | Performance & data-budget audit | `tests/test_budgets.py` | all |
| 40 | KDPA compliance workflows | `apps/compliance/` | 04, 27 |
| 41 | Security hardening & backups | `mtaani/settings/`, `docs/runbooks/` | all |
| 42 | Deployment & launch docs | `deploy/`, `docs/`, CI | all |

### Prompt 36 — Rate Limiting & Abuse Prevention
**Build:** `apps/core/throttle.py` (per-endpoint config), settings middleware
(parent) + `tests/test_throttle.py`
**Depends on:** 04, 15

Systematic throttles: per-user and per-IP buckets per endpoint class (auth
already limited from 04; posting 20/day, messaging 100/day, reports
10/hour from 15, uploads 30/day), spam heuristics from 17/23 unified into
one rules module with a single dashboard read, and progressive penalties
(first = soft warning, repeated = timeouts). Throttle state in Redis
(compose service) with a pure-Python fallback store so tests don't need it.
**Pitfall:** throttles must live server-side; anything client-side is a
suggestion.
Tests: bucket math, headers on 429s, warning-then-timeout progression,
fallback store correctness, per-endpoint config loading.
**Acceptance:** a spammer script gets progressively slower and eventually
locked out, while normal use never notices.

---

### Prompt 37 — Privacy-Safe Analytics & Admin
**Build:** `apps/analytics/` + `tests/test_analytics.py`
**Depends on:** 15, 27

Aggregate-only ops metrics (brief §6-8): daily active users, posts/guides/
listings created, moderation queue load, tracking sessions by SACCO,
feed-tier engagement (what % of feed items are local — the utility metric,
NOT time-on-app). NO per-user tracking, NO session replay, NO ad-tech
pixels — an analytics event schema that structurally cannot hold a user ID
(counts by day×category only). Admin dashboards for moderators (queue age,
report volume) and SACCO operators (their vehicles' session hours).
**Pitfall:** the schema is the privacy guarantee — if a well-meaning
contributor can add a user_id column, the DPA exposure comes back.
Tests: event schema rejects user-identifiable fields (assert columns),
aggregate queries, dashboard rendering, moderator vs SACCO role scoping.
**Acceptance:** the team can answer "is the app useful?" without surveillance.

---

### Prompt 38 — Monetization Hooks (Off by Default)
**Build:** `apps/monetize/` + `tests/test_monetize.py`
**Depends on:** 19, 24

Scaffolding only, flags default OFF (brief §8: don't monetize before
demand): promoted-listings slots in search results (clearly labeled
"promoted", capped ratio ≤1 per 6 organic, never in feed ranking — 11's
scoring is untouched), merchant subscription tier model (feature flags:
portfolio size, listing refresh cadence), SACCO premium tools registry
(fleet analytics view stubs). Every monetization surface ships with its
label copy in en+sw and an audit row when enabled. No payments processing —
billing is a future prompt once demand is proven.
Tests: flags default-off, promoted labeling, ratio caps, audit rows, feed
ranking independence.
**Acceptance:** monetization is armed but pointed at nothing until the team
flips flags deliberately.

---

### Prompt 39 — Performance & Data-Budget Audit
**Build:** `tests/test_budgets.py`, `scripts/audit_pages.py` + fixes across
apps (each fix listed per-app under Parent integration)
**Depends on:** all prior content prompts

The 3G/70 MB-bundle discipline made mechanical: page-weight audit for every
primary route (anonymous + authed) against the 50 KB budget; image-variant
weights re-asserted end-to-end; query audit via `assertNumQueries` on feed,
search, and map endpoints (N+1 hunts); pagination sanity on every list
surface; a scripted slow-3G simulation (throttled local run) measuring time-
to-usable on the feed, a guide page, and the live map; SW cache correctness
re-verified (no authenticated JSON cached, offline guide flow still works).
**Pitfall:** budgets rot as features land — this test suite is the
enforcement, not the review.
Tests: the audit suite IS the deliverable; every failure found gets a
per-app fix (file ownership respected) until green.
**Acceptance:** full suite green with budgets enforced; a slow-3G walk of
the core journeys stays usable.

---

### Prompt 40 — KDPA Compliance Workflows
**Build:** `apps/compliance/` + `tests/test_compliance.py`
**Depends on:** 04, 27

Makes the DPA operational (research-notes §KDPA): data inventory
(`DataMap` registry: every model field that holds personal data → purpose,
lawful basis, retention period — self-documenting and test-audited),
data-subject request workflows (access: export a user's data as a readable
archive; deletion: anonymize account while preserving moderation evidence
and financial-adjacent audit rows — the DPA balances deletion against legal
retention; correction: profile self-serve), consent ledger UI (04's
ConsentEvents viewable per user, exportable for ODPC demonstration),
retention jobs (prune expired pings 27, expired sessions, aged consents per
inventory), and a DPIA document for live-location tracking checked into
docs (the flagship feature is high-risk processing — the DPIA is required,
and 27's guardrails are its mitigations). **Human task, not code:** ODPC
registration — tracked in Project_state.md as a launch blocker with the
checklist in docs.
Tests: inventory completeness (every model with personal-data flags has a
DataMap entry), access export round-trip, deletion anonymization (no PII
remains resolvable; moderation evidence survives), retention job math,
consent export.
**Acceptance:** an ODPC auditor's core asks (what data, why, where,
how long, show me my data, delete me) are answerable from the app itself.

---

### Prompt 41 — Security Hardening & Backups
**Build:** `mtaani/settings/` hardening (parent), `docs/runbooks/security.md`,
`docs/runbooks/backup.md`, `scripts/` + `tests/test_security.py`
**Depends on:** all

Pre-launch hardening: security headers (HSTS, CSP with map-tile allow-list,
X-Frame-Options, Referrer-Policy), CSRF/form hardening re-audit, session
cookie flags (`Secure`, `HttpOnly`, `SameSite=Lax`), a dependency audit step
in CI (`pip-audit`), and a secrets sweep test (fail the suite if any
credential-looking string lands in tracked files). Backup runbook: nightly
`pg_dump` to encrypted storage, documented restore drill (a restore is only
a backup after it has been rehearsed — runbook includes the drill steps).
**Pitfall:** CSP + Leaflet + inline HTMX needs a nonce strategy — plan the
allow-list before tightening, or the map goes white.
Tests: header assertions, cookie flags, secrets-sweep fixtures, restore-drill
script dry-run.
**Acceptance:** headers tight, cookies strict, secrets swept, restore
rehearsed.

---

### Prompt 42 — Deployment, CI & Launch Docs
**Build:** `deploy/` (compose-prod, Caddy/TLS notes), `.github/workflows/ci.yml`,
`docs/launch-checklist.md`, final README + `Project_state.md` updates
**Depends on:** all

Production compose (app + Postgres/PostGIS + Redis + Caddy reverse proxy),
GitHub Actions CI (migrations check, pytest against a PostGIS service
container, budget suite, pip-audit — the whole test stack runs on every PR),
launch checklist (from brief §10 + 40's human tasks: ODPC registration
confirmed, pilot SACCOs onboarded, DPIA reviewed, data inventory current,
backups rehearsed), operator README (env keys, deploy steps, health checks,
rollback), and the ADR index updated (docs/adr/) to reflect every decision
this build made.
**Pitfall:** CI that can't run PostGIS quietly skips geo tests — the workflow
must use the real `postgis/postgis` service container and FAIL if the
extension is missing.
Tests: CI workflow file validated (the budget + geo suites are named in it
explicitly), deploy compose config-check passes, health endpoint works.
**Acceptance:** clone→CI green→deploy is one documented path, and the
launch checklist is a real list of unambiguous human tasks.

---

## Dependency Graph (build order)

```
01 scaffold
└─ 02 types ──────────────┐
└─ 03 settings/geo ──┐    │
   ├─ 04 auth ───────┼────┤
   ├─ 05 design/PWA  │    │
   ├─ 06 images      │    │
   │   └─ 07 profiles┘    │
   │      ├─ 08 guides ─ 09 offline
   │      ├─ 10 posts ─ 11 feed (needs 13)
   │      ├─ 12 qa
   │      ├─ 13 follows/groups ────────┐
   │      ├─ 14 search (08,10,12)      │
   │      ├─ 15 moderation ───────────┤
   │      ├─ 16 trust (10,12)          │
   │      ├─ 17 listings ─ 18 services ─ 19 listing-search
   │      │            ─ 21 barter (needs 20) ─ 22 reviews ─ 23 disputes
   │      │            ─ 24 lifecycle
   │      ├─ 20 messaging (04,15)
   │      └─ 25 routes ─ 26 fleet ─ 27 tracking-sessions
   │                       ├─ 28 ingest/map ─ 31 alerts (needs 13)
   │                       ├─ 29 crew-flow
   │                       └─ 30 crowd-reports ─ 32 traffic-planning
   ├─ 33 ai-layer ─ 34 assistant (needs 08)
   └─ 35 i18n-completion (after all content prompts)
36 throttle · 37 analytics · 38 monetize · 39 budgets · 40 compliance ·
41 security · 42 deploy  (36–38 parallelizable; 39–42 sequential, last)
```

Parallelizable groups (one subagent each, no shared files):
- **Group A:** 08, 10, 13, 15 (after 07)
- **Group B:** 17, 20 (after 15), then 18, 21, 22, 24
- **Group C:** 25 (after 03) → 26 → 27 → {28, 29, 30} → 31

## Testing Strategy

- **Stack:** pytest + pytest-django against the real PostGIS container
  (`postgis/postgis:16`) — geo code is never tested against SQLite.
- **Unit:** every prompt's app ships its own `tests/test_<app>*.py`; mocks for
  SMS/push/LLM/network; clocks injected wherever time affects behavior
  (ranking, decay, expiry, throttles).
- **Integration:** phase-boundary runs of the FULL suite (`make test`); the
  parent runs these between phases, never mid-prompt.
- **Budgets:** `apps/core/testing.py::assert_under_budget` on every primary
  route; Prompt 39 makes this comprehensive.
- **Human gates:** moderation/suspension/dispute/monetization flows are tested
  with a two-actor model (drafter ≠ confirmer) — the gate is code, not policy.
- **CI (from 42):** every PR runs unit + budget + geo suites in a PostGIS
  service container; `pip-audit` blocks vulnerable deps.

## Progress Tracker

| # | Prompt | Status |
|---|--------|--------|
| 01 | Repo scaffold & dev environment | ✅ |
| 02 | Core types, enums & budget utils | ✅ |
| 03 | Settings, PostGIS & geo utilities | ✅ |
| 04 | Auth, verification & recovery | ✅ |
| 05 | Design system, i18n shell & PWA | ✅ |
| 06 | Image compression pipeline | ✅ |
| 07 | Profiles & privacy controls | ✅ |
| 08 | Guides CRUD & safety warnings | ☐ |
| 09 | Offline saved guides | ☐ |
| 10 | Posts & photos | ☐ |
| 11 | Utility feed ranking | ☐ |
| 12 | Q&A with AI-answer labeling | ☐ |
| 13 | Follows & groups | ☐ |
| 14 | Cross-entity search | ☐ |
| 15 | Moderation v1 | ☐ |
| 16 | Trust signals & helpfulness | ☐ |
| 17 | Marketplace listings (goods) | ☐ |
| 18 | Service listings & portfolios | ☐ |
| 19 | Listing search & geo filters | ☐ |
| 20 | Messaging v1 | ☐ |
| 21 | Barter & cash-plus-exchange | ☐ |
| 22 | Reviews & reputation | ☐ |
| 23 | Disputes & fraud flags | ☐ |
| 24 | Listing lifecycle | ☐ |
| 25 | Route & stage database + GTFS seeder | ☐ |
| 26 | Vehicle, SACCO & crew registration | ☐ |
| 27 | Authorized tracking sessions | ☐ |
| 28 | Location ingestion & live map | ☐ |
| 29 | Driver/conductor low-data flow | ☐ |
| 30 | Passenger crowd reports | ☐ |
| 31 | Arrival estimates & follow alerts | ☐ |
| 32 | Traffic & route planning | ☐ |
| 33 | AI abstraction layer | ☐ |
| 34 | Local-adaptation assistant | ☐ |
| 35 | Multilingual completion | ☐ |
| 36 | Rate limiting & abuse prevention | ☐ |
| 37 | Privacy-safe analytics & admin | ☐ |
| 38 | Monetization hooks (off by default) | ☐ |
| 39 | Performance & data-budget audit | ☐ |
| 40 | KDPA compliance workflows | ☐ |
| 41 | Security hardening & backups | ☐ |
| 42 | Deployment, CI & launch docs | ☐ |

*Working name "Mtaani Connect" is a placeholder per the brief — the prompt
set is name-agnostic (project package `mtaani` is trivially renamed).*