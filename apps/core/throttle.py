"""throttle.py — Prompt 36 rate limiting & abuse prevention (PURE core).

PURE CORE — hard rule (the 11/30/32 pattern): this module imports NOTHING
from the web framework and stays importable from anywhere (services, cron,
scripts). The web-coupled thin layer lives in ``throttle_django.py`` (see
there for middleware + header handling); the PARENT registers that class in
``MIDDLEWARE`` — registration is not this file's job.

WHAT lives here
---------------
1. Sliding-window bucket math   — ``sliding_consume`` (deque-of-timestamps
   state, injected clock, JSON-clean values).
2. THE engine                   — ``enforce``: per-user AND per-IP buckets
   composed, progressive penalties on top.
3. Progressive penalties        — every over-limit rejection records a
   strike; 3 strikes inside a 24 h offense window locks the key out of that
   endpoint class with a doubling penalty (60 → 120 → 240 … capped at 24 h).
   Lock checks happen BEFORE any bucket consumption: a locked key refuses
   every request in the class until expiry.
4. Store protocol               — ``ThrottleStore`` with ``get_state`` /
   ``set_state`` / ``add_ts`` / ``incr``; ``MemoryThrottleStore`` fallback
   (tests + single-process dev, no dependencies) and the honest
   ``RedisThrottleStore`` stub (real production backend, never silently
   functional — same ``is_stub`` honesty pattern as the M-Pesa / notify
   adapters).
5. Rules config                 — ``THROTTLE_RULES`` (module constant,
   numbers ALIGNED to the existing doors, never invented) + ``load_rules``
   (deep-merge override) + ``rules_summary`` (the one dashboard read).

PITFALL (PROMPTS.md, verbatim): throttles must live server-side; anything
client-side is a suggestion. Everything here counts requests the SERVER
received.

NUMBERS POLICY: Prompt 36 says reports are 10/hour *from 15*, auth is
already limited *from 04*, messaging 100/day, posting 20/day, uploads
30/day. The shipped ``THROTTLE_RULES`` mirrors those constants value-for-
value; ``tests/test_throttle.py`` imports the sibling constants and asserts
EQUALITY, so any drift fails loudly instead of silently desyncing.

FAIL-OPEN POLICY (loud, deliberate): an endpoint class that is NOT in the
rules map is NOT throttled (``enforce`` returns a ``fail_open`` verdict).
Rationale: a rules mis-registration must never brick the app; config
drift is caught by ``rules_summary`` consumers + tests instead.
MIS-CONFIGURATION inside a present rule (limit < 1) fails FAST with
``ValueError`` — bad rules must shout, never fail open.

Clocks: every time-taking function takes ``now`` as a NUMBER (seconds; any
monotonic or epoch float works — the engine never converts, the Django
layer converts ``datetime`` with ``.timestamp()`` before calling).
Timestamps stored in state are the injected numbers verbatim, so tests stay
deterministic.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Protocol, Tuple

# --------------------------------------------------------------------------
# tunables (structure, not secrets) — the penalty curve
# --------------------------------------------------------------------------

#: strikes inside OFFENSE_WINDOW before the first lockout (spec: soft warning
#: first, repeated abuse = timeouts; the brief pins the first lock at 3).
STRIKES_FOR_LOCKOUT = 3

#: how long strikes stay counted (verbatim from the brief: 24 h offense window).
OFFENSE_WINDOW_SECONDS = 24 * 3600

#: FIRST lockout duration in minutes (brief verbatim: 60), progressive
#: doubling after that (60/120/240 …) capped at a day.
PENALTY_TIMEOUT_MIN = 60
PENALTY_CAP_MIN = 24 * 60

# --------------------------------------------------------------------------
# rules config — numbers aligned with existing doors; NEVER invented
# --------------------------------------------------------------------------

THROTTLE_RULES: Dict[str, Dict[str, Any]] = {
    # Prompt 04 auth door (READ, not replaced): verification codes are capped
    # per (target, purpose) at accounts.services.CODE_SEND_LIMIT_PER_HOUR (3/h)
    # and logins at accounts.services.LOGIN_FAIL_LIMIT=5 within LOGIN_LOCK_MIN=15.
    # The 'auth' rule below mirrors the code-send number so the dashboard shows
    # one aligned picture; the (target, purpose) DB door stays the authority.
    "auth": {
        "per_user_limit": 3,
        "per_ip_limit": None,
        "window_seconds": 3600,
        "penalty_curve": "warn-then-timeout: 3 strikes/24h -> 60min, x2, cap 24h",
        "source": "Prompt 04 (accounts.CODE_SEND_LIMIT_PER_HOUR = 3/h per target)",
        "wired": True,
    },
    # Prompt 36 verbatim: posting 20/day (user), 60/day (IP) per the brief.
    "posting": {
        "per_user_limit": 20,
        "per_ip_limit": 60,
        "window_seconds": 24 * 3600,
        "penalty_curve": "warn-then-timeout: 3 strikes/24h -> 60min, x2, cap 24h",
        "source": "Prompt 36 (posting 20/day; IP bucket per brief)",
        "wired": False,
    },
    # Prompt 36 verbatim: messaging 100/day (auth-only endpoint — no anon;
    # IP bucket deliberately None: NAT'd cyber-cafés share one IP, and
    # messaging is already per-user authenticated).
    "messaging_send": {
        "per_user_limit": 100,
        "per_ip_limit": None,
        "window_seconds": 24 * 3600,
        "penalty_curve": "warn-then-timeout: 3 strikes/24h -> 60min, x2, cap 24h",
        "source": "Prompt 36 (messaging 100/day)",
        "wired": False,
    },
    # Prompt 15 report door (READ, not replaced): MAX_REPORTS_PER_HOUR = 10
    # per rolling 60 minutes, counted per user in the DB. Value + window are
    # Pinned by tests/test_throttle.py to the moderation constants.
    "reports": {
        "per_user_limit": 10,
        "per_ip_limit": None,
        "window_seconds": 3600,
        "penalty_curve": "warn-then-timeout: 3 strikes/24h -> 60min, x2, cap 24h",
        "source": "Prompt 15 (moderation.MAX_REPORTS_PER_HOUR = 10/h)",
        "wired": True,
    },
    # Prompt 36 verbatim: uploads 30/day. No upload rate door exists yet
    # (media.save_upload is size/purpose-only), so the middleware/view helper
    # adds the FIRST bucket when the parent wires it. Image-heavy members
    # share one IP behind NAT — no IP bucket for the same reason as messaging.
    "uploads": {
        "per_user_limit": 30,
        "per_ip_limit": None,
        "window_seconds": 24 * 3600,
        "penalty_curve": "warn-then-timeout: 3 strikes/24h -> 60min, x2, cap 24h",
        "source": "Prompt 36 (uploads 30/day)",
        "wired": False,
    },
}


def load_rules(*, override: Optional[Dict[str, dict]] = None) -> Dict[str, dict]:
    """Shipped rules deep-merged with ``override`` (per-class dict patches).

    The web layer passes ``settings.THROTTLE_RULES_OVERRIDE`` here — settings
    stay OUT of this module (purity); callers do the getattr.
    """
    rules: Dict[str, dict] = {}
    for cls, rule in THROTTLE_RULES.items():
        rules[cls] = dict(rule)
    for cls, patch in (override or {}).items():
        base = rules.get(cls)
        if base is None:
            rules[cls] = dict(patch)
        else:
            base.update(patch)
    return rules


# --------------------------------------------------------------------------
# sliding-window bucket math (pure, injected clock, JSON-clean states)
# --------------------------------------------------------------------------


def prune(timestamps: Sequence[float], *, now: float, window_seconds: int) -> List[float]:
    """Timestamps still inside the window.

    Boundary is INCLUSIVE (``ts >= now - window``) — same ``__gte`` semantics
    moderation uses for its rolling hour, so engine and DB door agree.
    """
    edge = now - window_seconds
    return [t for t in timestamps if t >= edge]


def sliding_consume(
    timestamps: Sequence[float],
    *,
    limit: int,
    window_seconds: int,
    now: float,
) -> Tuple[bool, int, Optional[int], List[float]]:
    """Try to consume ONE slot from a sliding window.

    Returns ``(allowed, remaining, retry_after, kept_timestamps)``:

    - allowed False  -> the 11th-of-an-hour case; ``retry_after`` is the
      whole seconds until the OLDEST counted hit leaves the window (a slot
      opens), ``remaining`` is 0.
    - allowed True   -> ``kept_timestamps`` is the NEW state (previous kept
      hits + ``now``) and ``remaining`` counts slots after this one.

    ``limit >= 1`` required — misconfigured zero/negative limits raise
    (fail-fast; see the FAIL-OPEN POLICY note in the docstring).
    """
    if limit is None:
        raise ValueError("limit must be an int, not None — treat absent limits as None UPSTREAM")
    if int(limit) < 1:
        raise ValueError(f"limit must be >= 1 (got {limit}) — bad rules shout, never fail open")
    if limit != int(limit):
        raise ValueError(f"limit must be an integer (got {limit!r})")

    kept = prune(timestamps, now=now, window_seconds=window_seconds)
    if len(kept) >= limit:
        retry_after = None
        if kept:
            retry_after = max(1, math.ceil(kept[0] + window_seconds - now))
        return False, 0, retry_after, kept
    kept = kept + [float(now)]
    return True, limit - len(kept), None, kept


# --------------------------------------------------------------------------
# progressive penalties
# --------------------------------------------------------------------------


def penalty_for(strikes: int) -> int:
    """Lockout minutes for a strike count, doubling from 3 strikes.

    1–2 strikes -> 0 (no lock: soft-warning territory), 3 -> 60, 4 -> 120,
    5 -> 240 … capped at PENALTY_CAP_MIN (24 h). Never negative: a lockout
    is ALWAYS the next doubling multiple, never a made-up small number.
    """
    if strikes < STRIKES_FOR_LOCKOUT:
        return 0
    doubled = PENALTY_TIMEOUT_MIN * (2 ** (strikes - STRIKES_FOR_LOCKOUT))
    return min(PENALTY_CAP_MIN, doubled)


# --------------------------------------------------------------------------
# store protocol
# --------------------------------------------------------------------------


class ThrottleStore(Protocol):
    """The ONLY persistence surface the engine touches.

    All values are JSON-clean dicts (engine state must survive the JSON
    roundtrip every backend performs). Timestamps are numbers, not
    datetimes — JSON has no datetime, and purity keeps this module
    framework-free.

    ``add_ts`` merges the append + prune of a sliding window into ONE atomic
    primitive so each backend can implement it without read-modify-write
    races (Redis does it as a sorted-set pipeline; see RedisThrottleStore).
    ``incr`` is a plain fixed-window counter primitive — reserved for the
    dashboards/cron (Prompt 42) that count hits without the sliding state.
    """

    def get_state(self, key: str) -> Optional[Dict[str, Any]]: ...

    def set_state(self, key: str, state: Dict[str, Any]) -> None: ...

    def add_ts(
        self, key: str, now: float, *, window_seconds: int
    ) -> List[float]: ...

    def incr(self, key: str) -> int: ...

    def stats(self) -> Dict[str, Any]: ...


class MemoryThrottleStore:
    """Fallback store: pure dicts, zero dependencies, JSON-clean by contract.

    WHY the json roundtrip on every read/write: it PROVES engine state meets
    the Redis contract (numbers/strings/lists/None only) — if the engine ever
    leaks a non-JSON object into state, THIS store raises instead of letting
    a broken key reach production where the Redis backend would choke.

    Concurrency envelope: per-process, GIL-thread-safe enough for tests/dev;
    multi-worker deployments need the Redis backend (see RedisThrottleStore).
    """

    backend = "memory"

    def __init__(self) -> None:
        self._data: Dict[str, Dict[str, Any]] = {}

    @staticmethod
    def _json_roundtrip(value: Dict[str, Any]):
        return json.loads(json.dumps(value))

    def get_state(self, key: str) -> Optional[Dict[str, Any]]:
        state = self._data.get(key)
        return None if state is None else self._json_roundtrip(state)

    def set_state(self, key: str, state: Dict[str, Any]) -> None:
        self._data[key] = self._json_roundtrip(state)

    def add_ts(
        self, key: str, now: float, *, window_seconds: int
    ) -> List[float]:
        state = self._data.get(key) or {}
        kept = prune(state.get("t") or [], now=now, window_seconds=window_seconds)
        kept.append(float(now))
        self._data[key] = {"t": kept}
        return kept

    def incr(self, key: str) -> int:
        state = self._data.get(key) or {}
        count = int(state.get("c", 0)) + 1
        self._data[key] = {**state, "c": count}
        return count

    def stats(self) -> Dict[str, Any]:
        return {"backend": self.backend, "keys": len(self._data)}

    def reset(self) -> None:
        """Drop ALL state (tests + dev convenience; production never calls)."""
        self._data.clear()


class RedisThrottleStore:  # pragma: no cover - honest stub, see below
    """REAL production backend (multi-process shared state) — deployed stub.

    ``is_stub = True`` honesty pattern (same as M-Pesa/notify adapters): the
    class exists and carries the deployment recipe, but CONSTRUCTION refuses
    so a half-wired deployment fails loudly at boot instead of silently
    losing every throttle key (silent throttle loss = spammer acceptance).

    Deployment recipe for Prompt 42 (compose service):

        # deploy/compose.yml — add next to postgres:
        redis:
          image: redis:7-alpine
          command: ["redis-server", "--save", "", "--appendonly", "no"]
          ports: ["127.0.0.1:6379:6379"]
          # WHY no persistence: throttle windows are minutes/hours — a Redis
          # restart just reopens the windows, that is acceptable, and durable
          # AOF for buckets would be noise.

        # .env (parent owns .env.example):
        REDIS_URL=redis://127.0.0.1:6379/3

    Wire-up, once a client is configured (prompt 42):

        import redis
        store = RedisThrottleStore(redis.Redis.from_url(
            os.environ["REDIS_URL"], decode_responses=True))
        throttle.set_store(store)  # OR settings.THROTTLE_STORE_URL wiring

    Command mapping used by this class (numbers = timestamps in SECONDS):

        get_state(key)    -> GET key (json)                 | None miss
        set_state(key, v) -> SET key json (no TTL here — buckets carry their
                             own EXPIRE via add_ts; strikes/locks TTL'd by
                             the engine's windows)
        add_ts(key,n,win) -> pipeline:
                              ZREMRANGEBYSCORE key -inf (n - win)   # prune
                              ZADD key (n) (n)                      # hit
                              ZCARD key                             # count
                              EXPIRE key (2 * win)                  # GC
                             returns ZCARD value; sorted-set = exact
                             sliding window, not a fixed-bin counter
        incr(key)         -> INCR key + EXPIRE window (caller supplies TTL
                             via the class arg; reserved counter op)
    """

    is_stub = True  # honesty marker — checks.py-style wiring may refuse it

    def stats(self) -> Dict[str, Any]:
        raise NotImplementedError  # pragma: no cover

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        raise NotImplementedError(
            "RedisThrottleStore is the documented production backend for "
            "Prompt 42's compose deployment (see class docstring for the "
            "exact recipe); until wired it must never be used silently — "
            "tests/dev run on MemoryThrottleStore."
        )


_default_store: Optional[ThrottleStore] = None


def get_store() -> ThrottleStore:
    """The process-wide store. Django layer wires this ONCE at boot."""
    global _default_store
    if _default_store is None:
        _default_store = MemoryThrottleStore()
    return _default_store


def set_store(store: ThrottleStore) -> None:
    """Swap the process-wide store (tests/production wiring point)."""
    global _default_store
    _default_store = store


# --------------------------------------------------------------------------
# the engine: per-user + per-IP buckets, strikes, lockouts
# --------------------------------------------------------------------------

_KEY_PREFIX = "throttle"


@dataclass
class Verdict:
    """One enforce() outcome — everything the web layer needs, nothing more."""

    allowed: bool
    endpoint_class: str
    kind: str  # "allow" | "reject" | "locked" | "fail_open"
    limit: Optional[int] = None  # the applicable (strictest) bucket limit
    remaining: Optional[int] = None
    retry_after: Optional[int] = None
    penalty_minutes: Optional[int] = None
    warning: bool = False  # soft warning == an over-limit 429 that stays sub-lockout
    locks_until: Optional[float] = None
    keys: List[str] = field(default_factory=list)  # state keys touched (debug/audit)

    def to_headers(self) -> Dict[str, str]:
        """HTTP headers the web layer sets verbatim (on any verdict).

        Remaining/Limit only when a bucket applies; Retry-After only when a
        refusal carries a time (lock OR window slot).
        """
        headers: Dict[str, str] = {}
        if not self.allowed and self.retry_after is not None:
            headers["Retry-After"] = str(int(self.retry_after))
        if self.remaining is not None:
            headers["X-RateLimit-Remaining"] = str(int(max(0, self.remaining)))
            if self.limit is not None:
                headers["X-RateLimit-Limit"] = str(int(self.limit))
        return headers


def _bucket_key(endpoint_class: str, flavor: str, ident: str) -> str:
    return f"{_KEY_PREFIX}:b:{endpoint_class}:{flavor}:{ident}"


def _strike_key(endpoint_class: str, flavor: str, ident: str) -> str:
    return f"{_KEY_PREFIX}:s:{endpoint_class}:{flavor}:{ident}"


def _lock_key(endpoint_class: str, flavor: str, ident: str) -> str:
    return f"{_KEY_PREFIX}:l:{endpoint_class}:{flavor}:{ident}"


def _locked_until(store, key: str, *, now: float) -> Tuple[Optional[float], Optional[int]]:
    """(until, penalty_minutes) when ``key`` is locked RIGHT NOW, else (None, None)."""
    state = store.get_state(key)
    if not state:
        return None, None
    until = state.get("until")
    if until is None or until <= now:
        return None, None
    return float(until), state.get("penalty_minutes")


def enforce(
    store: ThrottleStore,
    *,
    endpoint_class: str,
    user_key: Optional[str] = None,
    ip_key: Optional[str] = None,
    now: float,
    rules: Optional[Dict[str, dict]] = None,
    offense_window_seconds: int = OFFENSE_WINDOW_SECONDS,
) -> Verdict:
    """Consume one hit against ``endpoint_class`` for a (user, ip) caller.

    Composition order (the abuse-prevention meat):

    1. RULES — unknown class -> ``fail_open`` verdict, allowed, NOT throttled
       (loud, documented policy). Present-but-broken limits -> ValueError.
    2. LOCKOUT — every configured key checked BEFORE consumption; a locked
       key refuses the request WITHOUT touching buckets (probe-friendly:
       hammering during a lockout earns no new strikes).
    3. BUCKETS — user bucket and IP bucket (whichever the rule configures);
       over-limit refusals record strikes ONLY on the violated key(s), the
       allowed path consumes ALL keys.
    4. Anonymous caller on a user-limit-only class: the user limit applies
       against the IP key instead (unknown identity degrades to IP — abuse
       by anon requests must still be capped; why-stated in the rules table).

    ``user_key``/``ip_key`` are plain strings (uid / address); ``ip_key``
    may be None (no IP info) — that bucket is skipped entirely.
    """
    all_rules = rules if rules is not None else load_rules()
    rule = all_rules.get(endpoint_class)
    if rule is None:
        return Verdict(
            allowed=True, endpoint_class=endpoint_class, kind="fail_open",
            remaining=None, retry_after=None,
        )

    user_limit: Optional[int] = rule["per_user_limit"]
    ip_limit: Optional[int] = rule["per_ip_limit"]
    window_seconds = int(rule["window_seconds"])
    # misconfiguration fails FAST here too — not just in sliding_consume
    for name, value in (("per_user_limit", user_limit), ("per_ip_limit", ip_limit)):
        if value is not None and (value != int(value) or int(value) < 1):
            raise ValueError(
                f"throttle rule 'per_..._limit' {name}={value!r} is invalid "
                "(int >= 1 required) — bad rules shout, never fail open"
            )

    buckets: List[Tuple[str, str, str, int]] = []  # (flavor, ident, state_key, limit)
    anonymous = user_key is None
    if user_limit is not None:
        if anonymous:
            # anon downgrade: the user limit applies to the IP identity
            if ip_key is not None:
                ident = f"ip/{ip_key}"
                buckets.append((
                    "u", ident,
                    _bucket_key(endpoint_class, "u", ident), user_limit,
                ))
        else:
            buckets.append((
                "u", str(user_key),
                _bucket_key(endpoint_class, "u", str(user_key)), user_limit,
            ))
    if ip_limit is not None and ip_key is not None:
        buckets.append((
            "i", ip_key,
            _bucket_key(endpoint_class, "i", ip_key), ip_limit,
        ))
    limit_min: Optional[int] = min(b[3] for b in buckets) if buckets else None

    # ---- phase 1: lockouts BEFORE any consumption ---------------------------
    lock_candidates: List[Tuple[float, Optional[int]]] = []
    touched: List[str] = []
    for flavor, ident, state_key, _limit in buckets:
        # the lock lives on the parallel "l:" key built from the same identity
        lock_k = _lock_key(endpoint_class, flavor, ident)
        touched.append(state_key)
        until, minutes = _locked_until(store, lock_k, now=now)
        if until is not None:
            lock_candidates.append((until, minutes))
    if lock_candidates:
        until_latest = max(u for u, _m in lock_candidates)
        minutes_sum = max(_m or penalty_for(STRIKES_FOR_LOCKOUT) for _u, _m in lock_candidates)
        retry = int(math.ceil(max(1.0, until_latest - now)))
        return Verdict(
            allowed=False, endpoint_class=endpoint_class, kind="locked",
            limit=limit_min, remaining=0, retry_after=retry,
            penalty_minutes=minutes_sum, warning=False,
            locks_until=until_latest, keys=touched,
        )

    # ---- phase 2: over-limit detection WITHOUT consuming --------------------
    over: List[Tuple[str, str, str, int, List[float], int]] = []
    peeked: List[Tuple[str, int, List[float]]] = []
    for flavor, ident, state_key, limit in buckets:
        state = store.get_state(state_key) or {}
        kept = prune(state.get("t") or [], now=now, window_seconds=window_seconds)
        if len(kept) >= limit:
            kept_at_refusal = kept[0] if kept else now
            retry = max(1, math.ceil(kept_at_refusal + window_seconds - now))
            over.append((flavor, ident, state_key, limit, kept, retry))
        elif kept:
            peeked.append((state_key, limit, kept))

    if over:
        # ---- phase 3: strikes on every violated key; lockout at threshold ----
        kind, retry_after, penalty_minutes, until_latest = "reject", None, None, None
        for flavor, ident, state_key, limit, _kept, slot_retry in over:
            strike_k = _strike_key(endpoint_class, flavor, ident)
            kept_strikes = store.add_ts(
                strike_k, float(now), window_seconds=offense_window_seconds
            )
            if len(kept_strikes) >= STRIKES_FOR_LOCKOUT:
                minutes = penalty_for(len(kept_strikes))
                until = float(now) + minutes * 60
                store.set_state(
                    _lock_key(endpoint_class, flavor, ident),
                    {"until": until, "penalty_minutes": minutes},
                )
                kind, until_latest, penalty_minutes = "locked", until, minutes
            retry_after = max(retry_after or 0, slot_retry)
        return Verdict(
            allowed=False, endpoint_class=endpoint_class, kind=kind,
            limit=limit_min, remaining=0, retry_after=retry_after,
            penalty_minutes=penalty_minutes, warning=True,
            locks_until=until_latest, keys=touched,
        )

    # ---- phase 4: consume every bucket (allowed path) ------------------------
    remaining: Optional[int] = None
    for state_key, limit, kept in peeked:
        new_state = kept + [float(now)]
        store.set_state(state_key, {"t": new_state})
        rem_now = limit - len(new_state)
        remaining = rem_now if remaining is None else min(remaining, rem_now)
    consumed_keys = {state_key for state_key, _limit, _kept in peeked}
    for _flavor, _ident, state_key, limit in buckets:
        if state_key in consumed_keys:
            continue
        # bucket had NO prior hits this window — fresh state with exactly one
        store.set_state(state_key, {"t": [float(now)]})
        rem_now = limit - 1
        remaining = rem_now if remaining is None else min(remaining, rem_now)
    return Verdict(
        allowed=True, endpoint_class=endpoint_class, kind="allow",
        limit=limit_min,
        remaining=max(0, remaining) if remaining is not None else None,
        retry_after=None, warning=False, keys=touched,
    )


# --------------------------------------------------------------------------
# the ONE dashboard read (Prompt 36: "a single dashboard read")
# --------------------------------------------------------------------------


def rules_summary(
    *,
    store: Optional[ThrottleStore] = None,
    rules: Optional[Dict[str, dict]] = None,
) -> Dict[str, Any]:
    """The single read every dashboard/cron consumes (JSON-able, no store).

    ``store=None`` -> static rules only (pure; safe for scripts). Pass a
    store to append coarse LIVE stats (the state part is injectable so the
    dashboard wiring — Prompt 37's admin — stays out of this module).
    """
    all_rules = rules if rules is not None else load_rules()
    classes = [
        {
            "endpoint_class": cls,
            "per_user_limit": rule["per_user_limit"],
            "per_ip_limit": rule["per_ip_limit"],
            "window_seconds": rule["window_seconds"],
            "penalty_curve": rule["penalty_curve"],
            "source": rule["source"],
            "wired": rule.get("wired", False),
        }
        for cls, rule in sorted(all_rules.items())
    ]
    summary: Dict[str, Any] = {
        "module": "apps.core.throttle",
        "strike_policy": {
            "strikes_for_lockout": STRIKES_FOR_LOCKOUT,
            "offense_window_seconds": OFFENSE_WINDOW_SECONDS,
            "first_lock_minutes": PENALTY_TIMEOUT_MIN,
            "cap_minutes": PENALTY_CAP_MIN,
        },
        "classes": classes,
        "store": store.stats() if store is not None else None,
    }
    return summary
