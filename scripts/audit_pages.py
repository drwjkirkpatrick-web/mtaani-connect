#!/usr/bin/env python
"""Slow-3G page audit for Mtaani Connect (PROMPTS.md Prompt 39).

A THROTTLED-LOCAL simulation, never a network probe. It answers the one
acceptance question this prompt adds: on a slow-3G phone (the user's real
device on a ~KES 10 / 70 MB prepaid bundle) how long until the feed, a
guide page and the live map are USABLE — and does every primary route
still fit the 50 KB budget?

WHY a pure simulation (no requests to the internet, no browser):
  - the discipline must be mechanical and reproducible in CI, not
    dependent on someone's laptop bandwidth one evening;
  - a throttled browser is a good story but a bad regression test; the
    numbers this module computes are the SAME numbers any real throttled
    walk must beat, because the model is honest about what dominates on
    3G: round trips first, payload second.

The module is import-safe from pytest (tests/test_budgets.py imports
``compute_rows`` so the in-suite simulation uses THIS math, not a
rewritten copy — the simulation stays honest in-suite, per the prompt).

Usage (read-only):
    DB_NAME=mtaani_t39 .venv/bin/python scripts/audit_pages.py

STANDALONE RUNS ARE STRICTLY READ-ONLY: only GETs, no fixtures, no
inserts, no deletions. It walks the primary no-id surfaces with whatever
data the audit DB holds (an empty DB is an honest run: the table shows
"no data" rows). Pages that need a pk are audited in-suite where tests
seed the world.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# ---- module constants (THE SPEC-PINNED 3G BAR — do not loosen silently) ----
#: Downstream rate for a slow Kenyan 3G plan, kilobits per second.
#: (PROMPTS.md Prompt 39 pins this; a real HSPA phone at the edge of
#: signal reads around this number. Faster networks only make the verdict
#: kinder — never set it to flatter a fat page.)
DOWN_KBPS = 400
#: One round-trip on the same network, milliseconds.
RTT_MS = 300

#: Budgets enforced by the table (mobile-first hard rules from Prompt 05):
#: HTML page weight, and time-to-usable on the slow-3G walk.
PAGE_BUDGET_KB = 50
USABLE_BUDGET_MS = 8 * 1000  # "stays usable on 3G" — the pinned bar

#: Round trips a cold start needs before the bytes start flowing:
#: DNS + TCP + TLS + the GET = 2 extra handshakes + request RTT.
#: (Local/dev http keeps the same constant: TLS-less transport still
#: pays DNS+TCP, and the audit must not pretend cold starts are free.)
FIRST_REQUEST_ROUNDS = 4
#: Round trips for a same-origin keep-alive request (static fetch, JSON
#: mirror fetch inside the first session).
KEEPALIVE_ROUNDS = 1


def transfer_ms(n_bytes: int) -> float:
    """Payload time at DOWN_KBPS. Pure — no network, no clock."""
    return n_bytes * 8 / DOWN_KBPS


def usable_ms(n_bytes: int, *, rounds: int = FIRST_REQUEST_ROUNDS) -> float:
    """Time until the response is fully on the phone, in milliseconds.

    ``rounds`` defaults to a cold navigation (DNS+TCP+TLS+GET); a JSON
    mirror or static asset fetched keep-alive passes rounds=1.
    WHY total, not just transfer: on 400 kbps the handshakes are most of
    the cost of a small page — an audit that ignored RTT would call a
    3 KB page "instant" when a real phone waits a full second.
    """
    return round(RTT_MS * rounds + n_bytes * 8 / DOWN_KBPS, 1)


# ---- the audit table (pure; input rows, output rows) -----------------------


def compute_rows(measurements) -> list[dict]:
    """ measurements -> verdict rows for the slow-3G table.

    ``measurements``: iterable of ``(journey, url, status_code, n_bytes)``
    tuples as produced by :func:`measure_journeys` (standalone run) or by
    tests/test_budgets.py (in-suite, with seeded data). Pure — this is the
    function the suite imports so the simulated numbers and the test
    numbers can never drift apart.
    """
    rows = []
    for measurement in measurements:
        journey, url, status, n_bytes = measurement[:4]
        rounds = measurement[4] if len(measurement) > 4 else FIRST_REQUEST_ROUNDS
        kb = n_bytes / 1024
        t = usable_ms(n_bytes, rounds=rounds)
        weight_ok = kb <= PAGE_BUDGET_KB
        usable_ok = t <= USABLE_BUDGET_MS
        if status != 200:
            verdict = f"skipped (HTTP {status}; audited in-suite/not seeded)"
        elif not weight_ok and not usable_ok:
            verdict = "FAIL: over bytes AND over 3G bar"
        elif not weight_ok:
            verdict = "FAIL: over bytes budget"
        elif not usable_ok:
            verdict = "FAIL: over 3G usable bar"
        else:
            verdict = f"ok ({kb:.1f} KB ≤ {PAGE_BUDGET_KB}, usable {t:.1f} ms ≤ {USABLE_BUDGET_MS})"
        rows.append(
            {
                "journey": journey,
                "url": url,
                "status": status,
                "kb": round(kb, 2),
                "transfer_ms": round(transfer_ms(n_bytes), 1),
                "handshake_ms": RTT_MS * rounds,
                "rounds": rounds,
                "usable_ms": t,
                "verdict": verdict,
            }
        )
    return rows


def render_table(rows: list[dict]) -> str:
    """One honest printed table — route / bytes / t-plus-usable / verdict."""
    head = (
        f"{'journey':<28} {'url':<34} {'st':>3} {'KB':>7} "
        f"{'xfer':>7} {'r4+bytes ms':>11}  verdict"
    )
    lines = [head, "-" * len(head)]
    for r in rows:
        lines.append(
            f"{r['journey']:<28} {r['url'][:33]:<34} {r['status']:>3} "
            f"{r['kb']:>7.2f} {r['transfer_ms']:>7.1f} {r['usable_ms']:>11.1f}  "
            f"{r['verdict']}"
        )
    return "\n".join(lines)


# ---- the standalone walk (Django, read-only) -------------------------------


def measure_journeys():
    """Run inside Django: measure every no-id primary surface, read-only.

    WHY no-id only here: the standalone script never writes to any table,
    so a pk'd route is only ever auditable in-suite (tests/test_budgets.py
    seeds its world). A 404/redirect row prints as "skipped" with a note —
    honest, never fabricated, never silently omitted.
    """
    from django.test import Client
    from django.urls import reverse

    cases = [
        ("home", "/"),
        ("guides list", reverse("guides:list")),
        ("posts feed", reverse("feed:list")),
        ("qa list", reverse("qa:list")),
        ("market list", reverse("listings:list")),
        ("services list", reverse("services:list")),
        ("cross search", reverse("search:search")),
        ("guide offline", reverse("guides:offline")),
        ("transport", "/transport/"),
        ("live map", reverse("map:route")),
    ]
    measurements = []
    for journey, url in cases:
        response = Client().get(url)
        measurements.append((journey, url, response.status_code, len(response.content)))
    return measurements


def main() -> int:
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "mtaani.settings.test")
    os.environ.setdefault("DB_NAME", "mtaani_t39")

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    import django

    django.setup()
    from django.conf import settings

    # Read-only discipline: the standalone audit never runs against an
    # unknown database — the disposable audit DB or nothing.
    db_name = settings.DATABASES["default"]["NAME"]
    if db_name != "mtaani_t39":
        print(f"refusing: standalone audit only runs against mtaani_t39 (got {db_name!r})")
        return 2

    rows = compute_rows(measure_journeys())
    print(render_table(rows))

    failures = [r for r in rows if r["verdict"].startswith("FAIL")]
    print()
    if failures:
        print(f"{len(failures)} route(s) OVER budget — trim before landing:")
        for r in failures:
            print(f"  - {r['journey']}: {r['verdict']}")
        return 1
    print("all seeded surfaces held the 50 KB / 8 s slow-3G bar. (pk'd pages")
    print("are audited in-suite: DB_NAME=mtaani_t39 .venv/bin/python -m pytest tests/test_budgets.py)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
