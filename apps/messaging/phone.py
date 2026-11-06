"""Phone-shape detector for Messaging v1 (Prompt 20) — LOCAL module-level copy.

WHY this exists: phone-number sharing through messages is allowed ONLY as
explicit user choice, but the phone-pattern detector (Prompt 17) still flags
it so moderation analytics can track scam patterns (phones are the #1 scam
vector per PROMPTS.md). The flag is METADATA for analytics only — the message
still sends; nothing here blocks or rewrites a message (that would be
moderation's decision, in the Prompt 15 queue).

NOTE (parent consolidation): apps.listings (Prompt 17) is being built in a
sibling wave and owns the canonical detector. This module intentionally
mirrors the contract `phone_shapes(text: str) -> bool` so the parent can
replace the call sites (or import listings' implementation) without touching
callers. See KENYAN_MOBILE_RE below — reported verbatim in the build report.
"""
import re

# Matches Kenyan mobile-number shapes in free text, tolerant of the everyday
# separators people type: spaces, dots, dashes, parentheses between groups.
# Sources (prompt 17 verbatim): "07xx" / "+2547xx" patterns; 01xx/+2541xx is
# the newer Kenyan mobile range and is intentionally included (same intent:
# a mobile-shaped number). Anchoring lookarounds stop matches inside longer
# digit runs (coordinates, prices, IDs) so analytics stay honest.
#   - (?<![+\d])   nothing digit-or-plus glued on the left
#   - prefix:      "0" (domestic) or "+254" / bare "254" (international)
#   - operator:    "7XX" or "1XX" (mobile ranges)
#   - then two triples, separator-tolerant, with no digit glued on the right.
KENYAN_MOBILE_RE = re.compile(
    r"""
    (?<![+\d])            # left anchor: not inside a longer digit run
    (?:                   # -- country / trunk prefix --
        0                 #    domestic trunk zero
      | \+?254            #    +254 or bare 254 (international)
    )
    [\s\-.(]{0,2}?        # separator between prefix and operator code
    (?:7|1)\d{2}          # mobile operator code: 7XX / 1XX
    [\s\-.]*\d{3}         # first triple (separator-tolerant)
    [\s\-.]*\d{3}         # last triple
    (?!\d)                # right anchor: not inside a longer digit run
    """,
    re.VERBOSE,
)


def phone_shapes(text: str) -> bool:
    """True when `text` contains a Kenyan mobile-shaped number.

    WHY bool not the match: the flag is a per-message boolean column at save
    time; details (which shape, where) belong to moderation tooling, not to
    chat payloads — storing more would just be a second place to leak PII.
    """
    return bool(text and KENYAN_MOBILE_RE.search(str(text)))
