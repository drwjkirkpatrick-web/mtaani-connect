"""tests/test_crew_flow.py — Prompt 29: Driver/Conductor Low-Data Flow.

Acceptance coverage, provably (injected clock for service calls; view flows
run on the real clock — 27's own convention):

  * page machine — login wall; honest NOT_AUTHORIZED for logged-in non-crew
    (link to fleet's approval flow, NO start form); start via 27's PUBLIC
    door only (non-crew 403, foreign vehicle 403, no probing);
  * token discipline — the start POST response renders the raw token
    EXACTLY ONCE, into no cookie; a reload shows PUBLISHING minus token;
  * one-live-per-vehicle — 27 refuses the double start on the page;
  * end — through 27's door, honest ENDED summary (server-stored count +
    held device queue + 12 h cap note), refusal to rewrite a closed trip;
  * route pick honesty — a route the vehicle doesn't serve is refused;
  * page budget — every state < 20 KB (the repo's tightest),
    progressive enhancement markers,gettext markers;
  * JS contract pins — inline script markers (enableHighAccuracy FALSE
    always, maximumAge, poll/degrade intervals, versioned storage key,
    200-cap drop-OLDEST never-silent, accepted===true clear rule, pure
    counter formula, one write-through path, per-session queue) — keep the
    markers verbatim in apps/crew/templates/crew/trip.html;
  * flush/queue cycle — a Python MIRROR of the inline JS (no headless JS in
    this repo: the mirror + the marker pins together hold the contract)
    driven against a TEST-ONLY stub ingest route (apps.crew.tests_urls)
    standing in for 28's real endpoint: batch ≤ 50, posted EXACTLY ONCE
    with the X-Session-Token token header, cleared ONLY on accepted:true;
    offline/refused keeps the buffer; simulated 2G trip stays cheap.
"""
from __future__ import annotations

import json
import re
from datetime import timedelta
from pathlib import Path

import pytest
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone as dj_timezone

from apps.core.testing import assert_under_budget, mock_now
from apps.fleet import services as fleet_services
from apps.routes import crew as routes_crew
from apps.routes.models import Route, Sacco
from apps.tracking import services
from apps.tracking.models import AuthorizationSource, PingPoint

from apps.crew import views as crew_views
from apps.crew.tests_urls import STUB

REPO = Path(__file__).resolve().parents[1]
# THE CLOCK: direct SERVICE calls inject the fixed T0 (deterministic); view
# flows read a FRESH wall clock per assertion (vnow() below).
T0 = mock_now()


def vnow():
    """A FRESH wall-clock read for view-flow assertions.
    WHY not an import-time constant: views start sessions at the
    moment the request runs — a stale constant reads LATER rows as
    EXPIRED ("now < started_at"). Direct service calls below still
    inject their own deterministic T0."""
    return dj_timezone.now()

BATCH_MAX = crew_views.INGEST_BATCH_MAX
TOKEN_HEADER_ENV_KW = "HTTP_" + crew_views.INGEST_HEADER.replace("-", "_").upper()
INGEST_URL = crew_views.INGEST_URL_TEMPLATE

# The crew app is not in the (parent-owned) INSTALLED_APPS or root urls yet
# — my tests own the mounting, so nothing shared changes for this prompt:
TEST_TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [REPO / "templates", REPO / "apps" / "crew" / "templates"],
        "APP_DIRS": False,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.template.context_processors.i18n",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    }
]


@pytest.fixture
def crew_urls(db):
    """Mount the crew app + the test ingest stub WITHOUT shared files.

    ALSO resets the module-level STUB (calls + mode) around every test —
    the stub is a shared mock holder; leaking calls between tests would
    make "posted exactly once" assertions lie."""
    STUB.calls.clear()
    STUB.mode = "accepted"
    with override_settings(
        ROOT_URLCONF="apps.crew.tests_urls", TEMPLATES=TEST_TEMPLATES
    ):
        yield
    STUB.calls.clear()
    STUB.mode = "accepted"


