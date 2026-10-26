# Mtaani Connect — Product Brief
## Kenya Community Super-App

**Prepared for:** Development partners, co-founders, investors, and technical contractors
**Version:** 1.0
**Market:** Kenya (pilot: Nairobi → national → Africa)

---

## 1. What We Are Building

A single, unified Kenyan super-app that combines transport intelligence, community knowledge, social networking, and local commerce. It must solve everyday problems for ordinary Kenyans using the phones and data they already have.

**Working name:** Mtaani Connect (proposed — open to change)

**The core promise:** Help people move efficiently, solve practical problems, earn money, exchange value, and build stronger communities — all in one app that feels simple and trustworthy.

---

## 2. The Problems We Solve

| Problem | Current Reality | Our Solution |
|---------|----------------|--------------|
| **Waiting blindly for matatus** | Passengers stand at stages with no idea if their vehicle is coming, delayed, or rerouted | Real-time tracking + arrival estimates + disruption alerts |
| **Poor local knowledge access** | Online recipes and guides use ingredients or tools unavailable in rural Kenya | Community guides + AI that adapts solutions to locally available resources |
| **Fragmented local commerce** | Sellers rely on WhatsApp status and word of mouth | Integrated marketplace + skills listings + barter system |
| **No trusted barter platform** | People swap goods informally with no recourse if deals go wrong | Structured barter with negotiation, reviews, and dispute reporting |
| **Social networks that don't serve community utility** | Existing platforms optimize for ads and addiction, not local problem-solving | Feed prioritizes usefulness, local relevance, and trust over engagement bait |

---

## 3. Seven Core Modules

### Module 1: Matatu Tracking (Flagship Feature)
- Search by nickname, route, SACCO, stage, or destination
- Interactive map showing live vehicle locations
- Follow favourite matatus and get "approaching stage" alerts
- Confidence indicators distinguishing live GPS, community reports, and estimates
- Clear handling when GPS data is unavailable (no fabricated locations)
- Multiple location sources: authorized vehicle GPS, driver/conductor app, operator dashboard, verified passenger reports

**Critical non-functional requirement:** Design for intermittent connectivity, battery conservation, and limited data.

### Module 2: Traffic & Route Intelligence
- Route planning between estates, stages, landmarks
- Alternative routes during congestion
- Community reports for road closures, flooding, accidents
- Distinguish official data from estimates and crowd reports

### Module 3: Local Knowledge & Practical Guides (WikiHow for Kenya)
- Step-by-step guides searchable by category, location, and skill level
- Categories: cooking with local ingredients, farming, repairs, business, health education, crafts, transport
- AI assistant that asks what resources the user has, then adapts instructions using locally available substitutes
- Offline saving for selected guides
- AI answers clearly labelled; community verification encouraged
- Safety warnings for medical, electrical, and construction advice

### Module 4: Social Networking (Utility-First)
- Profiles with skills, interests, location (privacy-controlled)
- Posts, photos, short videos, educational content
- Follow users, groups, experts, sellers
- Groups by interest, neighbourhood, profession
- Robust reporting, blocking, and content moderation
- Feed balances followed accounts + local relevance + usefulness (not addiction optimization)

### Module 5: Marketplace (Goods & Services)
- New, second-hand, handmade, farm produce, electronics, furniture, tools
- Services: tutoring, repairs, coding, tailoring, catering, photography, farming services
- Search, filters, portfolio, reviews, broad location only

### Module 6: Barter & Cash-Plus-Exchange (Key Differentiator)
- Exchange items or services without full cash payment
- Cash-only, barter-only, or cash-plus-barter offers
- Multi-item offers, counteroffers, negotiation threads
- Mutual reviews, dispute reporting, fraud prevention
- App does NOT hold money or custody of items in V1
- Future path to M-Pesa integration for cash components

### Module 7: Messaging & Communication
- One-to-one, marketplace enquiries, barter negotiations, group chats
- Photo sharing, read receipts, spam controls
- No exposure of private phone numbers or precise locations without explicit consent
- Evaluate end-to-end encryption for private messaging

---

## 4. Target Users & Devices

