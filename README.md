# 🚐 Mtaani Connect

**One app for everyday life in the mtaa — how you get around, what you
know, what you sell, and who you trade with.**

*Why the name: "mtaa" is Swahili for street or neighborhood. Add "-ni" and
"mtaani" means "out there in the streets" — where everyday life actually
happens. The 🚐 is the matatu, because getting people moving is the
flagship.*

---

**The build is done.** All 42 recipes in the recipe book (`PROMPTS.md`)
have been cooked, each one sealed with its own passing exam before the next
started — **1,122 tests green**, every promise below enforced by an exam
that fails the build if it ever breaks. What remains is the human work of
launching, and you're invited (see the end).

Imagine the stage, the market, and the WhatsApp group rolled into one
trustworthy app that lives on the phone you already have, works on the data
bundle you already buy, and never plays tricks on you. That's what's in
this repo.

## What it does (four everyday jobs)

**Catch your matatu without the guesswork.** See where your vehicle is
(when the crew or the sacco is sharing it — with real, hashed-permission
tokens, not vibes), get a **heads-up alert when it's nearing your stage**
("my matatu is coming" — or an honest "not enough live data to estimate,"
never a fake ETA), and plan your journey across the stage graph with fare
ranges cited per leg. Delays, closures and flooding reach the map **with
their sources attached** — a rider's flood report on route X is labeled as
exactly that, next to the official route data, never blended into it.

**Learn things that work here.** Step-by-step guides for cooking, farming,
repairs and business — written by people like you, for the tools and
ingredients you can actually find at the duka, saved for offline reading on
the 3G ride home. **Ask Mtaani** adapts a guide to what *you* have ("I have
a sufuria, a jiko, and ughali flour" → an adapted plan, citing the guides
it learned from) — and every AI-touched word carries a badge that says so,
with the safety warning of the original guide pinned verbatim. A guess
that pretends to be a fact is worse than no answer, so the app never makes
one.

**Buy, sell, and swap.** A marketplace for goods and skills — and the thing
nobody else offers: proper **barter**. Trade two kilos of maize and an hour
of sewing for a solar lamp plus a little cash on top. Make offers and
counteroffers, shake hands, leave each other reviews, and if a deal goes
wrong there's a real dispute path with real moderation. The app never holds
your money — it helps two people agree and keeps the receipts; it doesn't
try to become a bank. (And nothing is monetized before people prove they
want it — the hooks exist, the flags are all off, and flipping one is an
audited, who-flipped-it-when event.)

**Talk without giving your number away.** Ask a seller about a listing,
negotiate a swap, chat in groups — through the app, so your phone number
and your exact location stay yours unless *you* decide to share them.

## Five promises we hold ourselves to — now exam-enforced

1. **We never lie to you.** Missing data gets a clear "no live data right
   now," a cited disruption line, or a labeled ESTIMATE — never a confident
   guess dressed up as a fact. The tests fail if a fake dot or an invented
   fare ever renders.
2. **We respect your bundle.** Data is money — about KES 10 buys 70 MB for
   a day. Every page is audited against a **50 KB budget** (the crew's trip
   page in **20 KB**), photos are squeezed into thumb + standard variants
   before they travel, the whole core journey is proven usable in under
   8 seconds on slow 3G, and guides save for offline. Cheap to use, or
   we've failed.
3. **Your privacy is the default, not a scavenger hunt.** You share your
   ward — never your exact location (and for crew tracking: trip endpoints
   suppressed plus a 1 km ring, positions gone after 72 hours, tokens
   stored only as hashes). You can **export your data, correct it, or
   delete yourself** — real workflows, with an ODPC-auditor-ready trail.
   The analytics the team sees cannot even *hold* a user ID; the schema
   refuses it. Kenya's Data Protection Act, operational — not fine print.
4. **Useful beats addictive.** The feed ranks genuine local usefulness, not
   doom-scroll. The whole thing is usable in **Kiswahili or English** (the
   community can review and improve every translation, and the build fails
   if translations drift), and your content stays yours — Sheng, Kiswahili,
   or plain English included.
5. **Community before extraction.** The core stays affordable or free.
   Every monetization surface ships off-by-default, clearly labeled,
   capped (≤1 promoted per 6 organic), and audited — armed until the team
   deliberately points it at something real.

