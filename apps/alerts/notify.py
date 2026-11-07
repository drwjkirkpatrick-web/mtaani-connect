"""notify.py — the NotifyAdapter seam (Prompt 31).

MOCK MODE FIRST (hard rule): pushes fire into a deterministic in-memory
``MockPush``; real Web Push is a DOCUMENTED LATER adapter — its stub below
is never networked and never called by any flow (same honesty pattern as
barter's MPesa stub: is_stub=True, loud failure on accidental use).

FROZEN INTERFACE (what a future adapter must implement — one method):

    adapter.send(follow, title, body) -> str | None

``follow`` is an alerts.VehicleFollow or alerts.RouteFollow row (the
adapter decides what a payload needs — device tokens live in ITS layer,
never here); title/body are already-final display strings from the alert
service. Return value is an ADAPTER RECEIPT ("mock:1") or None when the
channel accepted the push without one; the receipt is stored on the
FollowAlert ledger row, so "did a channel accept this?" is answerable.
Adapters must not raise for per-user conditions (no device, quiet hours) —
return None and let the ledger show the attempt honestly; raising is for
programming/misconfiguration errors only.
"""
from __future__ import annotations


class NotifyAdapter:
    """The interface every push channel implements (contract in docstring)."""

    adapter_name = "abstract"

    def send(self, follow, title: str, body: str) -> str | None:
        raise NotImplementedError


class MockPush(NotifyAdapter):
    """Deterministic in-memory channel — tests/dev only, zero side effects.

    Each instance records every send into ``self.sent`` (ordered dicts) and
    returns "mock:<n>" receipts. DETERMINISM CONTRACT (tests pin it): two
    identical scans run through fresh MockPush instances record IDENTICAL
    lists — the default adapter never reads the wall clock, random state or
    anything but its own arguments.
    """

    adapter_name = "mock"

    def __init__(self):
        self.sent: list[dict] = []

    def send(self, follow, title: str, body: str) -> str:
        self.sent.append(
            {
                "kind": follow.__class__.__name__,
                "follow_id": follow.pk,
                "title": title,
                "body": body,
            }
        )
        return f"mock:{len(self.sent)}"


class WebPushAdapter(NotifyAdapter):
    """STUB — Web Push is a documented later adapter (PROMPTS.md 31).

    Documented constructor contract for the real one (NOT built here):
    VAPID public/private keys from .env (never committed); a subscriptions
    store on the rider's account record, created only behind an explicit
    browser permission prompt; payload encryption per the Web Push RFC.

    ``is_stub = True`` marks it for checks: no flow in this app calls it,
    it performs no networking, and an accidental call fails LOUDLY with
    NotImplementedError instead of silently doing nothing.
    """

    adapter_name = "webpush"
    is_stub = True

    def send(self, follow, title: str, body: str) -> str | None:
        raise NotImplementedError(
            "WebPushAdapter is a stub: Web Push arrives as a documented later "
            "adapter (spec 31). Implement VAPID + subscription storage first."
        )


# ----------------------------------------------------------------- registry
# Name → class. The default is MockPush (mock mode first); a future adapter
# registers itself HERE — no flow changes. Unknown names raise, never guess.
ADAPTERS: dict[str, type[NotifyAdapter]] = {
    MockPush.adapter_name: MockPush,
    WebPushAdapter.adapter_name: WebPushAdapter,
}
DEFAULT_ADAPTER = MockPush.adapter_name


def get_adapter(name: str | None = None) -> NotifyAdapter:
    """Resolve ``name`` (or the default) into a FRESH adapter instance.

    WHY fresh per call: a scan pass owns its channel — no hidden shared
    state means two identical scans with fresh adapters are provably
    identical (the determinism tests use this); callers that want an
    accumulated view instantiate MockPush() themselves and pass it in.
    """
    name = name or DEFAULT_ADAPTER
    try:
        cls = ADAPTERS[name]
    except KeyError:
        raise ValueError(f"Unknown notification adapter {name!r} — one of {sorted(ADAPTERS)}")
    return cls()
