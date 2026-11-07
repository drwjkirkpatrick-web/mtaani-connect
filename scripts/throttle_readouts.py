#!/usr/bin/env python
"""scripts/throttle_readouts.py — Prompt 36 acceptance demo + dashboard read.

Run: .venv/bin/python scripts/throttle_readouts.py

Demonstrates, against the MemoryThrottleStore fallback (no services needed):

1. A spammer script driving the SAME endpoint class gets progressively
   slower (429 + Retry-After on each over-limit hit) and is eventually
   LOCKED OUT (3 strikes -> 60 min, doubling), with every lock check
   happening server-side BEFORE any bucket consumption.
2. Normal use never notices: a second well-behaved caller on the same class
   sails through the same window untouched.
3. rules_summary(store) — the ONE dashboard read (rules + live store stats).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from apps.core.throttle import (  # noqa: E402  (pure module, no services)
    MemoryThrottleStore,
    enforce,
    rules_summary,
)

DAY = 24 * 3600


def fmt(verdict) -> str:
    retry = f" retry-after={verdict.retry_after}s" if verdict.retry_after else ""
    penalty = (
        f" LOCK={verdict.penalty_minutes}min" if verdict.penalty_minutes else ""
    )
    state = "ALLOW" if verdict.allowed else verdict.kind.upper()
    rem = "" if verdict.remaining is None else f" remaining={verdict.remaining}"
    return f"{state:>9}{rem}{retry}{penalty}"


def main() -> int:
    store = MemoryThrottleStore()

    # ---- 1) the spammer: hammers 'posting' (20/day + IP bucket) ----
    print("spammer (user=u1, ip=9.9.9.9) against class 'posting':")
    t = 1000.0
    for i in range(24):  # limit 20/day: 20 allows then warnings then lockout
        verdict = enforce(
            store, endpoint_class="posting", user_key="u1",
            ip_key="9.9.9.9", now=t,
        )
        print(f"  try {i + 1:>2} @ t={t:9.1f} -> {fmt(verdict)}")
        t += 0.5

    # ---- 2) normal use, same class, same window: never notices ----
    print("\nnormal member (user=u2) same class, same second:")
    verdict = enforce(
        store, endpoint_class="posting", user_key="u2", ip_key="9.9.9.9", now=t
    )
    print(f"  their first post -> {fmt(verdict)}")

    # ---- 3) the single dashboard read ----
    print("\nrules_summary(store=...): the ONE dashboard read")
    import json

    print(json.dumps(rules_summary(store=store), indent=2)[:1200], "...")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
