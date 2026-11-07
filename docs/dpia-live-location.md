# DPIA — Data Protection Impact Assessment: Live Matatu Tracking

**Status:** ACTIVE · **Owner:** Walker (data controller of record) · **First written:** 2026-11 · **Review due:** 6 months after launch, then annually
**Scope:** Prompt 27 `apps/tracking` (authorized sessions + position storage) and Prompt 28 `apps/tracking/ingest.py` (crew-device ingestion), as consumed by `apps/map` (rider view), `apps/crew` (crew view), `apps/alerts` (ETA/approach).

## 1. Why this DPIA exists

Live location of moving vehicles is **high-risk processing** under the Data Protection Act (Kenya, 2019): positions are continuous, can map to individuals' movements, and the subjects (drivers/conductors) are embedded in a commercial power relation with their SACCO — consent pressure is a real risk. This document records the processing, the risks, and the concrete mitigations as they are enforced in code, so the assessment and the product stay in sync.

## 2. Processing described

- **What:** vehicle position pings (lat/lon, speed, recorded timestamp) uploaded by an authorized crew device during one session; stored server-side; shown to riders as the vehicle's position; used to compute ETAs and stage-approach alerts.
- **Who:** crew (data subjects — their vehicles' positions reflect their working activity); riders/viewers (data consumers of an *aggregate, vehicle-level* signal); SACCO admins (operational oversight, their own fleet only).
- **Lawful basis:** crew-side — **contract** (the SACCO's service obligation to publish vehicle positions to its paying riders) plus **consent** captured at session start (03 consent ledger); rider-side — no personal data is collected from viewers beyond what any page visit already entails (server logs), and no location of a person is ever stored — only of a *vehicle plate*.
- **Retention:** pings pruned after **72 h** (`tracking.services.prune_pings`); sessions are capped at **12 h** and derived-state (`session_status`) is computed, never stored; read-back twins expire on the same 72 h clock. No long-lived location history exists *by design*.

## 3. Controls in code (the mitigations — each enforced and test-pinned)

| Risk | Control | Enforced at |
|---|---|---|
| Token theft → position spoofing | session tokens stored **sha256-hashed**, display-once, scope-baked; rotation kills the old raw token | `TrackingSession` (Prompt 27); ingest refuses non-matching tokens uniformly |
| Historical movement profiling | 72-hour prune sweep; no status column stored (derived per read) | `prune_pings()` / `expire_sessions()` sweeps |
| Rider stalking of a *person* | R3: **anchor pings always suppressed** + a 1 km geodesic suppression ring around trip endpoints — trip endpoints are where drivers live/work | `services.visible_positions()` (query-layer rule, template-free by construction) |
| Stale "last seen" building a track | R1: only LIVE sessions ever show positions; R2: read-backs expire with the session's 72-h twin | `visible_positions()` |
| Over-collection from the device | ingest caps: ≤50 rows/batch, JSON-only, uniform refusal shapes; impossible speed (>150 km/h) → kept but **downgraded to ESTIMATE confidence**, never silently deleted | `apps/tracking/ingest.py` |
| Cross-tenant surveillance | SACCO admins see only their own fleet's sessions (`visible_vehicles(user)` query wall; staff have no bypass) | `apps/fleet` (Prompt 26) |
| Free-tier abuse → cost pressure to over-retain | throttle middleware + per-user AI/notify quotas | Prompt 36 (`apps/core/throttle_django.py`) |
| Consent drift | consent ledger is exportable for ODPC demonstration; DSAR workflows in `apps/compliance` | Prompt 04 + Prompt 40 |

## 4. Residual risks (accepted + reviewed)

1. **Vehicle ≠ person, but high-frequency positions of a plate correlate with a driver.** Mitigated by endpoint suppression + the 72-h prune; residual risk accepted for the operational value; revisit with the crew-welfare interviews (Project_state, user research).
2. **Operator-side aggregation reveals productivity patterns.** The SACCO analytics surface (`apps/analytics`) is aggregate-only (day × metric, structurally no person column); per-vehicle session *hours* are visible to the sacco admin who could compute them anyway from their own dispatch data.
3. **Compromise of the server exposes up to 72 h of positions for live vehicles.** Encryption at rest + pg_dump hygiene are the Prompt 41 controls; the blast radius is bounded by the retention sweep.
4. **Crew consent pressure.** Publishing positions is a service contract term; the mitigation is visibility (crew see exactly what riders see of their own vehicle via the crew page) and the documented right to raise a dispute (Prompt 23) if tracking is misused.

## 5. ODPC posture

Registration is a human task tracked as a **launch blocker** in `Project_state.md`; the evidence pack this app already yields: consent exports, DataMap inventory, DSAR request records, retention-sweep job logs, and this document. The checklist lives in `docs/odpc-registration-checklist.md`.

## 6. Sign-offs & change log

- Controls verified in the test suite at the SHAs of Prompts 26, 27, 28, 36 and 40 (this repo's history).
- Next review: with the SACCO pilot (before public launch), then annually.