# ----------------------------------------------------------------- fixtures
@pytest.fixture
def world(db):
    """Two SACCOs: crew (driver seat + two routes on vehicle A), crew_b,
    their admins, a PENDING applicant and a plain member."""
    User = get_user_model()
    staff = User.objects.create_superuser("staff_t29", "staff_t29@example.com", "***")
    sacco_a = Sacco.objects.create(name="Ngong Road Sacco")
    sacco_b = Sacco.objects.create(name="Kinoo Riders")
    admin_a = User.objects.create_user("op_a_t29", "a@example.com", "***")
    admin_b = User.objects.create_user("op_b_t29", "b@example.com", "***")
    crew = User.objects.create_user("crew_t29", "crew@example.com", "***")
    crew_b = User.objects.create_user("crew_b_t29", "crewb@example.com", "***")
    pending = User.objects.create_user("pending_t29", "p@example.com", "***")
    plain = User.objects.create_user("plain_t29", "plain@example.com", "***")
    fleet_services.grant_sacco_admin(user=admin_a, sacco=sacco_a, granter=staff, now=T0)
    fleet_services.grant_sacco_admin(user=admin_b, sacco=sacco_b, granter=staff, now=T0)
    r_in = Route.objects.create(name_en="CBD–Kikuyu 106", name_sw="")
    r_back = Route.objects.create(name_en="Kikuyu–CBD (106 return)", name_sw="")
    r_else = Route.objects.create(name_en="CBD–Ruiru 107", name_sw="")
    veh_a = fleet_services.register_vehicle(
        sacco=sacco_a, raw_plate="KAA 321M", capacity=33, nickname="Imara",
        routes=[r_in, r_back], actor=admin_a, now=T0,
    )
    veh_b = fleet_services.register_vehicle(
        sacco=sacco_b, raw_plate="KBX 909N", capacity=14, nickname="Kariakose",
        routes=[r_else], actor=admin_b, now=T0,
    )
    seat_a = fleet_services.request_crew(user=crew, vehicle=veh_a, role="DRIVER", now=T0)
    fleet_services.approve_crew(seat_a, approver=admin_a, now=T0)
    seat_b = fleet_services.request_crew(user=crew_b, vehicle=veh_b, role="DRIVER", now=T0)
    fleet_services.approve_crew(seat_b, approver=admin_b, now=T0)
    pending_seat = fleet_services.request_crew(
        user=pending, vehicle=veh_a, role="CONDUCTOR", now=T0
    )
    return {
        "vehicle": veh_a, "vehicle_b": veh_b, "crew": crew, "crew_b": crew_b,
        "admin_a": admin_a, "admin_b": admin_b, "pending": pending,
        "plain": plain, "pending_seat": pending_seat,
        "routes": [r_in, r_back], "route_b": r_else,
    }


def _client_for(client, user):  # 27's helper shape
    client.force_login(user)
    return client


def _raw_token(body: str) -> str:
    """Pull the ONE-TIME raw token (27's prefix contract) from a page."""
    match = re.search(r"mtk_[A-Za-z0-9_-]+", body)
    return match.group(0) if match else ""


def _start_live(client, world, **form):
    """Start a live trip through the crew page; returns the raw token."""
    form.setdefault("vehicle", world["vehicle"].pk)
    form.setdefault("route", "")
    form.setdefault("direction", "FORWARD")
    response = client.post(reverse("crew:trip_start"), form)
    assert response.status_code == 200
    return _raw_token(response.content.decode())


# ================================== Python MIRRORS of the page's inline JS
def compact(obj) -> str:
    """JS JSON.stringify equivalent (no spaces) — byte math is exact."""
    return json.dumps(obj, separators=(",", ":"))


def kb_counter(req_bytes: int, res_bytes: int) -> float:
    """Mirror of the JS counter `toKB` — the SAME pure formula:
    round(((reqBytes + resBytes) / 1024) * 10) / 10. The inline script's
    marker pin (test below) keeps the two from drifting apart."""
    return round(((req_bytes + res_bytes) / 1024) * 10) / 10


