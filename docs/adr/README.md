# ADR index — decisions this build made

- [ADR-0001 — Stack decision](0001-stack-decision.md) · Django 6 + PostgreSQL
  18/PostGIS, server-rendered PWA, Postgres over NoSQL, no external routing
  engine. (Phases 0–1.)
- **0002 — Plain lat/lon over GeoDjango** (Prompt 25, routes/GTFS) · `Decimal`
  lat/lon + SQL-cast + gist gist index where PostGIS matters; GeoDjango's
  GDAL stack avoided — distances via `ST_DWithin` in `apps/listings/search.py`
  (Prompt 19) and pure haversine elsewhere; the geo capability is pinned by
  EXPLAIN tests rather than inherited from a framework.
- **0003 — Pure module pattern** (Prompts 11, 30, 32, 35, 38) · Anything
  that is an algorithm gets a Django-free module inside its own app
  (`feed/ranking.py`, `crowd/weighting.py`, `traffic/graph.py`,
  `monetize/ratio.py`, `core/throttle.py`), imported by views that only
  orchestrate; test-pinned by import-time purity probes. WHY: the algorithm
  stays cheap to reason about, unit-testable without Django, and the
  Django layer stays thin.
- **0004 — Privacy by query layer** (Prompts 26, 27, 37, 40) · Access rules
  live in service/query functions (`visible_vehicles`, `visible_positions`,
  aggregate-only analytics schema, compliance exporter) rather than
  template conditionals; "template-free by construction" is the standard
  the tracking privacy rules set and the analytics column pinning extends.
- **0005 — Mock-first adapters with honest stubs** (Prompts 04, 21, 31, 33)
  · Every external system (SMS/email, M-Pesa, Web Push, LLM providers,
  Redis throttle store) enters through an interface whose default is a
  deterministic mock; a REAL backend ships as a documented `is_stub` class
  that raises on call until env-wired — no silent network calls, no fake
  success outputs.
- **0006 — One commit per prompt; waves of parallel builders** (whole build)
  · The 42-prompt contract executed in parallel subagent waves, one commit
  per prompt, shared files owned by the parent only; salvage-first when a
  wave dies mid-flight. The build log carries the cadence (see
  Project_state.md).
- **0007 — Cost honesty** (Prompts 24, 33, 34, 38) · Nothing fabricates a
  single number where uncertainty exists: fares are observed ranges with
  confidence labels, ETAs carry ±25% bands + ESTIMATE labels, AI content is
  labeled and never auto-published, monetization is off by default. Missing
  data renders as honest absence, never as 0.
