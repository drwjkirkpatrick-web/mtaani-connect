"""Phone-shape detector for Messaging v1 (Prompt 20) — thin adapter.

Parent consolidation APPLIED: Prompt 17's canonical detector
(apps.listings.services.detect_phone_patterns) is the single pattern
contract for listings AND message analytics; its 01XX/1XX range absorbs
what this module's local copy had. This adapter keeps the message-side
save-time seam (`phone_shapes(text) -> bool`) so callers
(apps/messaging/models.py Message.clean) don't change — the flag is
METADATA for moderation analytics only; the message still sends. Nothing
here blocks or rewrites a message (that is the human moderator's decision,
in the Prompt 15 queue — people legitimately text phone-shaped things).

WHY bool not the match: a per-message boolean keeps chat payloads clean;
the matched strings belong to moderation tooling — storing more would just
be a second place to leak PII (KDPA: don't store what you don't need).
"""
from apps.listings.services import detect_phone_patterns


def phone_shapes(text: str) -> bool:
    """True when `text` contains a Kenyan mobile-shaped number (07XX/01XX,
    +2547XX/+2541XX, separator-tolerant; immune to prices/dates/coords)."""
    return bool(text and detect_phone_patterns(str(text)))