def _ping(i: int, session) -> dict:
    """One synthetic fix the way the JS builds it (rounded, contract keys
    + the per-session tag the queue keeps, which the body strips)."""
    return {
        "session_id": session.pk,
        "recorded_at": (T0 + timedelta(minutes=i)).isoformat(),
        "lat": round(-1.286400 + (i % 9) * 0.000001, 6),
        "lon": round(36.817200 + (i % 50) * 0.001, 6),
        "speed_kmh": 28 + (i % 7),
    }


def _contract_ping(p: dict) -> dict:
    """The body only carries 28's documented per-ping keys (mirror of the
    JS `map()` that strips the session tag)."""
    return {k: p[k] for k in ("recorded_at", "lat", "lon", "speed_kmh")}


class Flusher:
    """Python mirror of the inline JS queue/flush cycle (page + tests hold
    the SAME contract; the marker pins in test_inline_javascript_contract
    keep template and mirror in step):

      * queue(): write-through per fix, capped at BUFFER_MAX (200) dropping
        the OLDEST, returning the drop count for the VISIBLE notice;
      * flush(): NO-OP when offline or without the one-time token; posts
        the first ≤ BATCH_MAX (50) buffered fixes ONCE as
        {"vehicle_id", "pings"} with the X-Session-Token token header;
        clears the posted prefix ONLY on r.ok + body accepted === true;
        counter += toKB(request bytes, response bytes).
    """

    def __init__(self, client, *, session, token, vehicle_id):
        self.client = client
        self.session_id = session.pk
        self.vehicle_id = vehicle_id
        self.token = token
        self.buffer = []
        self.counter_kb = 0.0
        self.last_call_count = 0

    def queue(self, ping: dict) -> int:
        self.buffer.append(ping)
        dropped = 0
        while len(self.buffer) > crew_views.BUFFER_MAX:
            self.buffer.pop(0)
            dropped += 1
        return dropped

    def flush(self, *, online: bool = True):
        if not online or self.token is None or not self.buffer:
            return None  # JS guards: offline, tokenless tab, empty batch
        batch = list(self.buffer[:BATCH_MAX])
        body = compact(
            {"vehicle_id": self.vehicle_id, "pings": [_contract_ping(p) for p in batch]}
        )
        STUB.calls.clear()  # mirror ONE fetch per flush ("exactly once")
        response = self.client.post(
            _ingest_url(session_id=self.session_id),
            data=body,
            content_type="application/json",
            **{TOKEN_HEADER_ENV_KW: self.token},
        )
        try:
            data = response.json()
        except ValueError:
            data = None
        self.counter_kb += kb_counter(len(body.encode("utf-8")), len(response.content))
        if response.status_code == 200 and data and data.get("accepted") is True:
            self.buffer = self.buffer[len(batch):]  # ONLY on accepted true
        self.last_call_count = len(STUB.calls)
        return data


def _ingest_url(*, session_id: int) -> str:
    return INGEST_URL.format(session_id=session_id)


def boot_clear(buffer, *, session_id: int):
    """Mirror of the one-time render's boot-cleanup: pings held for a
    DIFFERENT (ended) session can never be accepted again — dropped with
    a visible notice (stale count > 0), never smuggled into the new trip."""
    fresh = [p for p in buffer if str(p.get("session_id")) == str(session_id)]
    stale = len(buffer) - len(fresh)
    return fresh, stale


# ========================================================= the page machine
def test_trip_requires_login(world, client, crew_urls):
    client.logout()
    response = client.get(reverse("crew:trip"))
    assert response.status_code == 302
    assert "login" in response["Location"].lower()
    # the status partial is behind the same wall:
    assert client.get(reverse("crew:trip_status")).status_code == 302


def test_trip_honest_without_crew_approval(world, client, crew_urls):
    """A logged-in non-crew gets the honest refusal state: link to the
    fleet approval flow, and NO start button to fake-press."""
    body = _client_for(client, world["plain"]).get(reverse("crew:trip")).content.decode()
    assert 'data-test="not-authorized"' in body
    assert reverse("fleet:crew_mine") in body
    assert 'data-test="start-form"' not in body
    assert "Start publishing" not in body
    # The pending applicant is equally honest (seat approval is the gate).
    body = _client_for(client, world["pending"]).get(reverse("crew:trip")).content.decode()
    assert 'data-test="not-authorized"' in body


