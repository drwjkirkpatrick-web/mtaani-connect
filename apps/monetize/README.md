# apps/monetize — Monetization Hooks, OFF by default (PROMPTS.md Prompt 38)

Scaffolding only. Acceptance: **monetization is armed but pointed at
nothing until the team flips flags deliberately.** No payments processing —
billing is a future prompt once demand is proven. The feed's scoring
(Prompt 11) is never imported here; search's own ordering (Prompt 19) is
never changed here either.

## Library contract (what OTHER code may consume)

- `apps.monetize.ratio.interleave(organic_rows, promoted_rows, *, ratio=6)`
  — pure; weaves ≤1 promoted after every `ratio` organic rows; promoted
  rows MUST be payload mappings carrying `promoted_en` + `promoted_sw`
  (non-empty) — unlabeled paid rows cannot pass; promoted never adjacent;
  never rendered into an empty organic stream; inputs never mutated.
- `apps.monetize.ratio.SLOT_RATIO = 6` — the ≤1-per-6 constant.
- `apps.monetize.labels.PROMOTED_LABEL_EN / PROMOTED_LABEL_SW` —
  `"Promoted"` / `"Imeandikwa"`, the defaults stored on every placement row.
- `apps.monetize.features.features(tier) -> MerchantFeatureSet` (plus
  `portfolio_size_cap`, `refresh_cadence_hours`) — FREE = 8 active rows /
  168h refresh; PRO = 25 / 24h; unknown tier degrades to FREE (never a
  grant).
- `apps.monetize.services.*` — flips (`flip_flag`, `flip_placement`,
  `set_merchant_tier`, `flip_sacco_tool`), registrations (`promote_listing`,
  `enroll_merchant`, `register_sacco_tool`), reads (`is_flag_on`,
  `effective_merchant_features`, `build_search_inline_promoted`,
  `sacco_payload_doc`, `audit_rows_for`). Flips require a STAFF actor and
  write one `MonetizationAudit` row each; no-change flips write none.

## SEAM for the search-results consumer (parent to wire — monetize won't)

The promoted library is deliberately NOT wired into any of this app's own
pages. To land promoted inline slots on the market search page (Prompt 19),
the parent applies exactly two small patches; 0 of the listings files are
touched by monetize, and 19's test pins (`tests/test_listing_search.py`,
incl. `promoted_applied is False`) stay green:

1. `apps/listings/views.py` — `listing_search` (def at line 376):
   after `search_listings(...)`/pagination build, feed the render context's
   `results` list (render call at lines 406-421; the literal results value
   is built at line 410) through the library, e.g.:
   ```python
   from apps.monetize import services as monetize_services
   # inside listing_search, before render:
   results = monetize_services.search_inline(list(page_obj.object_list))
   ```
   (Off-by-default: with the flag off the rows pass through untouched, so
   this patch is a no-op until staff flip `promoted_search_inline`.)
2. `apps/listings/templates/listings/search.html` — the results loop
   (`{% for listing in results %}` at line 66, card block lines 67-78):
   branch promoted payload rows out and render THE STORED LABEL verbatim:
   ```jinja
   {% if item.promoted_en %}
   <li class="card promoted">
     ...{{ item.listing.title }}...
     <span class="chip chip promoted">{{ item.promoted_en }} / {{ item.promoted_sw }}</span>
   </li>
   {% else %}
   ...existing organic card...
   {% endif %}
   ```
   (Loop variable rename `listing` → `item` inside the parent's patch.)

## Flags + audit schema summary

- `monetize.FeatureFlag`: key (unique), label + label_sw (en+sw copy),
  enabled (schema default FALSE), enabler (FK→staff user, SET_NULL, NULL
  until enabled), enabled_at (NULL until enabled).
- `monetize.MonetizationAudit`: surface (FLAG/PLACEMENT/TIER/SACCO_TOOL) +
  action (ENABLED/DISABLED), actor (FK, PROTECT, NULL-able), generic target
  (ContentType+object_id, allow-list = monetize's own models only), note,
  created_at (the "at"). Append-only: ORM update/delete raise; admin is
  view-only. ONE per real flip, from any surface (page/admin/services).
- `monetize.PromotedPlacement`: listing (FK→"listings.Listing" public
  model), slot (SEARCH_INLINE only in v1), active (default FALSE),
  promoted_en/promoted_sw stored label copy, enabler/enabled_at.
- `monetize.MerchantEnrollment`: user (OneToOne), tier (FREE/PRO, default
  FREE), tier_set_by/tier_set_at (NULL until a staff PRO grant).
- `monetize.SaccoPremiumTool`: sacco (FK→"routes.Sacco", same model chain
  fleet uses), tool (SESSION_ANALYTICS only in v1), label_en/label_sw,
  enabled (default FALSE), enabler/enabled_at.

## I18N

No gettext markers exist in this app: the project sw catalog under
`locale/` is a shared file monetize does not own, and its drift detector
(apps.core.i18n, enforced suite-wide) fails the suite on any new code
msgid. Every monetization surface ships its bilingual copy as DATA — the
stored en+sw CharFields above, `labels.py` constants, and bilingual inline
template copy (the repo's "Saved. / Imehifadhiwa." style).
