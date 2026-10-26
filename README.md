# 🔥 Mtaani Connect

**One app for how Kenya actually moves, learns, trades, and looks out for each other.**

Mtaani Connect is a community super-app built for the phones, bundles, and daily
reality of urban Kenya — not a Silicon Valley feed with a matatu sticker on it.
Seven modules, one promise: help people move efficiently, solve practical
problems, earn, exchange value, and build stronger communities.

- **Track matatus honestly.** Live positions, arrival estimates, and disruption
  alerts — with visible confidence labels, and a plain "no live data" when there
  is none. We never invent a position, an ETA, or a fare.
- **Learn what works here.** Step-by-step guides for cooking, farming, repairs,
  and business — written by and for Kenyans, with AI that adapts instructions to
  what's actually at your duka (clearly labeled, community-verified).
- **Trade and barter.** Goods, services, and the differentiator: structured
  barter with counteroffers, mutual reviews, and dispute workflows — no escrow
  games, no custody of your money, no surprises.
- **A feed that helps.** Local relevance and usefulness beat engagement bait.
  Yesterday's genuinely useful post outranks five-minutes-ago rage.

## Repository status: build contract complete

This repo currently holds the **product brief**, the **research grounding**, and
the **build contract: `PROMPTS.md`** — 42 numbered, testable prompts organized
into 6 phases that map one-to-one onto the roadmap. Each prompt is
self-contained (a cold-start agent or contractor can execute it alone), ships
with its own tests, and lands as exactly one commit.

| Phase | Prompts | Goal |
|-------|---------|------|
| 0 — Foundation | 01–07 | Auth, profiles, design system, PostGIS geo layer, PWA shell |
| 1 — Community | 08–16 | Guides, offline saving, posts, utility feed, Q&A, search, moderation |
| 2 — Commerce | 17–24 | Listings, barter + negotiation, messaging, reviews, disputes |
| 3 — Transport | 25–31 | Route DB (open GTFS seed), authorized tracking, live map, alerts |
| 4 — Intelligence | 32–35 | Route planning, AI local-adaptation, full Kiswahili UI |
| 5 — Scale & Trust | 36–42 | Abuse prevention, privacy-safe analytics, KDPA workflows, deploy |

## Technology choices (ADR-0001)

- **Django modular monolith + PostgreSQL 16 / PostGIS** — the brief's stated
  architecture; geo is core, and PostGIS is the brief's explicit requirement.
- **Server-rendered templates + HTMX-style progressive enhancement + Leaflet** —
  page payloads stay tiny for 3G and 70 MB daily bundles. No SPA bundle tax.
- **PWA (installable, offline-capable)** — Android-first on low-cost phones
  without a native build; iOS later.
- **Mock-first adapters** — SMS, push, and AI providers are interfaces with
  deterministic mocks, so every test runs offline and no prompt needs paid keys.
- **Privacy by design, Kenya Data Protection Act 2019** — ward-level location
  by default, EXIF stripped from uploads, logged consent, subject
  access/deletion workflows, and tracking that is public *only* during active
  authorized sessions. See `docs/adr/0001-stack-decision.md`.

## Grounded in research, not vibes

The prompt set was written against real sources (full list with links in
`docs/research-notes.md`): the open **Digital Matatus GTFS** (135+ routes,
~3,000 stops) seeds the route database; **Kenyan bundle economics**
(~KES 10 / 70 MB daily) set the enforced page-weight budgets; the **Kenya Data
Protection Act 2019** and ODPC guidance define the consent, minimization, and
subject-rights prompts; and the competitive landscape (booking/tracking apps,
SACCO fleet tools) shaped the differentiation: community first, honest data,
barter as a first-class citizen.

## What we're looking for

Per the brief: technical partners, pilot SACCOs in Nairobi, and user research
(15–20 interviews) to validate module priority — the build sequence flips
Phases 1 and 3 if research says transport is the urgent need. The contract is
written so that flip is cheap.

## Repo layout

```
PROMPTS.md                 # The build contract — 42 testable prompts
docs/product-brief.md      # The brief this repo executes
docs/research-notes.md    # Sources + how each shaped the prompts
docs/adr/                  # Architecture decision records
Project_state.md           # Living status: where we are, what's next
```

## License

Proprietary — all rights reserved pending co-founder/partner agreements.

*Working name "Mtaani Connect" is a placeholder; the codebase is name-agnostic.*