def test_trip_start_refuses_non_crew(world, client, crew_urls):
    """Start runs through 27's door ONLY: plain member or pending applicant
    POSTing is 403 and creates NOTHING."""
    for user in (world["plain"], world["pending"]):
        client = _client_for(client, user)
        response = client.post(reverse("crew:trip_start"), {"vehicle": world["vehicle"].pk})
        assert response.status_code == 403
        # even a missing/blank vehicle can't probe the machinery:
        blank = client.post(reverse("crew:trip_start"), {"vehicle": ""})
        assert blank.status_code == 403
    assert services.live_session_for(world["vehicle"], now=vnow()) is None


def test_trip_start_renders_the_token_exactly_once(world, client, crew_urls):
    """GET lists ONLY the crew's own vehicle (tenant scoping) and the POST
    renders the raw token ONCE — in page memory, never a cookie."""
    client = _client_for(client, world["crew"])
    form = client.get(reverse("crew:trip"))
    body = form.content.decode()
    assert form.status_code == 200
    assert 'data-state="AUTHORIZED_IDLE"' in body
    assert "KAA-321-M" in body and "KBX-909-N" not in body
    assert "CBD–Kikuyu 106" in body and "CBD–Ruiru 107" not in body
    response = client.post(
        reverse("crew:trip_start"),
        {"vehicle": world["vehicle"].pk, "route": world["routes"][0].pk,
         "direction": "REVERSE"},
    )
    assert response.status_code == 200
    body = response.content.decode()
    raw = _raw_token(body)
    assert raw, "the one-time render must show the token"
    assert body.count("mtk_") == 1  # exactly one display, exactly once
    assert raw not in str(response.cookies)  # NO lingering credential
    assert 'data-state="PUBLISHING"' in body
    # The session is REAL and came from 27's door (CREW source, this user):
    session = services.live_session_for(world["vehicle"], now=vnow())
    assert session is not None
    assert session.authorized_by_id == world["crew"].pk
    assert session.authorization_source == AuthorizationSource.CREW.value
    assert services.session_status(session, now=vnow()) == "LIVE"
    # The token works at 27's door (scope-baked):
    assert services.verify_session_token(
        raw, session_id=session.pk, vehicle_id=session.vehicle_id
    ) == session


def test_reload_shows_state_minus_token(world, client, crew_urls):
    """Display-once has teeth in the flow: after the ONE-TIME render, a
    plain GET shows PUBLISHING with NO token anywhere."""
    client = _client_for(client, world["crew"])
    _start_live(client, world)
    body = client.get(reverse("crew:trip")).content.decode()
    assert 'data-state="PUBLISHING"' in body
    assert "mtk_" not in body
    assert '<code data-test="raw-token">' not in body  # the element is gone
    # but the trip is not a lie: the end control is right there
    assert 'data-test="trip-end-form"' in body
    assert "show-once token" in body  # the honest no-flush note


def test_second_start_refused_by_p27s_one_live_rule(world, client, crew_urls):
    client = _client_for(client, world["crew"])
    _start_live(client, world)
    second = client.post(
        reverse("crew:trip_start"), {"vehicle": world["vehicle"].pk}, follow=True
    )
    body = second.content.decode()
    assert "already has a live tracking session" in body  # 27's refusal verbatim
    assert world["vehicle"].tracking_sessions.count() == 1  # nothing doubled
    assert services.live_session_for(world["vehicle"], now=vnow()) is not None
    assert 'data-state="PUBLISHING"' in body  # page reflects the live trip


