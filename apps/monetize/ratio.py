"""The promoted→organic ratio math (PROMPTS.md Prompt 38) — PURE, no Django.

Spec: promoted listings may appear inside SEARCH RESULTS only, clearly
labeled, capped at ≤1 promoted per 6 organic rows, and NEVER in the feed's
ranking — the feed's scoring function (Prompt 11) stays untouched; this PURE
function never imports it and the consumer composes it AFTER its own
ordering, so promoted content can never re-arrange organic rows, only slot
in beside them.

Contract enforced by `interleave`:
- capacity = len(organic) // ratio — one promoted slot "costs" `ratio`
  organic rows, which makes the ≤1-per-6 cap STRUCTURAL (not a UI promise):
  7 organic + 3 promoted → capacity 1; a leftover tail shorter than `ratio`
  can never host a slot.
- promoted rows must be PAYLOAD rows that ALREADY carry their bilingual
  disclosure (keys "promoted_en" + "promoted_sw", both non-empty strings) —
  an unlabeled promoted row can never pass this door, because paid content
  rendered without its label is the one failure this surface must never
  have. The label copy ships INSIDE the payload rows; consumers render it,
  they never invent it (see apps/monetize/labels.py).
- with NO organic rows the promoted rows are never shown (paid content must
  never render into an honest empty stream).
- promoted rows are never adjacent: at most one lands per slot and slots are
  separated by ≥1 organic row by construction.
- PURE: no clock, no DB, no Django import; inputs are never mutated (new
  list out, originals untouched and reusable).
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping

# ≤1 promoted per 6 organic — the ratio the spec pins for v1.
SLOT_RATIO = 6

_PROMOTED_LABEL_KEYS = ("promoted_en", "promoted_sw")


def _verify_payload_shape(row: Any, index: int) -> None:
    """Reject ANY promoted row that does not carry complete bilingual label
    copy. Cheap, loud, wrong-in-the-best-way: this guard is the reason a
    disclosure bug cannot exist through this door."""
    if not isinstance(row, Mapping):
        raise ValueError(
            "interleave: promoted rows must be payload MAPPINGs that carry "
            "their own label copy (spec: the label ships inside the row)"
        )
    for key in _PROMOTED_LABEL_KEYS:
        value = row.get(key)
        if not (isinstance(value, str) and value.strip()):
            raise ValueError(
                f"interleave: promoted payload #{index} has no usable "
                f"'{key}' label — an unlabeled promoted row can never "
                "interleave (disclosure is the whole contract, Prompt 38)"
            )


def interleave(
    organic_rows: Iterable[Any],
    promoted_rows: Iterable[Any],
    *,
    ratio: int = SLOT_RATIO,
) -> list:
    """Weave promoted rows into organic rows, ≤1 promoted per `ratio`
    organic.

    Placement rule: ONE promoted row goes immediately after every
    `ratio`-th organic row (so after the 6th organic it sits at overall
    position 7 — the parent's "after the 6th organic" reading of ≤1 per 6);
    slots beyond the capacity ratio × len(organic) are DROPPED, never
    stacked (two promoted rows can never be adjacent and no promoted row
    ever leads or replaces an organic one).
    """
    if not isinstance(ratio, int) or isinstance(ratio, bool) or ratio < 1:
        raise ValueError(f"interleave: ratio must be an int ≥ 1, got {ratio!r}")
    organic = list(organic_rows or [])
    promoted = list(promoted_rows or [])
    for i, row in enumerate(promoted):
        _verify_payload_shape(row, i)
    if not organic:
        return []  # no organic context → no promoted renders, ever
    out: list = []
    used = 0
    capacity = len(organic) // ratio
    for position, row in enumerate(organic, start=1):
        out.append(row)
        if position % ratio == 0 and used < capacity and used < len(promoted):
            out.append(promoted[used])
            used += 1
    return out
