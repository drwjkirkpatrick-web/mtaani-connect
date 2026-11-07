"""inventory.py — the HAND-MAINTAINED personal-data inventory (Prompt 40).

WHAT this is: the machine-readable half of the DataMap. ``INVENTORY`` is a
human-written dict listing every project model that holds personal data,
its personal-data FIELDS, purpose, lawful basis (the DPA vocabulary:
CONSENT / CONTRACT / LEGITIMATE_INTEREST / LEGAL_OBLIGATION), retention
days (None = follows the documented deletion trade-off) and a note. It is
upserted into DataMapEntry by services.load_inventory() and AUDITED in
both directions by tests/test_compliance.py:

  * registry-side drift — a DataMapEntry pointing at a model that no
    longer exists, or at fields the model does not have — fails;
  * inventory-side drift — a model with a personal-data field missing
    from the inventory — fails the PII SCAN AUDIT.

METHODOLOGY (how the fields below were found, and how to re-find them):

  1. Reflection dump: every concrete field of every model of every
     project app was dumped and read app by app (this was done by hand,
     and the scan audit re-checks the conclusion every run).
  2. The scan flag set is deliberately TIGHT to avoid false positives
     (PROMPTS.md: "pick the flag list carefully"). ``SCAN_FLAG_FIELDS``
     below holds only field NAMES that always mean personal data when
     they appear — contact (email/phone/target), identity
     (username/username_norm/first_name/last_name/display_name),
     credentials/tokens (password/token_hash — sha256-hashed tokens are
     pseudonymous at rest and STILL personal data; that is the point of
     the audit), network (ip/source_ip/ip_address) and the profile
     avatar FK. A prose field like ``note`` or ``description`` is NOT in
     the scan set — it cannot be classified by name alone — so content
     rows below are hand-listed instead (HONEST scope split, documented).
  3. Content fields (posts.text, messaging Message.body, reviews.text,
     disputes narratives, profile bio, AssistantRun have/need text…) are
     personal data too — they are hand-listed in INVENTORY with their
     own purpose/basis rows. Public prose (a published post) is personal
     but public; its handling is the deletion trade-off, not this scan.
  4. RELATIONSHIP/linkage fields (user FKs: author/sender/reporter/
     follower…) are hand-listed rows — the FK column holds an ID that
     resolves to a person, so the table processes personal data by
     linkage.

DOCUMENTED EXCLUSIONS (models/fields NOT treated as personal data):

  * apps.analytics — aggregate tables are structurally incapable of
    holding user data (Prompt 37: schema-privacy test; columns are
    {date, metric, value} / {date, sacco_id, sacco_name, value}).
  * ai.AICall.tokens_in/tokens_out — LLM usage counters, not auth
    tokens; AICall also carries NO user FK by design (KDPA
    minimization for assistant infrastructure).
  * messaging.Message.is_phone_flagged — a safety BOOLEAN about content,
    holds no phone number.
  * routes/fleet business data — plates, SACCO names, GTFS ids, stages,
    fares: business data (the tracking app's KDPA note splits business
    from identity; fleet.CrewMember.user and SaccoAdmin.user ARE
    personal linkage and ARE listed).
"""
from __future__ import annotations

# Fields that, by their name alone, must mean personal data. The PII
# scan audit walks every project model and demands every field found
# here be covered by an INVENTORY row of the same (app, model).
SCAN_FLAG_FIELDS = frozenset({
    "username",
    "username_norm",  # accounts.LoginAttempt (normalised username)
    "first_name",
    "last_name",
    "email",
    "phone",
    "password",       # credential (hashed at rest) — still personal data
    "token_hash",     # sha256 pseudonymous credential — INCLUDED on purpose
    "ip",
    "ip_address",
    "source_ip",
    "avatar",         # FK to media owned by the person
    "display_name",
    "target",         # accounts.VerificationCode.target = email or E.164
})

# The lawful-basis short keys used inside INVENTORY (validated against
# the DataMapEntry enum at load time; typos fail loudly in the loader).
_C = "CONTRACT"
_CO = "CONSENT"
_LI = "LEGITIMATE_INTEREST"
_LO = "LEGAL_OBLIGATION"