def test_trip_end_stops_publishing_and_summarizes_honestly(world, client, crew_urls):
    """POST end → 27's door stops it; the page honestly reports ENDED with
    the server-stored count and the 12 h cap reminder."""
    client = _client_for(client, world["crew"])
    _start_live(client, world)
    session = services.live_session_for(world["vehicle"], now=vnow())
    PingPoint.objects.create(
        session=session, lat="-1.2864", lon="36.8172",
        recorded_at=vnow(), received_at=vnow(),
    )
    response = client.post(reverse("crew:trip_end", args=[session.pk]))
    assert response.status_code == 302
    assert f"?ended={session.pk}" in response["Location"]
    # The ENDED summary renders on the REDIRECT TARGET (the ?ended= state):
    body = client.get(f"{reverse('crew:trip')}?ended={session.pk}").content.decode()
    assert 'data-state="ENDED"' in body
    assert "Stored by the server: 1" in body  # the honest server-stored count
    assert "12 h — the session cap" in body  # the cap reminder
    session.refresh_from_db()
    assert session.ended_reason == "STOPPED"
    assert services.session_status(session, now=vnow()) == "ENDED"


def test_second_end_is_refused_without_a_rewrite(world, client, crew_urls):
    client = _client_for(client, world["crew"])
    _start_live(client, world)
    session = services.live_session_for(world["vehicle"], now=vnow())
    assert client.post(reverse("crew:trip_end", args=[session.pk])).status_code == 302
    assert services.live_session_for(world["vehicle"], now=vnow()) is None
    second = client.post(reverse("crew:trip_end", args=[session.pk]), follow=True)
    assert "already ended" in second.content.decode()  # honest refusal
    from apps.tracking.models import TrackingSession as TS

    row = TS.objects.get(pk=session.pk)
    assert row.ended_reason == "STOPPED"  # no rewrite, no extension


def test_foreign_vehicle_and_session_are_tenancy_refused(world, client, crew_urls):
    """A forged vehicle pk on an approved crew of ANOTHER sacco is 403;
    ending someone else's session is 403; their OWN start still works."""
    client = _client_for(client, world["crew_b"])
    foreign = client.post(reverse("crew:trip_start"), {"vehicle": world["vehicle"].pk})
    assert foreign.status_code == 403
    assert services.live_session_for(world["vehicle"], now=vnow()) is None
    session, raw = services.start_session(world["vehicle"], by=world["crew"], now=vnow())
    ending = client.post(reverse("crew:trip_end", args=[session.pk]))
    assert ending.status_code == 403  # not crew on THAT vehicle
    # the door wasn't a blanket block — their seat works on their metal:
    own = client.post(
        reverse("crew:trip_start"), {"vehicle": world["vehicle_b"].pk}, follow=True
    )
    assert own.status_code == 200 and 'data-state="PUBLISHING"' in own.content.decode()


def test_route_pick_must_be_the_vehicle_s_own(world, client, crew_urls):
    client = _client_for(client, world["crew"])
    response = client.post(
        reverse("crew:trip_start"),
        {"vehicle": world["vehicle"].pk, "route": world["route_b"].pk},
        follow=True,
    )
    body = response.content.decode()
    assert "actually serves" in body  # honest refusal, nothing started
    assert services.live_session_for(world["vehicle"], now=vnow()) is None


# ==================================================== status JSON + budget
def test_status_json_exact_keys_across_states(world, client, crew_urls):
    """The partial NEVER disagrees with the page (one shared machine);
    the exact key set is pinned."""
    client = _client_for(client, world["crew"])
    payload = client.get(reverse("crew:trip_status")).json()
    assert set(payload) == {"state", "session_id", "vehicle_id", "plate", "pings_stored"}
    assert payload["state"] == "AUTHORIZED_IDLE" and payload["session_id"] is None
    session, raw = services.start_session(world["vehicle"], by=world["crew"], now=vnow())
    payload = client.get(reverse("crew:trip_status")).json()
    assert payload["state"] == "PUBLISHING"
    assert payload["session_id"] == session.pk and payload["plate"] == "KAA-321-M"
    services.end_session(session, by=world["crew"], now=vnow() + timedelta(minutes=5))
    payload = client.get(reverse("crew:trip_status")).json()
    assert payload["state"] == "AUTHORIZED_IDLE"  # nothing live → honest idle
    # plain member, same wall as the page:
    payload = _client_for(client, world["plain"]).get(reverse("crew:trip_status")).json()
    assert payload["state"] == "NOT_AUTHORIZED"


