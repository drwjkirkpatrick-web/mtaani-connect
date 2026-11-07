"""Pure content-language chip helpers (PROMPTS.md Prompt 35).

Chips render a mixed feed's language mix. WHY a helper and not a model
column here: content models belong to other prompts and stay untouched;
the chip reads whatever language metadata an item CARRIES if it carries
any, and falls back to a neutral marker for free-form content (Sheng,
code-switched, unmixed-but-unknown). Honest by construction.

The parent wiring seam (12-seam): give a content model an optional
``content_lang`` attribute (column, property, or view-attached value) and
``language_chip(obj)`` does the rest. Without it, every item shows the
neutral chip — correct, merely less labelled.
"""
from __future__ import annotations

# Content-side markers: sw/en cover the two chrome languages; 'sheng' is
# a first-class content dialect marker (Prompt 25 alias/nickname fields
# already accept Sheng); anything else renders NEUTRAL.
_CONTENT_CODES = {"sw", "en", "sheng"}

_CHIP = {
    "sw": {"code": "sw", "label": "Kiswahili", "css": "lang chip-sw", "neutral": False},
    "en": {"code": "en", "label": "English", "css": "lang chip-en", "neutral": False},
    "sheng": {"code": "sheng", "label": "Sheng", "css": "lang chip-sheng", "neutral": False},
    "neutral": {
        "code": "",
        "label": "content",
        "css": "lang chip-neutral",
        "neutral": True,
    },
}


def language_chip(obj) -> dict:
    """Chip dict for an item with optional ``content_lang`` metadata.

    Returns {'code','label','css','neutral'}; missing/unknown metadata
    yields the neutral content chip (never a fabricated language claim).
    """
    code = getattr(obj, "content_lang", None)
    if code in _CONTENT_CODES:
        return dict(_CHIP[code])
    return dict(_CHIP["neutral"])


def chips_for(items) -> list[dict]:
    """Convenience: chip for each item in a mixed feed."""
    return [language_chip(item) for item in items]