# retention: N days -> swept when older (retention.py); None -> kept while
# the account lives, then per the deletion trade-off (services.anonymize_account).
RET_CONSENT_LEDGER_DAYS = 2555  # 7 years: KDPA consent-proof horizon (legal obligation)

INVENTORY: list[dict] = [
    # -------------------------------------------------------- compliance
    {
        "app": "compliance", "model": "DataRequest",
        "fields": ["user", "kind", "status", "handled_by", "handled_at", "note"],
        "purpose": "The DSAR ledger: the paper trail proving each "
                   "access/deletion/correction request was asked and answered.",
        "basis": _LO, "retention": 90,
        "note": "Handled rows age out 90 days after the decision (OPEN ones "
                "are never swept). DataMapEntry itself holds NO user data "
                "(columns-purity test) and so is described only by its "
                "module docstring.",
    },

    {
        "app": "accounts", "model": "User",
        "fields": ["username", "first_name", "last_name", "email", "phone",
                   "password", "last_login", "email_verified_at",
                   "phone_verified_at"],
        "purpose": "Account identity and contact channels so the member can "
                   "sign in, be verified, and be recoverable.",
        "basis": _C, "retention": None,
        "note": "Deletion trade-off: on a DELETE request the identity is "
                "anonymized in place (username -> anon-sha256, names/email/"
                "phone blanked, password made unusable, account deactivated); "
                "the row survives so audit PKs keep resolving.",
    },
    {
        "app": "accounts", "model": "ConsentEvent",
        "fields": ["purpose_key", "doc_version", "source_ip", "created_at"],
        "purpose": "KDPA §32 consent proof: who consented, to which document "
                   "version, when, from where — the burden the controller bears.",
        "basis": _LO, "retention": RET_CONSENT_LEDGER_DAYS,
        "note": "Never deleted at account anonymization (legal obligation); "
                "aged rows are pruned only after the 7-year ledger horizon.",
    },
    {
        "app": "accounts", "model": "VerificationCode",
        "fields": ["target", "code_hash", "expires_at", "used_at", "created_at"],
        "purpose": "Signup/login OTP delivery and single-use verification.",
        "basis": _C, "retention": 1,
        "note": "Cutoff = expires_at + 1 day grace. Codes are hashed at rest; "
                "not FK'd to a user, so they expire with the account contact "
                "unchanged and are swept within days.",
    },
    {
        "app": "accounts", "model": "RecoveryToken",
        "fields": ["token_hash", "expires_at", "used_at", "created_at"],
        "purpose": "Account recovery for a member locked out.",
        "basis": _C, "retention": 1,
        "note": "Cutoff = expires_at + 1 day grace (1-hour token life by rule).",
    },
    {
        "app": "accounts", "model": "LoginAttempt",
        "fields": ["username_norm", "ip", "succeeded", "created_at"],
        "purpose": "Brute-force lockout bookkeeping (5 fails -> 15-minute lock "
                   "per user+IP) — abuse resistance only.",
        "basis": _LI, "retention": 90,
        "note": "KDPA minimization: no user-agent, no device fingerprint.",
    },
    # ----------------------------------------------------------- profiles
    {
        "app": "profiles", "model": "Profile",
        "fields": ["display_name", "bio", "skills", "interests", "ward",
                   "location_visibility", "field_visibility",
                   "precise_location_consent", "avatar", "is_verified_seller"],
        "purpose": "Voluntary profile presentation the member writes and "
                   "controls (self-serve correction is profiles:me).",
        "basis": _CO, "retention": None,
        "note": "On deletion: display_name/bio/skills/interests/ward blanked, "
                "avatar unlinked, precise_location_consent unset.",
    },
    # -------------------------------------------------------------- media
    {
        "app": "media", "model": "MediaRef",
        "fields": ["owner", "purpose", "standard_path", "thumb_path"],
        "purpose": "Hosting the images members upload (incl. avatars).",
        "basis": _C, "retention": None,
        "note": "Rows are path/size records; blobs stay on disk under the "
                "documented content trade-off. On deletion the avatar FK on "
                "Profile is unlinked; owner FK keeps resolving to the anon row.",
    },
    # ------------------------------------------------------- guides (07)
    {
        "app": "guides", "model": "Guide",
        "fields": ["created_by", "title_en", "title_sw", "safety_warning"],
        "purpose": "Authorship attribution on the shared community how-to "
                   "library.",
        "basis": _LI, "retention": None,
        "note": "Guides are SHARED community assets: on account deletion they "
                "are deliberately NOT removed (removal would gut the tool "
                "every next reader needs); attribution resolves to the anon "
                "row. Documented trade-off, not an oversight.",
    },
    {
        "app": "guides", "model": "OfflineSavedGuide",
        "fields": ["user"],
        "purpose": "Reading list of which account saved which guide offline.",
        "basis": _C, "retention": None,
    },
    {
        "app": "guides", "model": "GuideConfirmation",
        "fields": ["user", "ward", "note"],
        "purpose": "\"I used this guide\" confirmations, optionally ward-tagged.",
        "basis": _CO, "retention": None,
    },
    # --------------------------------------------------------- posts (10)
    {
        "app": "posts", "model": "Post",
        "fields": ["author", "text"],
        "purpose": "Members publishing teachposts to the local feed.",
        "basis": _CO, "retention": None,
        "note": "Public prose (personal but public). On deletion the status "
                "flips to REMOVED via moderation.services.apply_removal (the "
                "rows remain as moderation evidence).",
    },
    # ------------------------------------------------------------ qa (11)
    {
        "app": "qa", "model": "Question",
        "fields": ["asked_by", "body"],
        "purpose": "Members asking the community for help.",
        "basis": _CO, "retention": None,
        "note": "Removed via the moderation door on deletion (status flip).",
    },
    {
        "app": "qa", "model": "Answer",
        "fields": ["author", "body"],
        "purpose": "Members answering.",
        "basis": _CO, "retention": None,
        "note": "No status column (Answer is a direct reply): on deletion the "
                "row REMAINS with an anonymous author — same trade-off as the "
                "shared guide assets; a thread's answers stay coherent and "
                "any later abuse review still has the text.",
    },
    {
        "app": "qa", "model": "AnswerVote",
        "fields": ["user"],
        "purpose": "Which member endorsed which answer.",
        "basis": _CO, "retention": None,
    },
    # --------------------------------------------------------- follows
    {
        "app": "follows", "model": "Follow",
        "fields": ["follower", "followee"],
        "purpose": "The member's social graph.",
        "basis": _CO, "retention": None,
    },
    # ------------------------------------------------------------ groups
    {
        "app": "groups", "model": "Group",
        "fields": ["owner", "name", "ward"],
        "purpose": "Community group ownership/contact context.",
        "basis": _C, "retention": None,
        "note": "Groups survive a deletion (communal entity); ownership "
                "resolves to the anon row.",
    },
    {
        "app": "groups", "model": "GroupMembership",
        "fields": ["user", "role"],
        "purpose": "Membership of the account.",
        "basis": _CO, "retention": None,
    },
    # -------------------------------------------------------- moderation
    {
        "app": "moderation", "model": "Report",
        "fields": ["reporter", "note", "decision_note", "target_id"],
        "purpose": "Dispute/abuse evidence: what was reported, by whom, "
                   "decided by whom.",
        "basis": _LO, "retention": None,
        "note": "PRESERVED THROUGH DELETION (KDPA evidence retention vs "
                "erasure balance — the Report row is the record a later "
                "dispute needs).",
    },
    {
        "app": "moderation", "model": "Block",
        "fields": ["blocker", "blocked"],
        "purpose": "Personal content curation (who hides whom) — kept per side.",
        "basis": _LI, "retention": None,
    },
    {
        "app": "moderation", "model": "SuspensionDraft",
        "fields": ["target", "drafted_by", "confirmed_by", "reason"],
        "purpose": "Two-actor suspension gate record with its written reason.",
        "basis": _LI, "retention": None,
        "note": "PRESERVED through deletion (enforcement evidence).",
    },
    {
        "app": "moderation", "model": "ModeratorAudit",
        "fields": ["actor", "target_id", "reason"],
        "purpose": "Append-only moderation audit trail (who did what, why).",
        "basis": _LO, "retention": None,
        "note": "Append-only by model contract. After anonymization the audit "
                "rows still resolve to an actor id whose account row reads "
                "anon-* — the trail stays intact, names do not.",
    },
    # ------------------------------------------------------------ search
    {
        "app": "search", "model": "SearchDocument",
        "fields": ["author", "label", "body"],
        "purpose": "Search index mirroring published content and public "
                   "profile allow-list fields.",
        "basis": _LI, "retention": None,
        "note": "Maintained by Postgres TRIGGERS — when content status flips "
                "or the profile row changes during anonymization, the index "
                "rebuilds itself; docs written only from public allow-list "
                "fields (emails/phones/coordinates can never land here).",
    },
    # -------------------------------------------------------------- trust
    {
        "app": "trust", "model": "HelpfulVote",
        "fields": ["voter", "owner"],
        "purpose": "Peer reputation votes (who vouched for whom).",
        "basis": _CO, "retention": None,
    },
    {
        "app": "trust", "model": "BadgeEvent",
        "fields": ["user"],
        "purpose": "Reputation badges earned by the account.",
        "basis": _CO, "retention": None,
    },
    # -------------------------------------------------------- listings
    {
        "app": "listings", "model": "Listing",
        "fields": ["seller", "title", "title_sw", "description",
                   "description_sw", "ward", "price"],
        "purpose": "Member-to-member trade offers in the marketplace.",
        "basis": _CO, "retention": None,
        "note": "Removed via the moderation door on deletion; a CONCLUDED "
                "listing referenced by a dispute/offer keeps its row as "
                "trade evidence.",
    },
    # -------------------------------------------------------- messaging
    {
        "app": "messaging", "model": "Thread",
        "fields": ["created_by", "title"],
        "purpose": "Identifying a conversation (title is the origin object's "
                   "label, e.g. the listing).",
        "basis": _C, "retention": None,
        "note": "Threads survive deletion: threads are joint personal data of "
                "every participant (the other members still need their "
                "history).",
    },
    {
        "app": "messaging", "model": "ThreadParticipant",
        "fields": ["user", "muted", "show_receipts"],
        "purpose": "Per-participant chat settings.",
        "basis": _C, "retention": None,
    },
    {
        "app": "messaging", "model": "Message",
        "fields": ["sender", "body"],
        "purpose": "Private correspondence between members.",
        "basis": _C, "retention": None,
        "note": "DELETION TRADE-OFF: a message's body is shared by ALL thread "
                "participants (joint personal data); bodies are preserved "
                "and the author FK resolves to the anon row. Erasing one "
                "member's half of a shared conversation would falsify the "
                "others' records.",
    },
    # ----------------------------------------------------------- services
    {
        "app": "services", "model": "ServiceListing",
        "fields": ["provider", "title", "description", "ward", "rate_amount"],
        "purpose": "Skills/services offered by a provider.",
        "basis": _CO, "retention": None,
        "note": "Removed via the moderation door on deletion.",
    },
    # ------------------------------------------------------------- barter
    {
        "app": "barter", "model": "Offer",
        "fields": ["initiator", "counterpart", "cash_amount"],
        "purpose": "Trade/negotiation offers — the record of a deal in flight.",
        "basis": _C, "retention": None,
        "note": "Financial-adjacent transaction evidence: PRESERVED through "
                "deletion (disputes reference these deals; barter rows are "
                "an OFFER history, the account's identity within it "
                "anonymizes).",
    },
    # ------------------------------------------------------------ reviews
    {
        "app": "reviews", "model": "DealReview",
        "fields": ["reviewer", "reviewed", "text", "dispute_note"],
        "purpose": "Marketplace integrity: post-deal reputation of the "
                   "counterparty.",
        "basis": _LI, "retention": None,
        "note": "PRESERVED through deletion (deal evidence about the OTHER "
                "party's conduct as well).",
    },
    # ------------------------------------------------------------ disputes
    {
        "app": "disputes", "model": "DisputeCase",
        "fields": ["opener", "what_promised", "what_happened",
                   "decided_by", "decided_note"],
        "purpose": "Deal-dispute record with both sides' account and the "
                   "decision.",
        "basis": _LO, "retention": None,
        "note": "PRESERVED through deletion (dispute evidence, incl. the "
                "counterparty's rights).",
    },
    {
        "app": "disputes", "model": "EvidenceAttachment",
        "fields": ["added_by", "text", "thread_id", "offer_id"],
        "purpose": "Evidence a party attached to their dispute.",
        "basis": _LO, "retention": None,
    },
    {
        "app": "disputes", "model": "DisputeProposal",
        "fields": ["proposed_by", "confirmed_by", "note", "target_id"],
        "purpose": "Two-actor dispute resolution gate.",
        "basis": _LO, "retention": None,
    },
    # ------------------------------------------------------------- routes
    {
        "app": "routes", "model": "CorrectionQueue",
        "fields": ["opened_by", "note"],
        "purpose": "Community corrections to route/fare data with authorship.",
        "basis": _CO, "retention": None,
    },
    # --------------------------------------------------------- fleet (26)
    {
        "app": "fleet", "model": "CrewMember",
        "fields": ["user", "role", "approved_by", "revoked_by"],
        "purpose": "Crew authorisation: who may run which vehicle "
                   "(the authority chain for live tracking).",
        "basis": _C, "retention": None,
        "note": "On deletion seats are NOT revoked by code on the operator's "
                "behalf (fleet's public door requires the SACCO admin's "
                "hand); the anonymized account is deactivated, so all crew "
                "authority is inert. Documented operator follow-up.",
    },
    {
        "app": "fleet", "model": "SaccoAdmin",
        "fields": ["user", "added_by"],
        "purpose": "Operator authorisation for SACCO management surfaces.",
        "basis": _C, "retention": None,
        "note": "Same inert-after-deactivation reasoning as CrewMember.",
    },
    {
        "app": "fleet", "model": "FleetAudit",
        "fields": ["actor", "crew", "note"],
        "purpose": "Operator audit trail (fleet side): who registered/"
                   "approved/revoked what.",
        "basis": _LO, "retention": None,
        "note": "Audit rows survive with actor ids intact — resolvable to an "
                "anonymous account after deletion, exactly like "
                "ModeratorAudit.",
    },
    # ------------------------------------------------------------ crowd
    {
        "app": "crowd", "model": "CrowdReport",
        "fields": ["reporter", "note", "destination_hint"],
        "purpose": "Crowdsourced stage/route congestion reports.",
        "basis": _CO, "retention": None,
    },
    {
        "app": "crowd", "model": "CrowdReporterLink",
        "fields": ["rider"],
        "purpose": "Attribution chain keeping merges honest.",
        "basis": _CO, "retention": None,
    },
    # --------------------------------------------------------- tracking (27)
    {
        "app": "tracking", "model": "TrackingSession",
        "fields": ["token_hash", "authorized_by", "cap_hours", "ends_at"],
        "purpose": "Live-location sessions: the authorization window for "
                   "publishable vehicle positions (DPIA: high-risk processing "
                   "— see docs/dpia-live-location.md).",
        "basis": _CO, "retention": None,
        "note": "Crew identity never serialized publicly. Sessions survive "
                "deletion (authority audit); token is a hashed, scope-bound "
                "credential, rotated/dead by design.",
    },
    {
        "app": "tracking", "model": "PingPoint",
        "fields": ["lat", "lon", "speed_kmh", "recorded_at", "received_at"],
        "purpose": "Precise vehicle positions during an authorized session "
                   "(the high-risk processing itself).",
        "basis": _CO, "retention": 3,
        "note": "72-hour floor (PING_RETENTION_HOURS=3d), pruned by the "
                "tracking sweep AND invisible past the cut at read time.",
    },
    # ------------------------------------------------------------ alerts
    {
        "app": "alerts", "model": "VehicleFollow",
        "fields": ["user", "notify_enabled", "radius_km"],
        "purpose": "Which vehicle a member watches for departures.",
        "basis": _CO, "retention": None,
    },
    {
        "app": "alerts", "model": "RouteFollow",
        "fields": ["user", "notify_enabled", "radius_km"],
        "purpose": "Which route a member watches.",
        "basis": _CO, "retention": None,
    },
    {
        "app": "alerts", "model": "FollowAlert",
        "fields": ["session", "title", "body", "adapter_id", "notified_at"],
        "purpose": "Delivered-arrival alert log via SMS/email.",
        "basis": _C, "retention": 90,
        "note": "Cutoff = notified_at + 90 days (a delivery log, then gone).",
    },
    # ---------------------------------------------------------------- ai
    {
        "app": "ai", "model": "AssistantRun",
        "fields": ["user", "have_text", "need_text", "draft_text"],
        "purpose": "The assistant's what-I-have/what-I-need drafting aid — "
                   "the user's own words in, draft out.",
        "basis": _CO, "retention": 90,
        "note": "Ephemeral aid, not a memory: swept 90 days after the last "
                "touch. ai.AICall (provider/model/tokens) is EXCLUDED — no "
                "user FK, model-vendor counters only.",
    },
    # --------------------------------------------------------------- lang
    {
        "app": "lang", "model": "ContentLanguagePreference",
        "fields": ["user", "value"],
        "purpose": "Content language preference (en/sw) per member.",
        "basis": _CO, "retention": None,
    },
    # ---------------------------------------------------------- monetize
    {
        "app": "monetize", "model": "MerchantEnrollment",
        "fields": ["user", "tier", "tier_set_by"],
        "purpose": "Merchant subscription tier (off-by-default scaffolding).",
        "basis": _C, "retention": None,
    },
    {
        "app": "monetize", "model": "FeatureFlag",
        "fields": ["enabler"],
        "purpose": "Who armed a monetization surface (ops accountability).",
        "basis": _LI, "retention": None,
    },
    {
        "app": "monetize", "model": "MonetizationAudit",
        "fields": ["actor", "target_id", "note"],
        "purpose": "Audit rows when monetization surfaces are enabled.",
        "basis": _LO, "retention": None,
    },
    {
        "app": "monetize", "model": "PromotedPlacement",
        "fields": ["enabler"],
        "purpose": "Who promoted a listing (labelled by contract).",
        "basis": _LI, "retention": None,
    },
    {
        "app": "monetize", "model": "SaccoPremiumTool",
        "fields": ["enabler"],
        "purpose": "Who enabled a SACCO premium tool.",
        "basis": _LI, "retention": None,
    },
]