def test_page_budget_all_states_under_20kb(world, client, crew_urls):
    """The TIGHTEST budget in the repo: the crew page renders < 20 KB in
    every state (pinned, not aspirational)."""
    client = _client_for(client, world["crew"])
    assert_under_budget(client.get(reverse("crew:trip")).content.decode(), kb=20)
    assert_under_budget(
        _client_for(client, world["plain"]).get(reverse("crew:trip")).content.decode(),
        kb=20,
    )
    # Crew again — the shared client remembers its last login (plain's).
    _client_for(client, world["crew"])
    _start_live(client, world)
    assert_under_budget(client.get(reverse("crew:trip")).content.decode(), kb=20)
    session = services.live_session_for(world["vehicle"], now=vnow())
    client.post(reverse("crew:trip_end", args=[session.pk]))
    assert_under_budget(
        client.get(f"{reverse('crew:trip')}?ended={session.pk}").content.decode(), kb=20
    )


def test_page_renders_and_works_without_javascript(world, client, crew_urls):
    """Progressive enhancement: real form POSTs + literal dash counters;
    the flow tests above already prove start/end work with zero JS."""
    source = (REPO / "apps" / "crew" / "templates" / "crew" / "trip.html").read_text()
    assert "<script src=" not in source  # no crew-side external JS file
    client = _client_for(client, world["crew"])
    idle = client.get(reverse("crew:trip")).content.decode()
    assert 'method="post" action="/crew/trip/start/"' in idle
    assert 'data-test="no-js-fallback"' in idle  # the honest no-JS note
    # the counters carry their literal no-JS dashes on a publishing render:
    _start_live(client, world)
    body = client.get(reverse("crew:trip")).content.decode()
    assert '<span data-sent-kb>—</span>' in body
    assert '<span data-queued>—</span>' in body


# ============================================ JS contract pins (no headless)
def test_inline_javascript_contract_pins(world, client, crew_urls):
    """Marker pins for the INLINE JS (apps/crew/templates/crew/trip.html) —
    the battery/data contract of the page, asserted against a render:
    keep every marker verbatim when editing the template."""
    _client_for(client, world["crew"])
    _start_live(client, world)
    page = client.get(reverse("crew:trip")).content.decode()
    # SEAM attributes rendered from the module's constants (28's contract):
    assert f'data-ingest-url="{INGEST_URL}"' in page
    assert f'data-ingest-header="{crew_views.INGEST_HEADER}"' in page
    assert 'data-batch-max="50"' in page
    assert 'data-buffer-max="200"' in page
    assert f'data-storage-key="{crew_views.STORAGE_KEY}"' in page
    # battery pitfall pins: LOW-accuracy ALWAYS, cached coarse fixes,
    # interval polling that DEGRADES after failures:
    assert "var HIGH_ACCURACY = false" in page
    assert "enableHighAccuracy: HIGH_ACCURACY" in page
    assert re.search(r"enableHighAccuracy\s*:\s*true", page) is None
    assert "var MAX_AGE_MS = 120000" in page and "maximumAge: MAX_AGE_MS" in page
    assert "var POLL_MS = 60000" in page
    assert "var POLL_MS_DEGRADED = 120000" in page
    # queue integrity pins: versioned key, 200-cap, drop-OLDEST never silent,
    # ONE write-through path, per-session entries:
    assert "var BUFFER_MAX = 200" in page
    assert "OLDEST" in page
    assert len(re.findall(r"localStorage\.setItem", page)) == 1
    assert '"mtaani.crew.trip.v1"' in page
    assert "session_id" in page and "String(p.session_id)" in page
    # flush/seam pins: batch, token header from data attrs, EXACT clear rule:
    assert "var BATCH_MAX = 50" in page
    assert "accepted === true" in page
    assert "memory-only" in page  # token never enters storage/cookies
    # counter pin: the PURE formula marker (mirror = same math, pinned):
    assert "((reqBytes + resBytes) / 1024)" in page
    assert 'replace("{session_id}"' in page
    assert 'addEventListener("online"' in page