**Primary users:** Kenyans in urban and peri-urban areas who use matatus, buy/sell locally, and seek practical knowledge

**Devices:** Android first (low-cost smartphones), responsive web, iOS later

**Primary languages:** English and Kiswahili initially; Sheng and other Kenyan languages in future phases

**Connectivity:** Must function acceptably on 3G, limited data bundles, and intermittent connectivity

---

## 5. Trust, Safety & Privacy (Non-Negotiable)

- Secure auth with email/phone verification; account recovery
- Optional seller identity verification (not mandatory for ordinary users)
- Role-based permissions
- Rate limiting, abuse prevention, spam/scam detection
- Community reporting + moderation tools
- Reputation, reviews, and dispute workflows
- Kenya Data Protection Act compliance
- **Transport:** Only authorized, active tracking sessions are public. Never expose driver home locations or personal movements outside authorized trips.
- Data minimization and privacy-by-design throughout

---

## 6. Technology Principles

- Mobile-first, responsive, fast on affordable phones
- Compressed images, adaptive video, pagination, lazy loading, offline indicators
- Modular monolith initially (avoid premature microservices)
- PostGIS for geographic data
- Modular boundaries between transport, social, knowledge, marketplace, barter, messaging, AI
- Database migrations, API versioning, automated testing, secrets management
- AI abstraction layer supporting multiple providers and local models
- Documented architectural decisions

---

## 7. Roadmap (Build Sequence)

| Phase | Focus | Goal |
|-------|-------|------|
| **1** | Foundation | Auth, profiles, design system, repo, basic navigation |
| **2** | Community MVP | Posts, Q&A, guides, search, categories, basic moderation |
| **3** | Commerce MVP | Marketplace listings, search, barter offers/counteroffers, reviews |
| **4** | Transport Pilot | Route/stage DB, vehicle registration, authorized tracking, live map, pilot with willing operators in Nairobi |
| **5** | Intelligence Layer | Traffic routing, AI local adaptation, better recommendations, multilingual support |
| **6** | Scale & Monetize | Performance, analytics, more towns, business tools, fraud detection, partnerships |

**Important:** This sequence is validated by user demand. If research shows transport is more urgent than social, we flip Phases 2 and 4.

---

## 8. Business Model (Sustainable, Not Extractive)

- Keep essential community access affordable/free
- Revenue streams (phased): promoted listings, merchant subscriptions, professional profiles, transparent advertising, premium transport tools for SACCOs, fleet analytics, optional transaction services, premium AI features
- Do not monetize before demonstrating user demand
- Estimate and manage third-party API costs (maps, traffic, AI) carefully

---

## 9. What This Is NOT

- NOT a concept deck — we need working, production-quality software
- NOT a matatu-only app — transport is one module of seven
- NOT an e-commerce clone — barter and skills exchange are core differentiators
- NOT a social network optimized for addictive scrolling — utility and trust come first
- NOT a platform that fabricates data — if tracking is unavailable, say so clearly
- NOT a financial institution — app does not hold money in V1

---

## 10. Immediate Next Steps (What We Need From Partners)

1. **Architecture review** — validate tech stack choices for Kenyan conditions
2. **User research** — interview 15–20 potential users in Nairobi to confirm priority of modules
3. **Transport operator conversations** — identify 3–5 SACCOs willing to pilot tracking
4. **Cost modeling** — map out third-party API, hosting, and bandwidth costs at 10K, 100K, and 1M users
5. **Prototype** — build Phase 1 (auth + profiles + navigation) and a clickable demo of Phase 2
6. **Legal review** — ensure compliance with Kenya Data Protection Act, consumer protection, and electronic transaction regulations

---

## 11. Key Assumptions to Validate

- Users will share location data via driver/conductor app if battery and data costs are low enough
- Enough operators will authorize access to make live transport data useful
- People will trust a barter platform without escrow in the first version
- The AI local-adaptation feature adds genuine value, not novelty
- A super-app model beats separate specialized apps for Kenyan users

---

**Contact:** [Your name and contact information]
**Status:** Seeking technical partners and pilot operators for Phase 1 development
