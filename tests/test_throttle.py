"""tests/test_throttle.py — Prompt 36: rate limiting & abuse prevention.

Pure-module tests (bucket math, penalties, stores, rules, purity) run with
NO database; the Django thin-layer tests stub ``resolver_match`` and drive
the middleware through ``RequestFactory`` with a stub ``get_response``.

Spec pins (PROMPTS.md Prompt 36, verbatim numbers): posting 20/day,
messaging 100/day, reports 10/hour FROM PROMPT 15 (imported constant
equality — drift fails here, never silently), uploads 30/day, auth
already limited from 04.

Pitfall honored: throttles are server-side — the only client-visible parts
are HTTP surface (429 + Retry-After + X-RateLimit headers), never state.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from django.conf import settings
from django.contrib.auth.models import AnonymousUser
from django.http import HttpResponse
from django.test import RequestFactory

from apps.core import throttle, throttle_django
from apps.core.throttle import (
    MemoryThrottleStore,
    RedisThrottleStore,
    enforce,
    load_rules,
    penalty_for,
    prune,
    rules_summary,
    sliding_consume,
)

REPO = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------- fixtures


@pytest.fixture
def store():
    """A fresh memory store per test — isolated, no cross-test leakage."""
    return MemoryThrottleStore()


@pytest.fixture
def clean_default_store(monkeypatch):
    """Point get_store() at a fresh instance for middleware tests."""
    fresh = MemoryThrottleStore()
    monkeypatch.setattr(throttle, "_default_store", fresh)
    return fresh


def hit(store, cls, *, user=None, ip=None, t=1000.0, rules=None):
    return enforce(
        store, endpoint_class=cls, user_key=user, ip_key=ip, now=t, rules=rules
    )


@pytest.fixture
def posted_rule():
    """A rule with BOTH buckets — the composed-identity testing vehicle."""
    return load_rules(override={
        "testcls": {
            "per_user_limit": 2, "per_ip_limit": 3, "window_seconds": 60,
            "penalty_curve": "warn-then-timeout", "source": "test",
        },
    })


# ------------------------------------------------------------ bucket math


def test_limit_hit_exactly_then_rejected():
    allowed, remaining, retry, kept = sliding_consume([], limit=10, window_seconds=60, now=100.0)
    assert (allowed, remaining, retry) == (True, 9, None)
    for i in range(1, 10):
        allowed, remaining, retry, kept = sliding_consume(
            kept, limit=10, window_seconds=60, now=100.0 + i
        )
    assert (allowed, remaining) == (True, 0)  # 10th allowed, ZERO remaining
    allowed, remaining, retry, _ = sliding_consume(
        kept, limit=10, window_seconds=60, now=100.5
    )
    assert allowed is False and remaining == 0  # the 11th refused


def test_one_under_limit_is_allowed():
    allowed, remaining, retry, _ = sliding_consume([1.0, 2.0], limit=3, window_seconds=60, now=3.0)
    assert (allowed, remaining, retry) == (True, 0, None)


def test_window_rolls_with_injected_clock():
    _, _, _, kept = sliding_consume([], limit=2, window_seconds=60, now=100.0)
    _, _, _, kept = sliding_consume(kept, limit=2, window_seconds=60, now=101.0)
    allowed, remaining, retry, _ = sliding_consume(kept, limit=2, window_seconds=60, now=100.5)
    assert allowed is False  # full right now
    # roll past BOTH hits: the 101.0 hit leaves the edge (=101.0) immediately
    allowed, remaining, retry, kept = sliding_consume(kept, limit=2, window_seconds=60, now=161.1)
    assert (allowed, remaining, retry) == (True, 1, None)


def test_window_boundary_is_inclusive():
    # ts exactly at now-window still counts (matches moderation's __gte)
    kept = prune([40.0], now=100.0, window_seconds=60)
    assert kept == [40.0]
    kept = prune([40.0], now=100.001, window_seconds=60)
    assert kept == []  # one atto outside -> expired


def test_retry_after_points_at_first_opening_slot():
    allowed, remaining, retry, kept = sliding_consume([], limit=2, window_seconds=60, now=100.0)
    allowed, remaining, retry, kept = sliding_consume(kept, limit=2, window_seconds=60, now=101.0)
    allowed, remaining, retry, _ = sliding_consume(kept, limit=2, window_seconds=60, now=101.5)
    assert allowed is False
    assert retry == 59  # oldest (100) + 60 - 101.5 -> ceil => 59s wait


def test_zero_and_fractional_limits_fail_fast():
    with pytest.raises(ValueError):
        sliding_consume([], limit=0, window_seconds=60, now=0.0)
    with pytest.raises(ValueError):
        sliding_consume([], limit=2.5, window_seconds=60, now=0.0)
    with pytest.raises(ValueError):
        sliding_consume([], limit=None, window_seconds=60, now=0.0)


# -------------------------------------------------- composition: user + IP


def test_user_under_but_ip_over_is_rejected(store, posted_rule):
    # one IP burned by a DIFFERENT user's quota
    for i in range(3):
        hit(store, "testcls", user=f"other{i}", ip="9.9.9.9", t=1000 + i, rules=posted_rule)
    verdict = hit(store, "testcls", user="7", ip="9.9.9.9", t=1003, rules=posted_rule)
    assert verdict.allowed is False and verdict.kind == "reject"


def test_ip_under_but_user_over_is_rejected(store, posted_rule):
    for _ in range(2):
        hit(store, "testcls", user="7", ip="9.9.9.1", t=1000, rules=posted_rule)
    verdict = hit(store, "testcls", user="7", ip="9.9.9.1", t=1001, rules=posted_rule)
    assert verdict.allowed is False and verdict.kind == "reject"


def test_both_buckets_compose_min_remaining(store, posted_rule):
    v = hit(store, "testcls", user="7", ip="9.9.9.9", t=1000, rules=posted_rule)
    # after the hit: user bucket 2-typed limit -> remaining 1; ip 3-limit -> 2
    assert v.remaining == 1  # strictest bucket wins
    assert v.kind == "allow"


def test_buckets_do_not_leak_across_identities(store, posted_rule):
    assert hit(store, "testcls", user="7", ip="9.9.9.9", t=1000, rules=posted_rule).allowed
    assert hit(store, "testcls", user="7", ip="9.9.9.9", t=1000.5, rules=posted_rule).allowed
    assert hit(store, "testcls", user="8", ip="8.8.8.8", t=1001, rules=posted_rule).allowed  # own bucket
    verdict = hit(store, "testcls", user="7", ip="9.9.9.9", t=1002, rules=posted_rule)
    assert verdict.allowed is False  # user 7's OWN bucket is burned; others fine


# ------------------------------------------------- anonymous / missing data


def test_anonymous_downgrades_to_ip_key(store, posted_rule):
    # user_key None: the USER limit applies against the IP identity
    assert hit(store, "testcls", user=None, ip="5.5.5.5", t=1000, rules=posted_rule).allowed
    assert hit(store, "testcls", user=None, ip="5.5.5.5", t=1001, rules=posted_rule).allowed
    verdict = hit(store, "testcls", user=None, ip="5.5.5.5", t=1002, rules=posted_rule)
    assert verdict.allowed is False  # 3rd anon hit from same ip refused


def test_no_identity_info_at_all_fails_open(store, posted_rule):
    verdict = hit(store, "testcls", user=None, ip=None, t=1000, rules=posted_rule)
    assert verdict.allowed is True and verdict.remaining is None


# ------------------------------------------------- progressive penalties


def test_penalties_curve_doubles_and_caps():
    assert penalty_for(1) == 0 and penalty_for(2) == 0  # soft-warning zone
    assert penalty_for(3) == 60
    assert penalty_for(4) == 120
    assert penalty_for(5) == 240
    assert penalty_for(6) == 480
    assert penalty_for(12) == 24 * 60  # hard cap: never more than a day


def test_first_over_limit_is_soft_warning_reject(store, posted_rule):
    for i in range(2):
        assert hit(store, "testcls", user="7", ip="1.1.1.1", t=1000 + i, rules=posted_rule).allowed
    verdict = hit(store, "testcls", user="7", ip="1.1.1.1", t=1002, rules=posted_rule)
    assert verdict.allowed is False
    assert verdict.kind == "reject" and verdict.warning is True  # warning level
    assert verdict.penalty_minutes is None  # no lockout yet


def test_three_strikes_engage_60_minute_lock(store, posted_rule):
    for i in range(2):  # burn the 2-hit bucket
        assert hit(store, "testcls", user="7", ip="1.1.1.1", t=1000 + i, rules=posted_rule).allowed
    assert hit(store, "testcls", user="7", ip="1.1.1.1", t=1002, rules=posted_rule).kind == "reject"  # strike 1
    assert hit(store, "testcls", user="7", ip="1.1.1.1", t=1003, rules=posted_rule).kind == "reject"  # strike 2
    verdict = hit(store, "testcls", user="7", ip="1.1.1.1", t=1004, rules=posted_rule)  # strike 3
    assert verdict.allowed is False  # locked NOW
    assert verdict.kind == "locked" and verdict.penalty_minutes == 60
    # further requests ALSO refused (probe during lockout refuses all)
    probe = hit(store, "testcls", user="7", ip="1.1.1.1", t=1005, rules=posted_rule)
    assert probe.allowed is False and probe.kind == "locked"


def test_lockout_expiry_permits_again_and_fourth_strike_doubles(store, posted_rule):
    for i in range(2):  # burn the 2-hit bucket
        assert hit(store, "testcls", user="7", ip="1.1.1.1", t=1000 + i, rules=posted_rule).allowed
    hit(store, "testcls", user="7", ip="1.1.1.1", t=1002, rules=posted_rule)  # strike 1 (warning 429)
    hit(store, "testcls", user="7", ip="1.1.1.1", t=1003, rules=posted_rule)  # strike 2 (warning 429)
    locked = hit(store, "testcls", user="7", ip="1.1.1.1", t=1004, rules=posted_rule)  # strike 3
    assert locked.kind == "locked" and locked.penalty_minutes == 60
    # past the lock, expired AND window rolled: normal use resumes
    resumed = hit(store, "testcls", user="7", ip="1.1.1.1", t=4700, rules=posted_rule)
    assert resumed.allowed is True
    assert hit(store, "testcls", user="7", ip="1.1.1.1", t=4701, rules=posted_rule).allowed
    # 4th strike within the still-live 24h offense window: the curve doubles
    fourth = hit(store, "testcls", user="7", ip="1.1.1.1", t=4702, rules=posted_rule)
    assert fourth.allowed is False and fourth.kind == "locked"
    assert fourth.penalty_minutes == 120  # 60 -> x2 -> 120
    assert 0 < fourth.retry_after <= 120 * 60


def test_locked_key_refuses_all_class_requests_without_consuming(store, posted_rule):
    hit(store, "testcls", user="7", ip="1.1.1.1", t=1000, rules=posted_rule)
    hit(store, "testcls", user="7", ip="1.1.1.1", t=1001, rules=posted_rule)
    hit(store, "testcls", user="7", ip="1.1.1.1", t=1002, rules=posted_rule)  # strike 1
    hit(store, "testcls", user="7", ip="1.1.1.1", t=1003, rules=posted_rule)  # strike 2
    assert hit(store, "testcls", user="7", ip="1.1.1.1", t=1004, rules=posted_rule).kind == "locked"  # strike 3
    before = json.loads(json.dumps(store.get_state("throttle:b:testcls:u:7")))
    strikes_before = len((store.get_state("throttle:s:testcls:u:7") or {}).get("t") or [])
    assert strikes_before == 3
    for _ in range(10):  # hammering DURING a lockout earns no new strikes
        probe = hit(store, "testcls", user="7", ip="1.1.1.1", t=1005, rules=posted_rule)
        assert probe.allowed is False and probe.kind == "locked"
    after = store.get_state("throttle:b:testcls:u:7")
    assert after == before  # bucket untouched by locked probes
    assert len(store.get_state("throttle:s:testcls:u:7")["t"]) == strikes_before


def test_old_strikes_fall_out_of_the_offense_window(store, posted_rule):
    hit(store, "testcls", user="7", ip="1.1.1.1", t=0.0, rules=posted_rule)   # allow
    hit(store, "testcls", user="7", ip="1.1.1.1", t=1.0, rules=posted_rule)   # allow
    hit(store, "testcls", user="7", ip="1.1.1.1", t=2.0, rules=posted_rule)   # strike 1
    verdict = hit(store, "testcls", user="7", ip="1.1.1.1", t=3.0, rules=posted_rule)  # strike 2
    assert verdict.allowed is False and verdict.penalty_minutes is None
    # 24h later: the 60s bucket AND the 24h offense window both rolled —
    # normal use resumes, then two MORE over-limit hits earn two fresh strikes
    assert hit(store, "testcls", user="7", ip="1.1.1.1", t=24 * 3600 + 1, rules=posted_rule).allowed is True
    assert hit(store, "testcls", user="7", ip="1.1.1.1", t=24 * 3600 + 2, rules=posted_rule).allowed is True
    verdict = hit(store, "testcls", user="7", ip="1.1.1.1", t=24 * 3600 + 3, rules=posted_rule)
    assert verdict.allowed is False and verdict.kind == "reject"  # strike 1 of the new era
    assert verdict.penalty_minutes is None  # NO lockout: old strikes forgiven
    verdict = hit(store, "testcls", user="7", ip="1.1.1.1", t=24 * 3600 + 4, rules=posted_rule)
    assert verdict.allowed is False and verdict.kind == "reject"  # strike 2
    assert verdict.penalty_minutes is None  # a third strike would lock, but old ones fell out
    strikes = (store.get_state("throttle:s:testcls:u:7") or {}).get("t") or []
    assert len(strikes) == 2  # exactly the fresh strikes survived


# ---------------------------------------------------------- stores


def test_memory_store_is_isolated_per_instance():
    a, b = MemoryThrottleStore(), MemoryThrottleStore()
    assert enforce(a, endpoint_class="testcls", user_key="7", ip_key=None,
                   now=1.0, rules={"testcls": {"per_user_limit": 1, "per_ip_limit": None,
                   "window_seconds": 60}}).allowed is True
    # store b NEVER saw the hit
    verdict = enforce(b, endpoint_class="testcls", user_key="7", ip_key=None,
                      now=1.0, rules={"testcls": {"per_user_limit": 1, "per_ip_limit": None,
                      "window_seconds": 60}})
    assert verdict.allowed is True and verdict.remaining == 0


def test_memory_store_state_survives_json_roundtrip(store):
    store.set_state("k", {"t": [1.0, 2.5], "note": None})
    assert store.get_state("k") == {"t": [1.0, 2.5], "note": None}
    # mutable copies: mutating the returned dict must NOT poison the store
    fetched = store.get_state("k")
    fetched["t"].append(999)
    assert store.get_state("k")["t"] == [1.0, 2.5]


def test_memory_store_refuses_non_json_state(store):
    with pytest.raises(TypeError):
        store.set_state("k", {"bad": {1, 2}})  # a SET is not JSON — loud, per contract


def test_memory_store_incr_counter_reserved_for_dashboards(store):
    assert store.incr("c:k") == 1 and store.incr("c:k") == 2
    assert store.stats() == {"backend": "memory", "keys": 1}
    store.reset()
    assert store.stats() == {"backend": "memory", "keys": 0}


class JsonSerializingFakeStore:
    """Protocol adapter test: a network-store stand-in (like Redis would be)
    that serializes EVERYTHING through json strings on the way in/out."""

    backend = "json-fake"

    def __init__(self) -> None:
        self._raw: dict[str, str] = {}

    def get_state(self, key):
        raw = self._raw.get(key)
        return None if raw is None else json.loads(raw)

    def set_state(self, key, state) -> None:
        self._raw[key] = json.dumps(state)

    def add_ts(self, key, now, *, window_seconds: int):
        state = json.loads(self._raw.get(key) or "{}")
        kept = prune(state.get("t") or [], now=now, window_seconds=window_seconds)
        kept.append(float(now))
        self._raw[key] = json.dumps({"t": kept})
        return kept

    def incr(self, key) -> int:
        state = json.loads(self._raw.get(key) or '{}')
        count = int(state.get("c", 0)) + 1
        self._raw[key] = json.dumps({**state, "c": count})
        return count

    def stats(self):
        return {"backend": self.backend, "keys": len(self._raw)}


def test_rules_module_runs_untouched_over_a_serializing_store():
    """The REAL fallback/store contract: the engine cannot tell the JSON
    store stand-in from memory — same verdicts, same lockout progression."""
    fake = JsonSerializingFakeStore()
    rule = load_rules(override={
        "testcls": {"per_user_limit": 1, "per_ip_limit": None,
                    "window_seconds": 60, "penalty_curve": "x", "source": "t"},
    })
    first = enforce(fake, endpoint_class="testcls", user_key="7", ip_key=None,
                    now=10.0, rules=rule)
    second = enforce(fake, endpoint_class="testcls", user_key="7", ip_key=None,
                     now=11.0, rules=rule)
    third = enforce(fake, endpoint_class="testcls", user_key="7", ip_key=None,
                    now=12.0, rules=rule)
    fourth = enforce(fake, endpoint_class="testcls", user_key="7", ip_key=None,
                     now=13.0, rules=rule)
    assert first.allowed is True                     # the only hit the bucket holds
    assert second.allowed is False and second.kind == "reject"   # strike 1
    assert third.allowed is False and third.penalty_minutes is None  # strike 2
    assert fourth.allowed is False and fourth.kind == "locked"   # strike 3
    assert fourth.penalty_minutes == 60
    # the fake carries the SAME shape any redis backend must return
    locked = fake.get_state("throttle:l:testcls:u:7")
    assert locked["penalty_minutes"] == 60 and locked["until"] > 13.0


def test_redis_store_is_the_honest_production_stub():
    assert RedisThrottleStore.is_stub is True
    with pytest.raises(NotImplementedError):  # loud at construction: never silent
        RedisThrottleStore()


# ------------------------------------------------- rules config + loading


def test_rules_ship_all_spec_classes_with_exact_numbers():
    rules = load_rules()
    assert set(rules) == {"auth", "posting", "messaging_send", "reports", "uploads"}
    assert rules["posting"]["per_user_limit"] == 20            # 20/day
    assert rules["posting"]["per_ip_limit"] == 60
    assert rules["posting"]["window_seconds"] == 24 * 3600
    assert rules["messaging_send"]["per_user_limit"] == 100    # 100/day
    assert rules["uploads"]["per_user_limit"] == 30            # 30/day
    assert rules["uploads"]["window_seconds"] == 24 * 3600


def test_reports_constants_match_prompt_15_imported():
    from apps.moderation import services as moderation
    rules = load_rules()
    assert rules["reports"]["per_user_limit"] == moderation.MAX_REPORTS_PER_HOUR == 10
    assert rules["reports"]["window_seconds"] == int(moderation.RATE_WINDOW.total_seconds())
    assert moderation.RATE_WINDOW == timedelta(minutes=60)  # rolling hour, verbatim


def test_auth_constants_match_prompt_04_imported():
    from apps.accounts import services as accounts
    rules = load_rules()
    # the code-send door: 3 sends/hour per (target, purpose)
    assert rules["auth"]["per_user_limit"] == accounts.CODE_SEND_LIMIT_PER_HOUR == 3
    assert rules["auth"]["window_seconds"] == 3600
    # the login door stays (username, ip)-keyed in 04 — documented, not replaced
    assert accounts.LOGIN_FAIL_LIMIT == 5 and accounts.LOGIN_LOCK_MIN == 15


def test_override_merges_without_mutating_the_shipped_dict():
    before = json.loads(json.dumps(throttle.THROTTLE_RULES))
    rules = load_rules(override={"posting": {"per_user_limit": 5}})
    assert rules["posting"]["per_user_limit"] == 5
    assert load_rules()["posting"]["per_user_limit"] == 20  # constant untouched
    assert json.loads(json.dumps(throttle.THROTTLE_RULES)) == before


def test_unknown_rule_class_fails_open_with_a_marker(store):
    verdict = hit(store, "absolutely_not_configured", user="7", ip="1.2.3.4", t=10.0)
    assert verdict.allowed is True and verdict.kind == "fail_open"
    assert verdict.remaining is None and verdict.to_headers() == {}


def test_misconfigured_present_rule_fails_fast_not_open(store):
    bad = {"broken": {"per_user_limit": 0, "per_ip_limit": None, "window_seconds": 60,
                      "penalty_curve": "x", "source": "t"}}
    with pytest.raises(ValueError):
        hit(store, "broken", user="7", ip=None, t=10.0, rules=bad)


# ------------------------------------------------------ the dashboard read


def test_rules_summary_is_the_single_dashboard_read():
    summary = rules_summary()
    classes = {c["endpoint_class"]: c for c in summary["classes"]}
    assert set(classes) == {"auth", "messaging_send", "posting", "reports", "uploads"}
    assert classes["reports"]["per_user_limit"] == 10
    assert classes["reports"]["wired"] is True      # 15's DB door is authority
    assert classes["posting"]["wired"] is False     # awaiting parent wiring
    assert summary["strike_policy"]["first_lock_minutes"] == 60
    # pure + JSON-able for any dashboard consumer
    json.dumps(summary)


def test_rules_summary_store_stats_are_injectable(store):
    enforce(store, endpoint_class="posting", user_key="7", ip_key="1.1.1.1", now=1.0)
    summary = rules_summary(store=store)
    assert summary["store"] == {"backend": "memory", "keys": 2}  # user + ip keys


# ---------------------------------------------------------- purity (PARENT contract)


def test_purity_no_framework_imports_in_throttle_py():
    import inspect
    src = inspect.getsource(throttle)
    assert not re.search(r"(?m)^\s*(?:import django|from django)", src), (
        "apps/core/throttle.py must stay framework-free (the 11/30/32 pure-core pattern)"
    )


def test_throttle_imports_clean_without_web_framework():
    """Subprocess proof: importing the pure module pulls in no web framework."""
    code = (
        "import sys; sys.path.insert(0, {repo!r});"
        "import apps.core.throttle;"
        "assert 'django' not in sys.modules, 'leaked framework import';"
        "print('pure-ok')"
    ).format(repo=str(REPO))
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60
    )
    assert out.returncode == 0, out.stderr
    assert "pure-ok" in out.stdout


# ------------------------------------------------- the Django thin layer


@pytest.fixture
def resolver_stub():
    def make(app_name="moderation", url_name="report_create"):
        return SimpleNamespace(app_name=app_name, url_name=url_name)
    return make


@pytest.fixture
def tiny_rules(monkeypatch):
    """Fast middleware vehicle: 'testcls' (limit 2 + IP 3/60s) mapped to a
    fake route; the REAL moderation route stays mapped to the real class."""
    monkeypatch.setattr(settings, "THROTTLE_RESOLVER_CLASSES", {
        ("moderation", "report_create"): "reports",
        ("testapp", "write"): "testcls",
        ("testapp", "unconfigured"): "no_such_class",
    }, raising=False)  # the setting is parent-optional; absent in stock settings
    monkeypatch.setattr(settings, "THROTTLE_RULES_OVERRIDE", {
        "testcls": {"per_user_limit": 2, "per_ip_limit": 3, "window_seconds": 60,
                    "penalty_curve": "x", "source": "test"},
    }, raising=False)


def _request(path="/posts/new/", user=None, ip="10.0.0.1", match=None, method="get"):
    rf = RequestFactory(HTTP_USER_AGENT="tests")
    req = getattr(rf, method)(path, REMOTE_ADDR=ip)
    if user is None:
        req.user = AnonymousUser()
    else:
        req.user = user
    if match is not None:
        req.resolver_match = match
    return req


def test_middleware_passes_through_unmapped_routes(clean_default_store):
    mw = throttle_django.ThrottleMiddleware(lambda req: HttpResponse("real-view"))
    # a real namespace but one the shipped map deliberately does not list
    request = _request(match=SimpleNamespace(app_name="tracking", url_name="sessions"))
    response = mw(request)
    assert response.status_code == 200
    assert "X-RateLimit-Remaining" not in response


def test_middleware_allow_paths_carry_headers(clean_default_store, tiny_rules):
    mw = throttle_django.ThrottleMiddleware(lambda req: HttpResponse("real-view"))
    request = _request(match=SimpleNamespace(app_name="testapp", url_name="write"))
    response = mw(request)
    assert response.status_code == 200
    assert response["X-RateLimit-Remaining"] == "1"  # limit 2, one consumed
    assert response["X-RateLimit-Limit"] == "2"


def test_middleware_answers_429_with_retry_and_remaining_headers(clean_default_store, tiny_rules):
    mw = throttle_django.ThrottleMiddleware(lambda req: HttpResponse("real-view"))
    rf = RequestFactory()
    got_429 = None
    for i in range(3):
        request = rf.get("/posts/new/", REMOTE_ADDR="10.0.0.1")
        request.user = SimpleNamespace(is_authenticated=True, pk=7)
        request.resolver_match = SimpleNamespace(app_name="testapp", url_name="write")
        response = mw(request)
        if response.status_code == 429:
            got_429 = response
    assert got_429 is not None  # 3rd request (limit 2) refused
    assert got_429["Retry-After"].isdigit() and 0 < int(got_429["Retry-After"]) <= 60
    assert got_429["X-RateLimit-Remaining"] == "0"
    assert got_429["X-RateLimit-Limit"] == "2"


def test_middleware_locked_refusal_covers_the_whole_lock_duration(clean_default_store, tiny_rules):
    # burn the 2-hit class through the middleware: hits 3-5 are the strikes,
    # the 3rd over-limit hit engages the 60-minute lock (no clock fiddling)
    mw = throttle_django.ThrottleMiddleware(lambda req: HttpResponse("real-view"))
    rf = RequestFactory()
    last = None
    for _ in range(5):
        request = rf.get("/posts/new/", REMOTE_ADDR="10.0.0.1")
        request.user = SimpleNamespace(pk=7, is_authenticated=True)
        request.resolver_match = SimpleNamespace(app_name="testapp", url_name="write")
        last = mw(request)
    assert last is not None and last.status_code == 429
    # while the lock is fresh, Retry-After ~ the lock duration, not a slot
    request = rf.get("/posts/new/", REMOTE_ADDR="10.0.0.1")
    request.user = SimpleNamespace(pk=7, is_authenticated=True)
    request.resolver_match = SimpleNamespace(app_name="testapp", url_name="write")
    response = mw(request)
    assert response.status_code == 429
    retry = int(response["Retry-After"])
    assert 3550 <= retry <= 60 * 60  # ~ the 60-min lock, never the 60s slot


def test_middleware_sets_the_warning_flag_on_soft_warning(clean_default_store, tiny_rules):
    mw = throttle_django.ThrottleMiddleware(lambda req: HttpResponse("real-view"))
    rf = RequestFactory()
    request = rf.get("/posts/new/", REMOTE_ADDR="10.0.0.2")
    request.user = AnonymousUser()
    request.resolver_match = SimpleNamespace(app_name="testapp", url_name="write")
    assert mw(request).status_code == 200  # anon hit 1
    request2 = rf.get("/posts/new/", REMOTE_ADDR="10.0.0.2")
    request2.user = AnonymousUser()
    request2.resolver_match = SimpleNamespace(app_name="testapp", url_name="write")
    assert mw(request2).status_code == 200  # anon hit 2
    request3 = rf.get("/posts/new/", REMOTE_ADDR="10.0.0.2")
    request3.user = AnonymousUser()
    request3.resolver_match = SimpleNamespace(app_name="testapp", url_name="write")
    response3 = mw(request3)
    assert response3.status_code == 429
    assert request3.throttle_warning["kind"] == "reject"
    assert request3.throttle_warning["endpoint_class"] == "testcls"
    assert request3.throttle_warning["penalty_minutes"] is None  # soft warning


def test_middleware_anonymous_ip_only_path_on_user_only_class(clean_default_store, monkeypatch):
    monkeypatch.setattr(settings, "THROTTLE_RESOLVER_CLASSES", {
        ("testapp", "sendmsg"): "messaging_send",  # 100/day, user-only
    }, raising=False)
    mw = throttle_django.ThrottleMiddleware(lambda req: HttpResponse("real-view"))
    rf = RequestFactory()
    request = rf.get("/chats/3/", REMOTE_ADDR="7.7.7.7")
    request.user = AnonymousUser()
    request.resolver_match = SimpleNamespace(app_name="testapp", url_name="sendmsg")
    response = mw(request)
    assert response.status_code == 200
    assert response["X-RateLimit-Limit"] == "100"  # the USER limit applied to the IP
    store = throttle.get_store()
    assert any("u:ip/7.7.7.7" in key for key in store._data)


def test_middleware_reports_class_matches_real_moderation_limit(clean_default_store):
    # the REAL resolver name for the Prompt 15 door maps to the SAME limit 10
    mw = throttle_django.ThrottleMiddleware(lambda req: HttpResponse("real-view"))
    rf = RequestFactory()
    request = rf.get("/moderation/report/", REMOTE_ADDR="10.1.1.1")
    request.user = SimpleNamespace(pk=123, is_authenticated=True)
    request.resolver_match = SimpleNamespace(app_name="moderation", url_name="report_create")
    response = mw(request)
    assert response.status_code == 200
    assert response["X-RateLimit-Limit"] == "10"  # == moderation.MAX_REPORTS_PER_HOUR
