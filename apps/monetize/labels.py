"""Bilingual label copy for monetization surfaces (PROMPTS.md Prompt 38).

I18N DESIGN (READ BEFORE ADDING gettext ANYWHERE IN THIS APP): the repo's
drift detector (apps.core.i18n, enforced by tests/test_i18n.py) makes ANY
new gettext/gettext_lazy/{% trans %} marker in ANY app a drift failure
against the SHARED catalog under locale/ — and locale/ is a shared file
monetize does not own. So every monetization string ships as stored en+sw
DATA (the spec's own mechanism: "label copy in en+sw stored as two
CharFields w/ defaults", one default per language) plus these pure-Python
constants for the honest stub copy. Templates render the stored fields and
bilingual inline copy (the repo's "Saved. / Imehifadhiwa." style).
Adding a gettext marker here without the parent moving these strings into
locale/ would break the whole suite — the flags stay pointed at nothing,
not at the catalog.

Audit-note strings and flash copy are plain bilingual compounds for the
same reason (staff-surface scaffold, short, both languages visible).
"""

# THE promoted disclosure (spec-given defaults; ships INSIDE every payload
# row a consumer renders — a consumer may never invent its own label).
PROMOTED_LABEL_EN = "Promoted"
PROMOTED_LABEL_SW = "Imeandikwa"

# --- SACCO premium tools (fleet analytics STUB copy, both honest states) ---
SACCO_TOOL_LABEL_EN = "Fleet analytics (sessions)"
SACCO_TOOL_LABEL_SW = "Uchambuzi wa magari (safari)"

# Disabled state: the view must say WHAT is coming and WHO can switch it on.
SACCO_STUB_DISABLED_EN = (
    "Fleet analytics view — coming when this premium tool is enabled."
)
SACCO_STUB_DISABLED_SW = (
    "Mtazamo wa uchambuzi wa magari — utaonekana zana hii maalum ikiwashwa."
)

# Enabled state: still a STUB (nothing is collected yet) that documents the
# one door shape the real view will return. NO fabricated numbers anywhere —
# the aggregates stay empty until the real view ships.
SACCO_STUB_ENABLED_EN = (
    "Fleet analytics stub — enabled, but the real view ships with billing "
    "(a later prompt). Below is the documented payload shape; NO analytics "
    "data is processed yet."
)
SACCO_STUB_ENABLED_SW = (
    "Mtazamo wa uchambuzi bado ni mfano — umewashwa, ila data halisi "
    "itafika na bili (prompt ijayo). Umbo la taarifa ni hapa chini; "
    "HAKUNA data ya uchambuzi inayokusanywa bado."
)

# The door-shaped payload fields the enabled stub documents. (field, meaning_
# en, example) — aggregate-only, mirroring the privacy rule: a SACCO analytics
# payload can structurally carry NO member identity, only day/vehicle counts.
SACCO_PAYLOAD_SHAPE = (
    ("sacco", "SACCO this payload belongs to", "<SACCO name>"),
    ("tool", "Tool key — v1 registry carries SESSION_ANALYTICS only",
     "SESSION_ANALYTICS"),
    ("window_days", "Rolling window the real view will honour (policy, not data)",
     "7"),
    ("sessions_per_day",
     "Aggregate tracking-session COUNT per day — no member identity anywhere",
     "[]"),
    ("vehicle_session_hours",
     "Aggregate session-hours per vehicle of THIS Sacco's fleet",
     "[]"),
    ("generated_at",
     "UTC instant the aggregates were produced at (injected clock)",
     "<UTC ISO-8601>"),
)