def test_crew_strings_are_gettext_marked():
    """Hard rule probe (27's pattern): user-facing strings through gettext
    in modules, {% trans %}/{% blocktranslate %} in the template."""
    views_source = (REPO / "apps" / "crew" / "views.py").read_text(encoding="utf-8")
    assert '_("' in views_source, "views.py lacks gettext markers"
    template = (REPO / "apps" / "crew" / "templates" / "crew" / "trip.html").read_text()
    assert "{% trans" in template or "{% blocktranslate" in template


# ================================================ mirrors of the device side
def test_counter_math_python_mirror_of_pure_function():
    """The counter formula, pinned numerically: (req+res)/1024 → 1 decimal;
    accumulation across flushes (the page's counter is cumulative)."""
    assert kb_counter(500, 300) == 0.8  # (500+300)/1024 = 0.78125 → 0.8
    assert kb_counter(0, 0) == 0.0
    assert kb_counter(10_000, 0) == 9.8  # 10000/1024 = 9.765625 → 9.8
    # accumulation across flushes (the page's counter is cumulative):
    total = kb_counter(4_000, 58) + kb_counter(1_200, 58)
    assert total == 4.0 + 1.2


def test_buffer_queue_caps_at_200_dropping_oldest(world, client, crew_urls):
    """Integrity: the queue survives anything BUT only to 200 pings; the
    OLDEST go first, each return a drop count for the visible notice."""
    client = _client_for(client, world["crew"])
    session, raw = services.start_session(world["vehicle"], by=world["crew"], now=T0)
    f = Flusher(client, session=session, token=raw, vehicle_id=world["vehicle"].pk)
    first = _ping(0, session)
    for i in range(201):
        f.queue(_ping(i, session))
    assert len(f.buffer) == 200
    assert f.buffer[0] == _ping(1, session)  # the OLDEST fix went out
    assert f.counter_kb == 0.0 and STUB.calls == []  # queueing alone never sends


def test_flush_cycle_posts_batch_once_then_clears_on_accepted(world, client, crew_urls):
    """THE flush/queue cycle against the TEST-ONLY stub ingest route: the
    buffer posts in ≤ 50-fix batches, EXACTLY ONCE per flush, with the one-
    time token under X-Session-Token, and clears ONLY on accepted:true."""
    client = _client_for(client, world["crew"])
    session, raw = services.start_session(world["vehicle"], by=world["crew"], now=T0)
    f = Flusher(client, session=session, token=raw, vehicle_id=world["vehicle"].pk)
    for i in range(120):
        f.queue(_ping(i, session))
    # flush #1 — posted exactly once, the FIRST batch only:
    data = f.flush()
    assert data == {"accepted": True, "duplicates": 0, "downgraded": 0}
    assert f.last_call_count == 1  # exactly one POST per flush
    posted = STUB.calls[0]
    assert posted["path"] == _ingest_url(session_id=session.pk)
    assert posted["token"] == raw  # the one-time token IS the credential
    assert set(posted["body"]) == {"vehicle_id", "pings"}
    assert posted["body"]["vehicle_id"] == world["vehicle"].pk
    pings = posted["body"]["pings"]
    assert len(pings) == BATCH_MAX  # batch ≤ 50 even with 120 buffered
    for ping in pings:
        assert set(ping) == {"recorded_at", "lat", "lon", "speed_kmh"}
    assert json.dumps(pings[0], separators=(",", ":")) == compact(
        _contract_ping(_ping(0, session))
    )
    assert f.buffer[0] == _ping(50, session) and len(f.buffer) == 70
    # flush #2 — the next batch: still exactly once, still sequential:
    assert f.flush() is not None
    assert f.last_call_count == 1 and len(STUB.calls[0]["body"]["pings"]) == 50
    assert f.buffer[0] == _ping(100, session)  # posted prefix cleared in order
    # flush #3 — the tail (20 fixes): full clear on acceptance:
    assert f.flush() is not None
    assert f.buffer == []  # cleared ONLY on accepted — which it was


