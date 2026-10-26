# 🚐 Mtaani Connect

**One app for everyday life in the mtaa — how you get around, what you know, what you sell, and who you trade with.**

*Why the name: "mtaa" is Swahili for street or neighborhood. Add "-ni" and
"mtaani" means "out there in the streets" — where everyday life actually
happens. The 🚐 is the matatu, because getting people moving is the flagship.*

Imagine the stage, the market, and the WhatsApp group rolled into one
trustworthy app that lives on the phone you already have, works on the data
bundle you already buy, and never plays tricks on you.

---

## What it does (four everyday jobs)

**Catch your matatu without the guesswork.** See where your vehicle is (when
the crew or the sacco is sharing it), get a heads-up when it's nearing your
stage, and know about delays, closures and flooding — reported by riders
like you. And here's the rule that makes it trustworthy: if the app doesn't
know where a matatu is, **it says so.** No fake dots. No made-up arrival
times. No invented fares.

**Learn things that work here.** Step-by-step guides for cooking, farming,
repairs and business — written by people like you, for the tools and
ingredients you can actually find at the duka. There's also a helper that
adapts a guide to what *you* have ("no oven? here's the jiko version") —
and it always admits when it's the computer talking, because a guess that
pretends to be a fact is worse than no answer.

**Buy, sell, and swap.** A marketplace for goods and skills — and the thing
nobody else offers: proper **barter**. Trade two kilos of maize and an hour
of sewing for a solar lamp plus a little cash on top. Make offers, make
counteroffers, shake hands, leave each other reviews, and if a deal goes
wrong there's a real way to report it. Important: the app never holds your
money. It helps two people agree and keeps the receipts; it doesn't try to
become a bank.

**Talk without giving your number away.** Ask a seller about a listing,
negotiate a swap, chat in groups — through the app, so your phone number
and your exact location stay yours unless *you* decide to share them.

## Five promises we hold ourselves to

1. **We never lie to you.** Missing data gets a clear "no live data right
   now," never a confident guess dressed up as a fact.
2. **We respect your bundle.** Data is money — about KES 10 buys 70 MB for a
   day. So every page is kept tiny on purpose, photos are squeezed before
   they travel, and guides can be saved for offline reading. The app is
   cheap to use, or we've failed.
3. **Your privacy is the default, not a scavenger hunt.** You share your
   ward or your neighborhood — never your exact location unless you say so.
   Photos have their hidden location tags quietly removed before anyone
   sees them. You can ask for a copy of your data, or its deletion, and we
   keep records to prove what you agreed to and when (Kenya's Data
   Protection Act, taken seriously, not as fine print).
4. **Useful beats addictive.** The feed ranks what's genuinely helpful and
   what's nearby — not what keeps you doom-scrolling. Yesterday's useful
   post outranks five-minutes-ago outrage.
5. **Community before extraction.** The core stays affordable or free.
   Money-making features come only after real people prove they want them.

## How we're building it: a recipe book, not a moonshot

Before writing a line of code, we wrote a **recipe book** for the whole app:
42 small, numbered recipes (`PROMPTS.md`). Each recipe adds exactly one thing,
must pass its own little exam before it counts as done, and gets sealed and
recorded before the next one starts. There are already **80+ of these little
exams**, and the app has to pass *all of them, every single time*, before
anything is called finished. If we ever break a promise above, an exam fails
and the build stops until it's honest again.

The order is like building a house:

| Stage | What gets built | In plain words |
|-------|-----------------|----------------|
| 1. Foundation *(done)* | Steps 01–07 | Joining, logging in safely, your profile and privacy controls, the app shell, photo handling |
| 2. Community | Steps 08–16 | The guides, posts, questions, search, and the tools that keep the neighborhood civil |
| 3. The market | Steps 17–24 | Listings, barter with counteroffers, private chats, reviews, dispute handling |
| 4. The matatu flagship | Steps 25–31 | The route map (from real open data), live tracking *with permission*, alerts |
| 5. Smart helpers | Steps 32–35 | Journey planning, the adapting guide-helper, full Kiswahili |
| 6. Strength & scale | Steps 36–42 | Spam protection, honest numbers for the team, launch preparation |

## Where we are today

**The foundation is finished (steps 1–7 of 42).** Concretely, the app can
already:

- let you join, log in, and verify by code (through a stand-in messaging
  service while we choose real providers — nothing here needs a paid key to
  work or be tested)
- give you a profile with real privacy controls: show "Kawangware," just
  "Nairobi," or nothing at all — your choice, enforced in code
- install on an Android phone's home screen straight from the browser — no
  app store, no big download — and keep working offline for saved pages
- speak **English and Kiswahili**, switchable anytime
- take your photo uploads and quietly do two jobs: strip the hidden
  location tag phones embed in pictures, and squeeze the file small so it
  costs less to share

## Where the ideas come from — not vibes

- **The route map starts from real open data** — Digital Matatus, a project
  where university teams rode and mapped about 135 Nairobi matatu routes,
  publicly and for free. We treat it as a starting sketch the community
  corrects, never gospel.
- **The bundle budgets come from actual carrier prices** — real Safaricom
  and Airtel bundle costs set the page-size limits the exams enforce.
- **The privacy rules come from Kenya's Data Protection Act (2019)** —
  consent you can prove, data kept only as long as needed, deletion on
  request.
- **We studied what already exists** — booking apps, sacco fleet tools,
  traffic feeds — and deliberately built the missing layer: the community
  around all of it.

(Full sources and links: `docs/research-notes.md`.)

## What's next

Step 8: the guides — "how to do things, for here" — with mandatory safety
warnings on anything medical, electrical, or structural. Then the market and
barter. Then the matatu pilot.

## Want in?

- **SACCOs and operators** — we need 3–5 willing to pilot tracking in Nairobi
- **Commuters** — 15–20 short interviews to check our priorities (if research
  says transport is the burning need, the build order flips — the recipe book
  was written to make that flip cheap)
- **Technical partners** — the contract, research, and current state are all
  in this repo, in plain view

**Contact:** [your name, email or phone]

## For technical readers

`PROMPTS.md` is the build contract: 42 self-contained, testable prompts, one
commit each. `docs/` holds the product brief, research notes with sources,
and architecture decision records; `Project_state.md` is the live status.
Stack in one line: Django + PostGIS-backed PostgreSQL, server-rendered PWA,
mock-first adapters, everything tested (83 passing). Clone, `make setup`,
`make db-start`, `make test`.

## License

Proprietary — all rights reserved while co-founder and partner conversations
are underway.

*“Mtaani Connect” is a working name; the codebase doesn't care what it's
called.*