## What's inside (by the people's stages)

| Stage | Steps | What's there |
|-------|-------|--------------|
| 1. Foundation | 01–07 | Joining, safe login, profiles with ward-granularity privacy, the offline-first PWA shell, image pipelines |
| 2. Community | 08–16 | Guides (with safety-critical warnings), posts, Q&A, search, moderation, trust & suspension |
| 3. The market | 17–24 | Listings, haggling, barter with counteroffers, messaging, reviews, disputes, lifecycle & pricing |
| 4. The flagship | 25–31 | Real GTFS route data, crew-authorized live tracking, honest map & ingest, the crew's 20 KB trip page, ETA alerts |
| 5. Intelligent | 32–35 | Stage-graph journey planner with cited disruptions, the AI layer + Ask Mtaani, full Kiswahili |
| 6. Strength | 36–42 | Throttles & lockouts, privacy-safe ops analytics, dormant monetization, the data-budget audit, KDPA compliance, hardening & backups, deploy + CI |

33 Django apps, **1,122 tests** (modules × behaviors × budgets × security ×
compliance), a CI that runs the whole stack on every change, and a deploy
story that ends with `{"ok": true}` from `/health/`.

## Where the ideas come from — not vibes

- **The route map starts from real open data** — Digital Matatus, a project
  where university teams rode and mapped ~135 Nairobi matatu routes,
  publicly and for free. We treat it as a starting sketch the community
  corrects through a built-in corrections queue, never gospel.
- **The bundle budgets come from actual carrier prices** — real Safaricom
  and Airtel bundle costs set the page-size limits the exams enforce.
- **The privacy rules come from Kenya's Data Protection Act (2019)** —
  consent you can prove, data kept only as long as documented, deletion
  that works, and a DPIA for live location on file.
- **We studied what already exists** — booking apps, sacco fleet tools,
  traffic feeds — and built the missing layer: the community around all of
  it.

(Full sources and links: `docs/research-notes.md`; the decisions:
`docs/adr/`.)

## What's next: launch, not build

The code is ahead of the paperwork. The human list lives in
`docs/launch-checklist.md`, honestly:

- **ODPC registration** (data controller of record) — the app already
  produces the evidence pack (data map, consent exports, DSAR receipts,
  retention logs, the DPIA)
- **3–5 pilot SACCOs** in Nairobi to put real crews on real routes
- **15–20 commuter interviews** to sanity-check priorities
- **A scheduler** for the built-in sweeps (expiry, pruning, alert scans —
  every one is a pure, clock-injected function waiting for a cron entry)
- One backup restore drill, then a real pilot

## Want in?

- **SACCOs and operators** — this is the moment; the pilot is the open door
- **Commuters** — your corrections and your reports are a featured feature,
  not a feedback form
- **Moderators, translators, technical partners** — the contract, research,
  runbooks, and standing state of every decision are all in this repo,
  in plain view

**Contact:** [your name, email or phone]

## For technical readers

- `PROMPTS.md` — the build contract: 42 self-contained, testable prompts,
  all landed, one commit each (`git log` tells the story)
- `Project_state.md` — the live status: health, decisions, ops notes
- `docs/runbooks/` — the security posture and the backup + restore drill
- `deploy/` — production compose (PostGIS + Redis + Caddy), secrets-gated;
  `.github/workflows/ci.yml` runs migrations check, proves the PostGIS
  extension exists, the full suite, the budget suite, geo suites, and
  `pip-audit` on every PR
- Stack in one line: **Django 6 + PostgreSQL 18/PostGIS, server-rendered
  PWA, pure-Python algorithm modules behind thin views, mock-first
  adapters for every external system**
- Run it: `make setup`, `make db-start`, `make test`
- An honesty test to carry home: `tests/test_budgets.py` fails if any page
  grows past its bundle budget, and `tests/test_security.py` fails if a
  secret ever lands in the repo. The app can't lie about size or leak by
  accident without someone noticing immediately.

## License

**MIT** — anyone may use, study, change, and share this code, including
commercially, as long as the license and attribution travel with it. See
`LICENSE`.

*"Mtaani Connect" is a working name; the codebase doesn't care what it's
called.*