def test_offline_and_refused_flushes_keep_the_buffer(world, client, crew_urls):
    """Integrity: an offline moment and a server refusal each keep the
    buffer EXACTLY as queued — 'cleared ONLY on accepted:true'."""
    client = _client_for(client, world["crew"])
    session, raw = services.start_session(world["vehicle"], by=world["crew"], now=T0)
    f = Flusher(client, session=session, token=raw, vehicle_id=world["vehicle"].pk)
    for i in range(30):
        f.queue(_ping(i, session))
    # navigator.onLine false → the mirror (like the JS) sends NOTHING:
    assert f.flush(online=False) is None
    assert STUB.calls == [] and len(f.buffer) == 30
    # server refuses: the POST happens (data was spent — that's honest, the
    # counter counts it) but the buffer keeps every queued fix:
    STUB.mode = "refused"
    try:
        refused = f.flush()
    finally:
        STUB.mode = "accepted"
    assert refused == {"accepted": False, "duplicates": 0, "downgraded": 0}
    assert f.last_call_count == 1  # the attempt cost data and counts
    assert f.counter_kb > 0
    assert len(f.buffer) == 30 and f.buffer[0] == _ping(0, session)  # intact
    # and when connectivity returns, the SAME buffer flows once and clears:
    assert f.flush() is not None
    assert f.buffer == [] and f.last_call_count == 1


def test_tokenless_reloaded_tab_cannot_send(world, client, crew_urls):
    """Display-once consequence, pinned structurally: a tab without the
    token (a reload) never sends a batch; the queue is preserved."""
    client = _client_for(client, world["crew"])
    session, raw = services.start_session(world["vehicle"], by=world["crew"], now=T0)
    f = Flusher(client, session=session, token=None, vehicle_id=world["vehicle"].pk)
    for i in range(5):
        f.queue(_ping(i, session))
    assert f.flush() is None  # the JS returns before any fetch
    assert STUB.calls == []
    assert len(f.buffer) == 5  # held honestly, sent from the armed tab only


def test_boot_clears_dead_session_pings_with_notice(world, client, crew_urls):
    """A NEW trip's boot drops pings held for a DIFFERENT (ended) session,
    with a visible notice — they can never be accepted again, so they are
    cleared loudly rather than smuggled into the new session's pipeline."""
    client = _client_for(client, world["crew"])
    old, raw_old = services.start_session(world["vehicle"], by=world["crew"], now=T0)
    f = Flusher(client, session=old, token=raw_old, vehicle_id=world["vehicle"].pk)
    for i in range(4):
        f.queue(_ping(i, old))
    services.end_session(old, by=world["crew"], now=T0 + timedelta(minutes=9))
    new, raw_new = services.start_session(world["vehicle"], by=world["crew"], now=T0)
    fresh, stale = boot_clear(f.buffer, session_id=new.pk)
    assert stale == 4 and fresh == []  # all held fixes belong to the dead trip
    f.buffer = fresh
    f.token = raw_new
    assert f.flush() is None  # nothing smuggled: no POST with the new token
    assert STUB.calls == []


def test_simulated_2g_trip_publishes_cheaply(world, client, crew_urls):
    """ACCEPTANCE: a crew phone publishes a trip on a simulated 2G crawl,
    cheaply — deterministic byte math over the mirrored cycle."""
    client = _client_for(client, world["crew"])
    session, raw = services.start_session(world["vehicle"], by=world["crew"], now=T0)
    f = Flusher(client, session=session, token=raw, vehicle_id=world["vehicle"].pk)
    # One trip-hour: fixes every 60 s, 2G repeatedly dropping mid-hour.
    for i in range(60):
        f.queue(_ping(i, session))
        f.flush(online=False)  # ...but 2G cannot push yet, queue holds
    # connectivity blips twice — each time a full-or-partial batch goes out:
    while f.buffer:
        data = f.flush()
        assert data == {"accepted": True, "duplicates": 0, "downgraded": 0}
    assert f.counter_kb > 0
    # Cheapness bound (deterministic): ~4 KB per ≤50-fix batch + tiny replies
    # → a full trip-hour stays an order below a single SMS's data cost:
    assert f.counter_kb < 20.0, f.counter_kb
    # …and the batch NEVER streamed: every POST was the ≤ 50 batch, once:
    assert f.last_call_count == 1
    assert f.buffer == []  # the trip-hour ended with a fully drained queue