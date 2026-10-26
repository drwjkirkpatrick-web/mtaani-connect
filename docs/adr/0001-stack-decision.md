# ADR-0001: Technology Stack — Django Modular Monolith + PostGIS, PWA Frontend

**Status:** Accepted (2026-10-25)
**Context:** Mtaani Connect product brief §6 mandates: modular monolith
initially, PostGIS for geographic data, mobile-first responsive on affordable
Android, intermittent 3G connectivity, English + Kiswahili, AI abstraction
layer, documented architecture decisions. Pilot in Nairobi on a small team.

**Decision:**

1. **Backend: Django 6 (LTS track) — modular monolith.** One Django app per
   prompt under `apps/` (accounts, guides, posts, feed, listings, barter,
   messaging, routes, tracking, crew, crowd, alerts, traffic, ai,
   moderation, compliance, ...). Boundaries enforced by app structure and
   public query functions, per the brief's "modular boundaries" principle.
   - *Why Django:* auth + phone/email verification, migrations, admin (SACCO
     operators and moderators need dashboards fast), i18n machinery for
     Kiswahili, mature PostGIS integration (GeoDjango), batteries that a small
     team cannot rewrite.
2. **Database: PostgreSQL 16 + PostGIS** via `docker compose`
   (`postgis/postgis:16`). All distance queries on `geography` columns.
   - *Why:* mandated by the brief; SQLite cannot stand in, and we do not
     maintain two query dialects.
3. **Frontend: server-rendered Django templates + small hand-written JS +
   Leaflet (OSM tiles), installed as a PWA.** No SPA framework in V1.
   - *Why:* the binding constraint is bundle economics (≈KES 10 buys 70 MB
     daily; 2 GB ≈ 3.9% of monthly income across comparable markets). Server
     HTML keeps authenticated pages ≤50 KB; a React/Vue bundle would spend the
     user's money before content arrives. Leaflet + cached OSM raster tiles
     is the lightest honest map. PWA gives install/offline on low-cost Android
     without a second codebase; native iOS can wait (brief §4).
4. **Interfaces behind adapters, mock-first:** `SMSAdapter`/`EmailAdapter`
   (MockSMS/MockEmail; Africa's Talking as the documented real adapter),
   `NotifyAdapter` (MockPush), `LLMProvider` (MockLLM + cloud/local adapters,
   local includes Ollama on the Jetson). No prompt requires network or paid
   keys to pass tests.
5. **Money:** no custody in V1 (brief §3 Module 6). M-Pesa/Daraja is a future
   adapter for cash-plus-barter settlement; the state machine leaves the stub.
6. **Testing:** pytest + pytest-django against the real PostGIS container;
   budget assertions in CI; every prompt ships tests.

**Alternatives considered:**
- *FastAPI + React SPA:* excellent APIs, but the SPA bundle works against the
  3G/data-budget constraint, and we'd rebuild auth/migrations/admin/i18n.
- *Flutter (Android-first native):* brief lists responsive web as a target and
  iOS later; two-platform native before validation contradicts the small-team
  pilot; PWA covers low-cost Android today.
- *Microservices:* explicitly excluded by the brief ("avoid premature
  microservices"); app-level module boundaries keep the future split cheap.

**Consequences:** HTMX-style progressive enhancement requires discipline about
JS size; template-first means any future native app needs a JSON API layer
(the apps already separate query functions from views, so DRF endpoints can
wrap them later); PostGIS-in-Docker is a hard dev-environment requirement,
enforced by Prompt 01's smoke test and CI's service container.