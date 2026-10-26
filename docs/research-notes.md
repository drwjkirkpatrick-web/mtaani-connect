# Research Notes — How the Prompt Set Is Grounded

Research done 2026-10-25 while writing `PROMPTS.md`. Each source lists what it
changed in the build contract.

## 1. Route data: Digital Matatus GTFS (open data)

**Sources:**
- Digital Matatus Project — https://digitalmatatus.com/about.html
- MIT Civic Data Design Lab — https://civicdatadesignlab.mit.edu/Digital-Matatus
- Klopp et al., *Journal of Transport Geography* (2015) via
  https://preparecenter.org/resource/the-openmatatus-project-collaborative-research-and-mapping-for-nairobis-informal-public-transit/
- Wired (2015) — https://www.wired.com/2015/08/nairobi-got-ad-hoc-bus-system-google-maps/

**Facts:** University of Nairobi + Columbia + MIT mapped ~135 matatu routes and
~3,000 stops, released as a modified GTFS (with a "continuous stops" field for
flexible boarding). Uploaded to Google Maps in 2015 — first semi-formal system
integrated there. Data updated summer 2018 (round 3); openly downloadable.

**Impact on prompts:** Prompt 25 seeds the route/stage database from this GTFS
with a community-correction queue; the 2018 vintage is treated as seed data
only, never truth. The modified-GTFS quirk (missing calendars, variable fares)
is why `Fare` is a labeled *range* with a `Confidence` enum, not a fixed price.

## 2. Legal: Kenya Data Protection Act 2019 + ODPC guidance

**Sources:**
- DPA 2019 full text — https://www.odpc.go.ke/wp-content/uploads/2024/02/TheDataProtectionAct__No24of2019.pdf
- Data Protection (General) Regulations 2021 —
  https://www.odpc.go.ke/wp-content/uploads/2024/03/THE-DATA-PROTECTION-GENERAL-REGULATIONS-2021-1.pdf
- ODPC Guidance Note, Communication Sector —
  https://www.odpc.go.ke/wp-content/uploads/2024/02/ODPC-Guidance-Note-for-the-Communication-Sector.pdf
- ODPC FAQs (registration duty) — https://www.odpc.go.ke/faqs/

**Facts:** Registration with the Data Commissioner is mandatory (§18).
Controller bears the burden of proof for consent (§32); consent must be
express, specific, informed, freely given, withdrawable. Collection notice
duties (§29). Data minimization is defined operationally (Reg 33: avoid /
limit / demonstrate / pseudonymize / delete). Children's data needs guardian
consent + age-verification mechanisms. DPIA is mandatory for high-risk
processing — live location tracking qualifies.

**Impact on prompts:** Prompt 04 logs `ConsentEvent`s (version + timestamp +
IP) at signup; Prompt 40 builds the data inventory, subject access/export,
anonymizing deletion, retention jobs, and the tracking DPIA; Prompt 27's
session guardrails are written as the DPIA mitigations. ODPC registration is
kept as an explicit *human* launch task, not automated.

## 3. Economic context: data bundles & devices

**Sources:**
- TechCabal, "Best data bundles in Kenya" (Aug 2025) —
  https://techcabal.com/2025/08/13/best-data-bundles-in-kenya-august-2025/
- Airtel Kenya bundle page — https://www.airtelkenya.com/internet-amazing-data-bundle
- Gates Foundation / Qhala report on Africa's smartphone divide (2025) —
  https://blog.qhala.com/wp-content/uploads/2025/12/Gates-Report_-Bridging-Africas-Smartphone-Divide_-Insights-from-10-Countries-Compressed.pdf

**Facts:** Daily bundles start near 70 MB for KES 10 (Airtel); 1 GB daily is
KES 50–100 across carriers; across Africa a 2 GB bundle ≈ 3.9% of average
monthly income — above the affordability benchmark. Expiring prepaid bundles
discourage bandwidth-heavy apps; devices are low-end Android.

**Impact on prompts:** Hard page-weight budgets (≤50 KB pages, ≤40 KB images,
≤20-item API pages) are enforced by tests (Prompt 02 util, Prompt 39 audit);
the service worker caches guides and map tiles (05, 09, 28); the crew flow
(29) batches and counts its own data usage visibly.

## 4. Transport-ops landscape: SACCOs, GPS economics, competitors

**Sources:**
- McTaba, "How Boda and Matatu Saccos Use Software" —
  https://www.mctaba.com/learn/african-stack/boda-matatu-sacco-software
- PandaMatatu (SACCO ops platform) — https://www.pandamatatu.com/
- Waah (passenger tracking/booking app) — Google Play listing
- Ranatec SACCO tracking proposal — https://ranatec.co.ke/pdfs/Matatu_Sacco_Vehicle_Tracking_Proposal_Ranatec.pdf
- LinkedIn posts on MatatuOS / Hopln (engineering-reality write-ups)

**Facts:** GPS trackers cost KES 3,000–8,000 per vehicle plus data plans;
smaller SACCOs prioritize money tracking over location; larger ones buy
fleet dashboards. Existing apps cover single purposes: booking + tracking
(Waah, partnered with SACCOs like Super Metro), SACCO fleet/ticketing
(PandaMatatu), crowdsourced traffic (Ma3Route), GTFS journey planning (Hopln).
Practitioners name offline-first architecture, stage-based boarding, and
conductor-friendly UX as the core engineering challenges.

**Impact on prompts:** Confirms the brief's multiple-sources design —
driver/conductor app reporting (29) works without bought hardware, with
authorized GPS as an upgrade path; the pilot targets SACCOs already disposed
to tracking (26); differentiation is the community super-app layer no
single-purpose competitor covers. Fleet tools appear only as premium hooks
(38), matching what SACCOs demonstrably pay for.

## 5. What was NOT found (and thus not promised)

- No open, current, maintained GTFS feed — the 2018 data is the best public
  seed. Hence "seed + community corrections," never a fake live network.
- No evidence that barter-without-escrow trust scales (brief §11 assumption) —
  Prompt 21/22/23 build the reputation/dispute machinery to test it, and the
  brief's assumption list stays open.