# Guard against my own hand-maintained drift inside this very module:
# every row must have exactly app/model/fields/purpose/basis/retention(+note).
_REQUIRED_KEYS = {"app", "model", "fields", "purpose", "basis", "retention"}


def validate_inventory_shape() -> list[str]:
    """Structural lint on INVENTORY itself (call sites: loader + tests)."""
    problems = []
    for i, row in enumerate(INVENTORY, start=1):
        missing = _REQUIRED_KEYS - set(row)
        if missing:
            problems.append(f"row {i} missing keys: {sorted(missing)}")
        unknown = set(row) - _REQUIRED_KEYS - {"note"}
        if unknown:
            problems.append(f"row {i} has stray keys: {sorted(unknown)}")
        if not isinstance(row.get("fields"), list) or not row.get("fields"):
            problems.append(f"row {i} ({row.get('model')}): fields must be a non-empty list")
    return problems


def inventory_index() -> dict[tuple[str, str], set[str]]:
    """(app_label, model_name) -> union of personal-data field names."""
    return {row["app"] + "." + row["model"]: set(row["fields"]) for row in INVENTORY}


def inventory_rows() -> list[dict]:
    """The raw rows (loader/tests iterate this; keeps INVENTORY read-only)."""
    return [dict(row) for row in INVENTORY]


def row_for(app_label: str, model_name: str) -> dict | None:
    model_name = model_name.rsplit(".", 1)[-1]
    for row in INVENTORY:
        if row["app"] == app_label and row["model"].rsplit(".", 1)[-1] == model_name:
            return dict(row)
    return None
