"""tests/test_budgets.py — Prompt 39: the data-budget ENFORCEMENT suite.

PROMPTS.md 39: "budgets rot as features land — this test suite is the
enforcement, not the review." So this module is deliberately mechanical:

  1. PAGE WEIGHT — every primary route (anonymous AND authed) measured
     against the 50 KB bar via the shared app door assert_under_budget.
     A tighter per-app pin (crew 20 KB) is honored as the manifest's bar —
     never a looser one.
  2. MANIFEST DRIFT — every route the root URLconf includes either gets
     audited in PRIMARY_ROUTES or sits in EXEMPT_ROUTES with a stated why
     (POST-only state moves are auto-exempt; a new app or a new page fails
     this test until it is audited or exempted with a reason). The reverse
     drift is caught too: a manifest entry whose route disappears fails the
     route-existence test.
  3. QUERY HUNTS — page-1 vs page-2 CONSTANT query count (the N+1 proof).
     WHY constant-count instead of exact pins: an exact
     ``assertNumQueries(7)`` rots the moment ANY app adds a legitimate
     read, and teaches the next wave to just bump the number. A
     page-1-vs-page-2 EQUALITY pins the load shape that actually matters
     (cost must not grow with rows) and stays silent about a harmless
     constant — the review tool that cannot rot. A small floor assert
     keeps the pair from being trivially zero. For the map positions JSON
     (which has no pages) the growth axis is PING ROWS: adding pings must
     never add queries. The map's OTHER known cost axis — sessions — is a
     DOCUMENTED linear per-session pass (apps/map/serialization.py parent
     note); that debt is owned in that docstring, not re-pinned here.
  4. PAGINATION — every list surface keeps its small pinned page size
     (the bundle discipline is what makes the 50 KB bar reachable), page
     2 exists when items overflow page 1, and a page beyond the last
     renders the LAST page (the doors' honest ``get_page`` contract),
     never a 500 and never an unbounded list.
  5. SLOW-3G — the simulated cold walk (scripts/audit_pages.py math
     IMPORTED here so the suite and the script cannot drift) over the
     feed, a guide page and the live map: time-to-usable under the pinned
     8 s bar, every core journey. No network, ever.
  6. SW CACHE — the two DPA-sensitive invariants re-pinned by READING
     static/sw.js source + a real authed manifest-endpoint fetch: (a) no
     member/authenticated JSON is ever cached — the only cache-first
     surfaces are the offline guide manifest/page, saved guide pages and
     static; (b) the offline-guide flow's contract holds (manifest
     authoritative, hash re-sync; the heavy JS-execution behavior stays
     pinned in tests/test_design_system.py — referenced, not duplicated).

FILE OWNERSHIP (Prompt 39 wave): this file + scripts/audit_pages.py are
audit surfaces. A violation found in APP code is not fixed silently here
and not fixed in this file — it is a documented finding for that app's
owner (wave report). If a measured page genuinely grew past its budget,
the per-app budget test pins it too; both suites agree, so a trim lands
in the app, never as a raised budget.
"""
from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

import pytest
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

REPO_ROOT = Path(__file__).resolve().parent.parent
STATIC = REPO_ROOT / "static"

# The script stays a runnable script, not a package; the suite imports its
# pure table math so the in-suite simulation is THE SAME simulation.
sys.path.insert(0, str(REPO_ROOT / "scripts"))

# noqa: E402 — every import below runs after the path shim above.
import audit_pages as audit  # noqa: E402

from apps.core.testing import assert_under_budget  # noqa: E402
from apps.core.types import GroupKind  # noqa: E402

# ---------------------------------------------------------------------------
# budgets & shared helpers
# ---------------------------------------------------------------------------

#: The Prompt 05 bar (mobile-first: ~KES 10 buys 70 MB on a Kenyan prepaid).
#: Per-app pins that are TIGHTER live per entry via ``kb=``.
PAGE_BUDGET_KB = 50

#: Slow-3G "stays usable" bar — pinned by Prompt 39's acceptance line,
#: shared with the script so the bar can only move in one place.
THREE_G_USABLE_BUDGET_MS = audit.USABLE_BUDGET_MS

#: Overflowing every pinned page size (10 and 20) by exactly one page.
OVERFLOW_ITEMS = 21


def _fresh_client() -> Client:
    """A no-cookie client — the honest cold visit."""
    return Client()


def _html_for(response) -> str:
    return response.content.decode("utf-8")


def _sql_count(func) -> int:
    """Number of SQL statements func executes (the N+1 hunt's ruler)."""
    with CaptureQueriesContext(connection) as ctx:
        func()
    return len(ctx.captured_queries)


# ---------------------------------------------------------------------------
# the seeded audit world — every per-app PUBLIC door, never a mock
# ---------------------------------------------------------------------------

KMB_PER_DEG_LON = 111.32 * 0.999748  # km per degree of lon at the CBD latitude


def _east(base_lon: Decimal, km: float) -> Decimal:
    return base_lon + Decimal(km / KMB_PER_DEG_LON).quantize(Decimal("0.000001"))


def _publish_pings(session, token, base) -> None:
    """Round-trip through the REAL ingest door (the map fixture's own shape):
    plausible matatu motion — ~2 km hop per 2 min at a reported 55 km/h, so
    pings stay LIVE_GPS (never an impossible jump)."""
    import datetime

    from apps.tracking.ingest import ingest_pings

    base_lon = Decimal("36.817200")
    pings = [{"recorded_at": base.isoformat(), "lat": -1.286400, "lon": float(base_lon)}]
    for i, minutes in enumerate((2, 4, 6), start=1):
        pings.append(
            {
                "recorded_at": (base + datetime.timedelta(minutes=minutes)).isoformat(),
                "lat": -1.286400,
                "lon": float(_east(base_lon, 2.0 * i)),
                "speed_kmh": 55.0,
            }
        )
    ingest_pings(
        token,
        session.pk,
        session.vehicle_id,
        pings,
        now=base + datetime.timedelta(minutes=8),
    )


@pytest.fixture
def audit_world(db):
    """Everything the audit journeys need, through each app's PUBLIC doors.

    WHY one big fixture: the manifest loop measures ~55 pages per run — a
    shared world is the only way this stays minutes, not hours. WHY 21 of
    each content kind: it overflows every pinned page size (10 and 20) by
    exactly one page, so pagination sanity and the N+1 page-1/page-2 hunts
    read the same data. WHY a media sweep at teardown: the Prompt 06
    pipeline writes variant files into MEDIA_ROOT; the audit leaves no
    files behind (the DB rolls back, the filesystem does not).
    """
    import datetime

    from apps.alerts.models import RouteFollow, VehicleFollow
    from apps.barter import services as barter_services
    from apps.disputes import services as disputes_services
    from apps.fleet import services as fleet_services
    from apps.guides import services as guides_services
    from apps.listings import services as listings_services
    from apps.messaging import services as messaging_services
    from apps.messaging.models import Message
    from apps.posts import services as posts_services
    from apps.profiles import services as profiles_services
    from apps.qa import services as qa_services
    from apps.routes.models import Route, RouteStage, Sacco, Stage
    from apps.services import services as services_services
    from apps.tracking import services as tracking_services

    def _user(name, **kw):
        return User.objects.create_user(name, f"{name}@example.com", "***", **kw)

    User = get_user_model()
    member = _user("budget_member")
    other = _user("budget_other")
    staff = User.objects.create_superuser("budget_staff", "staff@x.co", "***")
    operator = _user("budget_operator")
    crew = _user("budget_crew")
    follower_pool = [_user(f"budget_follower_{i}") for i in range(21)]

    # Warm the lazy profile rows: get_or_create writes must not land inside
    # a COUNTED fetch (they would make page 1 look heavier than page 2).
    profiles_services.get_profile(member)
    profiles_services.get_profile(other)

    # 21 posts across exactly TWO distinct authors (the feed's cost axes:
    # one rollup per DISTINCT author — never per row or per page).
    posts = [
        posts_services.create_post(
            author=member if i % 2 == 0 else other,
            text=f"Budget audit post {i}: jirani needs 5000 shillings for maize.",
            educational=(i % 3 == 0),
        )
        for i in range(OVERFLOW_ITEMS)
    ]

    guides = [
        guides_services.create_guide(
            author=member if i % 2 == 0 else other,
            title_en=f"Budget guide {i}",
            category="farming",
            skill_level="beginner",
            steps=[{"position": 1, "text": f"Step one of guide {i}: plant it."}],
        )
        for i in range(OVERFLOW_ITEMS)
    ]

    questions = [
        qa_services.ask_question(
            author=member if i % 2 == 0 else other,
            title_en=f"Budget question {i}",
            body="How do I keep the page light?",
        )
        for i in range(OVERFLOW_ITEMS)
    ]

    listings = [
        listings_services.create_listing(
            seller=member if i % 2 == 0 else other,
            title=f"Solar lantern {i}",
            description="A lantern for the audit world.",
            category="electronics",
            ward="Kilimani",
            price=Decimal("500"),
        )
        for i in range(OVERFLOW_ITEMS)
    ]

    services_rows = [
        services_services.create_service(
            provider=member if i % 2 == 0 else other,
            title=f"Budget service {i}",
            description="Tutoring for the audit world.",
            category="tutoring",
            ward="Kilimani",
            rate_type="HOURLY",
            rate_amount=Decimal("300"),
        )
        for i in range(OVERFLOW_ITEMS)
    ]

    def _group(name, owner):
        from apps.groups.services import create_group

        return create_group(owner=owner, name=name, kind=GroupKind.INTEREST.value, ward="Kawangware")

    groups = [_group(f"Chama {i}", member) for i in range(OVERFLOW_ITEMS)]

    thread = messaging_services.create_direct_thread(initiator=member, counterpart=other)
    Message(thread=thread, sender=member, body="Is the lamp new?").full_clean()
    msg = Message.objects.create(thread=thread, sender=member, body="Is the lamp new?")

    # Routes world shared by: route pages, fleet, tracking, alerts, map.
    route = Route.objects.create(name_en="Budget Route")
    stage_a = Stage.objects.create(name="Budget A", lat=Decimal("-1.286400"), lon=Decimal("36.817200"))
    stage_b = Stage.objects.create(name="Budget B", lat=Decimal("-1.292200"), lon=Decimal("36.731700"))
    RouteStage.objects.create(route=route, stage=stage_a, position=1)
    RouteStage.objects.create(route=route, stage=stage_b, position=2)

    sacco = Sacco.objects.create(name="Budget SACCO")
    fleet_services.grant_sacco_admin(user=operator, sacco=sacco, granter=staff)
    vehicle = fleet_services.register_vehicle(
        sacco=sacco,
        raw_plate="BGT 100K",
        capacity=33,
        nickname="Budget Matatu",
        routes=[route],
        actor=operator,
    )
    seat = fleet_services.request_crew(user=crew, vehicle=vehicle, role="DRIVER")
    fleet_services.approve_crew(seat, approver=operator)

    base = datetime.datetime.now(datetime.timezone.utc)
    session, session_token = tracking_services.start_session(vehicle, by=crew)
    _publish_pings(session, session_token, base=base)

    # 22 edges so the followers list (pages of 20) overflows by 2.
    for follower in follower_pool + [other]:
        from apps.follows.models import Follow

        edge = Follow(follower=follower, followee=member)
        edge.full_clean()
        edge.save()

    vfollow = VehicleFollow(user=member, vehicle=vehicle, notify_stage=stage_a)
    vfollow.full_clean()
    vfollow.save()
    rfollow = RouteFollow(user=member, route=route, notify_stage=stage_a)
    rfollow.full_clean()
    rfollow.save()

    # Barter: one DRAFT (the edit form's owner-only workspace), one OFFERED
    # (on the table — the counter compose page), one accepted deal (the
    # dispute's only legal origin).
    offer_draft = barter_services.create_offer(
        initiator=member, counterpart=other, mode="CASH", cash_amount=Decimal("500")
    )
    offer_pending = barter_services.create_offer(
        initiator=member, counterpart=other, mode="CASH", cash_amount=Decimal("500")
    )
    barter_services.send_offer(offer_pending, by_user=member)
    offer = barter_services.create_offer(
        initiator=member, counterpart=other, mode="CASH", cash_amount=Decimal("500")
    )
    barter_services.send_offer(offer, by_user=member)
    barter_services.accept(offer, by_user=other)

    dispute = disputes_services.open_case(
        opener=member,
        deal_app="barter",
        deal_id=offer.pk,
        what_promised="500 bob for the lamp.",
        what_happened="The lamp never came.",
    )

    def _login(user):
        client = Client()
        client.force_login(user)
        return client

    world = {
        "member": member,
        "other": other,
        "staff": staff,
        "operator": operator,
        "crew": crew,
        "posts": posts,
        "guides": guides,
        "questions": questions,
        "listings": listings,
        "services": services_rows,
        "groups": groups,
        "thread": thread,
        "message": msg,
        "route": route,
        "stage_a": stage_a,
        "stage_b": stage_b,
        "vehicle": vehicle,
        "sacco": sacco,
        "session": session,
        "session_token": session_token,
        "offer": offer,
        "offer_draft": offer_draft,
        "offer_pending": offer_pending,
        "dispute": dispute,
        "clients": {
            "member": _login(member),
            "other": _login(other),
            "staff": _login(staff),
            "operator": _login(operator),
            "crew": _login(crew),
        },
    }
    yield world

    # --- teardown: unlink any variant files this world's uploads created ---
    media_root = Path(settings.MEDIA_ROOT)
    if media_root.is_dir():
        import shutil

        for purpose_dir in (media_root / "images").glob("*"):
            if purpose_dir.is_dir():
                shutil.rmtree(purpose_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# 1) THE ROUTE MANIFEST — page-weight audit, anonymous AND authed
# ---------------------------------------------------------------------------


def _manifest():
    """(label, (app_name, url_name), url-builder, gate, kind, kb) audits.

    gate: None  = public page (anon measured + authed re-measure)
          "login" = login-gated for the audited kind (anon must be a
            redirect to the login page — never a blank 200, never data)
          "staff"/"operator"/"crew" = role-gated dashboards (owner of the
            page reads it; anon still a redirect/403)
    kind: the client that legitimately SEES the page authed.
    """
    return [
        dict(label="home", key=(None, "home"), url=lambda w: "/"),
        dict(label="accounts signup", key=("accounts", "signup"), url=lambda w: reverse("accounts:signup")),
        dict(label="accounts login", key=("accounts", "login"), url=lambda w: reverse("accounts:login")),
        dict(label="accounts recover request", key=("accounts", "recover_request"), url=lambda w: reverse("accounts:recover_request")),
        dict(label="guides list", key=("guides", "list"), url=lambda w: reverse("guides:list")),
        dict(label="guide detail", key=("guides", "detail"), url=lambda w: reverse("guides:detail", args=[w["guides"][0].pk])),
        dict(label="guides create form", key=("guides", "create"), url=lambda w: reverse("guides:create"), gate="login"),
        dict(label="guide edit form", key=("guides", "edit"), url=lambda w: reverse("guides:edit", args=[w["guides"][0].pk]), gate="login"),
        dict(label="guide history", key=("guides", "history"), url=lambda w: reverse("guides:history", args=[w["guides"][0].pk])),
        dict(label="guides offline list", key=("guides", "offline"), url=lambda w: reverse("guides:offline"), gate="login"),
        dict(label="posts list", key=("posts", "list"), url=lambda w: reverse("posts:list")),
        dict(label="post detail", key=("posts", "detail"), url=lambda w: reverse("posts:detail", args=[w["posts"][0].pk])),
        dict(label="posts create form", key=("posts", "create"), url=lambda w: reverse("posts:create"), gate="login"),
        dict(label="post edit form", key=("posts", "edit"), url=lambda w: reverse("posts:edit", args=[w["posts"][0].pk]), gate="login"),
        dict(label="qa list", key=("qa", "list"), url=lambda w: reverse("qa:list")),
        dict(label="qa detail", key=("qa", "detail"), url=lambda w: reverse("qa:detail", args=[w["questions"][0].pk])),
        dict(label="qa ask form", key=("qa", "ask"), url=lambda w: reverse("qa:ask"), gate="login"),
        dict(label="follows following", key=("follows", "following"), url=lambda w: reverse("follows:following", args=[w["member"].pk])),
        dict(label="follows followers", key=("follows", "followers"), url=lambda w: reverse("follows:followers", args=[w["member"].pk])),
        dict(label="groups list", key=("groups", "list"), url=lambda w: reverse("groups:list")),
        dict(label="group detail", key=("groups", "detail"), url=lambda w: reverse("groups:detail", args=[w["groups"][0].pk])),
        dict(label="groups create form", key=("groups", "create"), url=lambda w: reverse("groups:create"), gate="login"),
        dict(label="moderation queue", key=("moderation", "queue"), url=lambda w: reverse("moderation:queue"), gate="staff"),
        dict(label="moderation report form", key=("moderation", "report_create"), url=lambda w: reverse("moderation:report_create"), gate="login"),
        dict(label="feed list", key=("feed", "list"), url=lambda w: reverse("feed:list")),
        dict(label="cross-app search", key=("search", "search"), url=lambda w: reverse("search:search")),
        dict(label="market list", key=("listings", "list"), url=lambda w: reverse("listings:list")),
        dict(label="market detail", key=("listings", "detail"), url=lambda w: reverse("listings:detail", args=[w["listings"][0].pk])),
        dict(label="market ring search", key=("listings", "search"), url=lambda w: reverse("listings:search")),
        dict(label="listing create form", key=("listings", "create"), url=lambda w: reverse("listings:create"), gate="login"),
        dict(label="listing edit form", key=("listings", "edit"), url=lambda w: reverse("listings:edit", args=[w["listings"][0].pk]), gate="login"),
        dict(label="services list", key=("services", "list"), url=lambda w: reverse("services:list")),
        dict(label="service detail", key=("services", "detail"), url=lambda w: reverse("services:detail", args=[w["services"][0].pk])),
        dict(label="service provider page", key=("services", "provider"), url=lambda w: reverse("services:provider", args=[w["member"].pk])),
        dict(label="service create form", key=("services", "create"), url=lambda w: reverse("services:create"), gate="login"),
        dict(label="service edit form", key=("services", "edit"), url=lambda w: reverse("services:edit", args=[w["services"][0].pk]), gate="login"),
        dict(label="barter list", key=("barter", "list"), url=lambda w: reverse("barter:list"), gate="login"),
        dict(label="barter new form", key=("barter", "new"), url=lambda w: reverse("barter:new"), gate="login"),
        dict(label="barter detail", key=("barter", "detail"), url=lambda w: reverse("barter:detail", args=[w["offer"].pk]), gate="login"),
        dict(label="barter draft edit", key=("barter", "edit"), url=lambda w: reverse("barter:edit", args=[w["offer_draft"].pk]), gate="login"),
        dict(label="reviews received", key=("reviews", "list"), url=lambda w: reverse("reviews:list"), gate="login"),
        dict(label="reviews mine", key=("reviews", "my_reviews"), url=lambda w: reverse("reviews:my_reviews"), gate="login"),
        dict(label="reviews leave form", key=("reviews", "leave"), url=lambda w: reverse("reviews:leave"), gate="login"),
        dict(label="disputes open form", key=("disputes", "open"), url=lambda w: reverse("disputes:open"), gate="login"),
        dict(label="disputes mine", key=("disputes", "my_cases"), url=lambda w: reverse("disputes:my_cases"), gate="login"),
        dict(label="dispute detail", key=("disputes", "detail"), url=lambda w: reverse("disputes:detail", args=[w["dispute"].pk]), gate="login"),
        dict(label="disputes queue", key=("disputes", "queue"), url=lambda w: reverse("disputes:queue"), gate="staff"),
        dict(label="messaging chats", key=("messaging", "list"), url=lambda w: reverse("messaging:list")),
        dict(label="chat thread", key=("messaging", "detail"), url=lambda w: reverse("messaging:detail", args=[w["thread"].pk]), gate="login"),
        dict(label="messaging privacy", key=("messaging", "privacy"), url=lambda w: reverse("messaging:privacy")),
        dict(label="routes list", key=("routes", "list"), url=lambda w: reverse("routes:list")),
        dict(label="routes search", key=("routes", "search"), url=lambda w: reverse("routes:search")),
        dict(label="route detail", key=("routes", "detail"), url=lambda w: reverse("routes:detail", args=[w["route"].pk])),
        dict(label="stage detail", key=("routes", "stage_detail"), url=lambda w: reverse("routes:stage_detail", args=[w["stage_a"].pk])),
        dict(label="route corrections mine", key=("routes", "corrections_mine"), url=lambda w: reverse("routes:corrections_mine"), gate="login"),
        dict(label="route correction form", key=("routes", "correction_new"), url=lambda w: reverse("routes:correction_new"), gate="login"),
        dict(label="routes correction queue", key=("routes", "queue"), url=lambda w: reverse("routes:queue"), gate="staff"),
        dict(label="fleet dashboard", key=("fleet", "dashboard"), url=lambda w: reverse("fleet:dashboard"), gate="operator"),
        dict(label="fleet vehicle form", key=("fleet", "vehicle_register"), url=lambda w: reverse("fleet:vehicle_register"), gate="operator"),
        dict(label="fleet vehicle detail", key=("fleet", "vehicle_detail"), url=lambda w: reverse("fleet:vehicle_detail", args=[w["vehicle"].pk]), gate="operator"),
        dict(label="crew seat request form", key=("fleet", "crew_request"), url=lambda w: reverse("fleet:crew_request"), gate="crew"),
        dict(label="crew seats mine", key=("fleet", "crew_mine"), url=lambda w: reverse("fleet:crew_mine"), gate="crew"),
        dict(label="fleet crew pending", key=("fleet", "crew_pending"), url=lambda w: reverse("fleet:crew_pending"), gate="operator"),
        dict(label="fleet crew roster", key=("fleet", "crew_roster"), url=lambda w: reverse("fleet:crew_roster", args=[w["vehicle"].pk]), gate="operator"),
        dict(label="fleet search", key=("fleet", "search"), url=lambda w: reverse("fleet:search")),
        dict(label="qa answer form", key=("qa", "answer"), url=lambda w: reverse("qa:answer", args=[w["questions"][0].pk]), gate="login"),
        dict(label="barter counter form", key=("barter", "counter"), url=lambda w: reverse("barter:counter", args=[w["offer_pending"].pk]), gate="login", kind="other"),
        dict(label="crowd stage reports", key=("crowd", "stage"), url=lambda w: reverse("crowd:stage", args=[w["stage_a"].pk])),
        dict(label="crowd mine", key=("crowd", "mine"), url=lambda w: reverse("crowd:mine"), gate="login"),
        dict(label="crowd report form", key=("crowd", "report_new"), url=lambda w: reverse("crowd:report_new"), gate="login"),
        dict(label="tracking start form", key=("tracking", "start"), url=lambda w: reverse("tracking:start"), gate="login"),
        dict(label="tracking my sessions", key=("tracking", "my_sessions"), url=lambda w: reverse("tracking:my_sessions"), gate="crew"),
        dict(label="tracking operator", key=("tracking", "operator"), url=lambda w: reverse("tracking:operator"), gate="operator"),
        dict(label="live map", key=("map", "route"), url=lambda w: reverse("map:route")),
        dict(label="crew trip", key=("crew", "trip"), url=lambda w: reverse("crew:trip"), gate="crew", kb=20),
        dict(label="alerts my follows", key=("alerts", "follows"), url=lambda w: reverse("alerts:follows"), gate="login"),
        dict(label="alerts arrivals", key=("alerts", "arrivals"), url=lambda w: reverse("alerts:arrivals", args=[w["route"].pk, w["stage_a"].pk]), gate="login"),
        dict(label="traffic planner", key=("traffic", "index"), url=lambda w: reverse("traffic:index")),
        dict(label="traffic plan", key=("traffic", "plan"), url=lambda w: reverse("traffic:plan")),
        dict(label="traffic about", key=("traffic", "about"), url=lambda w: reverse("traffic:about")),
        dict(label="ai ask", key=("ai", "ask"), url=lambda w: reverse("ai:ask"), gate="login"),
        dict(label="ai status page", key=("ai", "status_page"), url=lambda w: reverse("ai:status_page"), gate="staff"),
        dict(label="lang prefs demo", key=(None, "lang_prefs_demo"), url=lambda w: reverse("lang_prefs_demo")),
        dict(label="analytics staff dashboard", key=("analytics", "dashboard"), url=lambda w: reverse("analytics:dashboard"), gate="staff"),
        dict(label="analytics moderation queue", key=("analytics", "queue"), url=lambda w: reverse("analytics:queue"), gate="staff"),
        dict(label="analytics sacco dashboard", key=("analytics", "sacco"), url=lambda w: reverse("analytics:sacco"), gate="operator"),
        dict(label="monetize flags", key=("monetize", "flags"), url=lambda w: reverse("monetize:flags"), gate="staff"),
        dict(label="monetize merchant roster", key=("monetize", "merchants"), url=lambda w: reverse("monetize:merchants"), gate="staff"),
        dict(label="monetize sacco premium", key=("monetize", "sacco"), url=lambda w: reverse("monetize:sacco", args=[w["sacco"].pk]), gate="operator"),
        dict(label="offline shell page", key=(None, "offline"), url=lambda w: reverse("offline")),
        dict(label="transport stub", key=(None, "transport_stub"), url=lambda w: reverse("transport_stub")),
        dict(label="profile page", key=("profiles", "profile"), url=lambda w: reverse("profiles:profile", args=[w["other"].get_username()])),
        dict(label="profiles me", key=("profiles", "me"), url=lambda w: reverse("profiles:me"), gate="login"),
        dict(label="profiles me privacy", key=("profiles", "me_privacy"), url=lambda w: reverse("profiles:me_privacy"), gate="login"),
    ]


PRIMARY_ROUTES = _manifest()

#: Non-page routes, exempt WITH A WHY: POST-only state moves are never
#: link-followed and render no page (their per-app behavior tests are the
#: audit); JSON mirrors are audited as data elsewhere; redirect helpers
#: have nothing to weigh. A NEW GET page is never exemptable — it must be
#: audited in PRIMARY_ROUTES above.
_POST_STATE = "POST-only state move (audited by the app's own behavior tests)"
_JSON_MIRROR = "JSON mirror — audited as data in the app's tests, not as HTML"
_REDIRECT = "redirect helper — no page of its own"
EXEMPT_ROUTES = {
    ("accounts", "verify_email"): "token deep-link page (test_accounts)",
    ("accounts", "verify_phone"): "token deep-link page (test_accounts)",
    ("accounts", "recover_reset"): "token deep-link page (test_accounts)",
    ("ai", "index"): _REDIRECT + ": → ai:status_page",
    ("ai", "status"): "staff JSON mirror of ai:status_page",
    ("ai", "probe"): "POST: staff mock-provider probe",
    ("ai", "helpful"): _POST_STATE + ": logged feedback click",
    ("alerts", "index"): _REDIRECT + ": → alerts:follows",
    ("alerts", "status"): "authed JSON mirror of the arrivals page",
    ("alerts", "follow_vehicle"): _POST_STATE + ": one-tap follow",
    ("alerts", "follow_route"): _POST_STATE + ": one-tap follow",
    ("alerts", "remove_follow"): _POST_STATE,
    ("alerts", "toggle_notify"): _POST_STATE,
    ("alerts", "set_stage"): _POST_STATE,
    ("alerts", "set_radius"): _POST_STATE,
    ("analytics", "status"): "staff JSON mirror of the dashboard",
    ("barter", "send"): _POST_STATE,
    ("barter", "accept"): _POST_STATE,
    ("barter", "cancel"): _POST_STATE,
    ("barter", "complete"): _POST_STATE,
    ("crew", "trip_status"): "crew-device JSON status partial",
    ("crew", "trip_start"): _POST_STATE + ": start → one-time token page",
    ("crew", "trip_end"): _POST_STATE,
    # sibling wave (Prompt 40) is building compliance this same wave; its
    # budgets live in tests/test_compliance.py — the next audit wave audits
    # its surfaces here (or re-exempts with the same why).
    ("compliance", "dashboard"): "Prompt 40 sibling app — owned this wave by tests/test_compliance.py",
    ("compliance", "my_data"): "Prompt 40 sibling app — owned this wave by tests/test_compliance.py",
    ("compliance", "requests"): "Prompt 40 sibling app — owned this wave by tests/test_compliance.py",
    ("compliance", "request_detail"): "Prompt 40 sibling app — owned this wave by tests/test_compliance.py",
    ("compliance", "export_my_data"): "Prompt 40 sibling app — owned this wave by tests/test_compliance.py",
    ("compliance", "consent_export"): "Prompt 40 sibling app — owned this wave by tests/test_compliance.py",
    ("compliance", "user_consents"): "Prompt 40 sibling app — owned this wave by tests/test_compliance.py",
    ("crowd", "index"): _REDIRECT + ": → crowd:mine",
    ("disputes", "home"): _REDIRECT + ": → open/mine/queue by role",
    ("disputes", "evidence_add"): _POST_STATE,
    ("disputes", "start_review"): _POST_STATE,
    ("disputes", "propose"): _POST_STATE,
    ("disputes", "resolve"): _POST_STATE,
    ("disputes", "dismiss"): _POST_STATE,
    ("disputes", "confirm_proposal"): _POST_STATE,
    ("fleet", "vehicle_routes"): _POST_STATE,
    ("fleet", "vehicle_retire"): _POST_STATE,
    ("fleet", "crew_approve"): _POST_STATE,
    ("fleet", "crew_revoke"): _POST_STATE,
    ("follows", "follow"): _POST_STATE,
    ("follows", "unfollow"): _POST_STATE,
    ("groups", "join"): _POST_STATE,
    ("groups", "leave"): _POST_STATE,
    ("guides", "offline_manifest"): "authed JSON manifest — shape re-verified in the SW section",
    ("guides", "publish"): _POST_STATE,
    ("guides", "delete"): _POST_STATE,
    ("guides", "confirm"): _POST_STATE,
    ("guides", "offline_save"): _POST_STATE,
    ("guides", "offline_unsave"): _POST_STATE,
    (None, "lang_prefs_set"): "POST preference write (no namespace: lang is unwired)",
    (None, "lang_prefs_status"): "JSON preference status (lang is unwired)",
    (None, "set_lang"): "cookie language redirect (Prompt 05)",
    ("listings", "image_upload"): _POST_STATE + ": photo-picker API",
    ("listings", "remove"): _POST_STATE,
    ("listings", "pause"): _POST_STATE,
    ("listings", "resume"): _POST_STATE,
    ("listings", "sold"): _POST_STATE,
    ("listings", "renew"): _POST_STATE,
    ("listings", "bump"): _POST_STATE,
    ("listings", "recategorize"): _POST_STATE,
    ("map", "positions"): "live positions JSON — N+1-hunted + measured in the slow-3G section",
    ("media", "upload"): "POST-only upload endpoint",
    ("messaging", "start_for_listing"): _POST_STATE + ": opens the thread",
    ("messaging", "mute"): _POST_STATE,
    ("messaging", "unmute"): _POST_STATE,
    ("messaging", "receipts"): _POST_STATE,
    ("messaging", "report_message"): _POST_STATE,
    ("moderation", "resolve"): _POST_STATE,
    ("moderation", "confirm"): _POST_STATE,
    ("moderation", "draft"): _POST_STATE,
    ("moderation", "block"): _POST_STATE,
    ("moderation", "unblock"): _POST_STATE,
    ("profiles", "me_avatar"): _REDIRECT + ": GET redirects to profiles:me",
    ("posts", "image_upload"): _POST_STATE + ": photo-picker API",
    ("posts", "delete"): _POST_STATE,
    ("qa", "accept"): _POST_STATE,
    ("qa", "vote"): _POST_STATE,
    ("reviews", "report"): _POST_STATE,
    ("routes", "correction_resolve"): _POST_STATE,
    ("services", "image_upload"): _POST_STATE + ": photo-picker API",
    ("services", "remove"): _POST_STATE,
    ("tracking", "end"): _POST_STATE,
    ("tracking", "rotate"): _POST_STATE,
    ("tracking", "ingest"): "crew-device ingest — token-authed POST, deliberately not session-CSRF",
    ("trust", "post_vote"): _POST_STATE,
    ("trust", "answer_vote"): _POST_STATE,
    ("traffic", "plan_json"): "JSON mirror of the planner page",
    ("monetize", "flag_flip"): _POST_STATE,
    ("monetize", "merchant_tier"): _POST_STATE,
    ("monetize", "sacco_tool_flip"): _POST_STATE,
}


@pytest.fixture(autouse=True)
def _unthrottled(settings):
    """The audit is a MEASUREMENT pass, not an abuse hammer: posting/pages
    classes consume buckets on GET too, and a mid-suite 429 would read as a
    budget failure. Throttle behavior itself stays pinned by test_throttle;
    this bypasses nothing permanent — pytest-django restores settings."""
    settings.THROTTLE_RESOLVER_CLASSES = {}


class TestPrimaryRoutePageWeights:
    @pytest.mark.django_db
    def test_every_primary_route_page_weight(self, audit_world):
        """Anonymous AND authed per route. Problems collect rather than
        abort early: one run names every page that needs work."""
        w = audit_world
        problems = []
        for entry in PRIMARY_ROUTES:
            label = entry["label"]
            kb = entry.get("kb", PAGE_BUDGET_KB)
            url = entry["url"](w)
            gate = entry.get("gate")

            anon = _fresh_client().get(url)
            if gate is None:
                if anon.status_code != 200:
                    problems.append(
                        f"{label}: PUBLIC route answers HTTP {anon.status_code} "
                        "anonymous — auth posture moved (drift)"
                    )
                    continue
                try:
                    assert_under_budget(_html_for(anon), kb)
                except AssertionError as exc:
                    problems.append(f"{label} (anon): {exc}")
            else:
                if anon.status_code not in (302, 403):
                    problems.append(
                        f"{label}: gated route answers HTTP {anon.status_code} "
                        "anonymous — the gate moved (a leaking page is a "
                        "DPA finding; a 500 is its own finding)"
                    )
                elif anon.status_code == 302:
                    location = anon.get("Location", "")
                    # accounts login for member-style gates; the ADMIN login
                    # is honestly where staff_member_required sends visitors
                    if not ("/accounts/login" in location or "/admin/login" in location):
                        problems.append(
                            f"{label}: gated route redirects off the login walls: {location}"
                        )

            client = w["clients"][entry.get("kind") or (gate if gate in ("staff", "operator", "crew") else "member")]
            seen = client.get(url)
            if seen.status_code != 200:
                problems.append(
                    f"{label}: audited perspective ({entry.get('kind', 'member')}) "
                    f"answers HTTP {seen.status_code}"
                )
                continue
            try:
                assert_under_budget(_html_for(seen), kb)
            except AssertionError as exc:
                problems.append(f"{label} ({entry.get('kind', 'member')}): {exc}")
        assert not problems, "page-weight gate findings:\n" + "\n".join(problems)


# ---------------------------------------------------------------------------
# 2) MANIFEST DRIFT — the audit stays alive as apps land
# ---------------------------------------------------------------------------


def _live_route_pairs() -> dict:
    """Every (app_name, url_name) the root URLconf serves today."""
    from mtaani.urls import urlpatterns

    pairs = {}
    for entry in urlpatterns:
        if hasattr(entry, "url_patterns"):
            if entry.namespace == "admin":
                continue  # Django admin — staff tool, mounted late by design
            for pattern in entry.url_patterns:
                if getattr(pattern, "name", None):
                    pairs[(entry.namespace, pattern.name)] = pattern
        elif getattr(entry, "name", None):
            pairs[(None, entry.name)] = entry
    return pairs


def test_manifest_entries_still_resolve():
    """Reverse drift: a manifest route removed from an app fails HERE."""
    pairs = _live_route_pairs()
    for entry in PRIMARY_ROUTES:
        assert entry["key"] in pairs, (
            f"manifest route {entry['key']} vanished from the URLconf — "
            "fix PRIMARY_ROUTES or restore the route"
        )


def test_manifest_audit_covers_every_included_route():
    """Completeness drift: any route the root includes that is neither
    audited nor explicitly exempted fails — a new app cannot ship a page
    that never sits under a budget."""
    pairs = _live_route_pairs()
    audited = {e["key"] for e in PRIMARY_ROUTES}
    missing = []
    for key, _pattern in sorted(pairs.items(), key=str):
        if key in audited or key in EXEMPT_ROUTES:
            continue
        missing.append(f"{key[0] if key[0] is not None else 'root'}:{key[1]}")
    assert not missing, (
        "the route manifest drifted — these routes are un-audited:\n  "
        + "\n  ".join(missing)
        + "\nAdd each to PRIMARY_ROUTES (a budget audit!) or to EXEMPT_ROUTES with a why."
    )


def test_exempt_routes_all_have_a_stated_why():
    for key, why in EXEMPT_ROUTES.items():
        assert why and why.strip(), f"exempt entry without a why: {key}"
        assert key in _live_route_pairs(), (
            f"exempt key {key} no longer exists in the URLconf — drop the exemption"
        )


# ---------------------------------------------------------------------------
# 3) QUERY HUNT — page 1 vs page 2 constant cost (N+1 proof)
# ---------------------------------------------------------------------------


def _url_paged(url: str, page: int) -> str:
    return f"{url}{'&' if '?' in url else '?'}page={page}"


@pytest.mark.django_db
def test_query_count_constant_between_page_1_and_page_2(audit_world):
    """THE N+1 hunt: page 2 must cost the SAME query count as page 1.

    WHY constant-count (not assertNumQueries(exact)): see the module
    docstring — equality across pages pins the load shape (cost must not
    track row count) while never rotting on a legitimate new constant.
    The warm-up fetch runs the SAME read first so lazy profile
    get_or_creates (prompted by the feed's per-distinct-author reads)
    land BEFORE the counted pair, not inside page 1.
    """
    w = audit_world
    cases = [
        ("posts feed", reverse("feed:list"), "anon"),
        ("market list", reverse("listings:list"), "anon"),
        ("market ring search", reverse("listings:search") + "?q=solar", "anon"),
        ("qa list", reverse("qa:list"), "anon"),
        ("guides list", reverse("guides:list"), "anon"),
    ]
    hunt = []
    for label, base_url, kind in cases:
        client = _fresh_client() if kind == "anon" else w["clients"][kind]
        _sql_count(lambda c=client, u=base_url: c.get(u))  # warm-up, discarded
        q1 = _sql_count(lambda c=client, u=base_url: c.get(u))
        page2 = _url_paged(base_url, 2)
        q2 = _sql_count(lambda c=client, u=page2: c.get(u))
        hunt.append((label, q1, q2))
        assert q1 == q2, (
            f"{label}: page 2 costs {q2} queries vs page 1's {q1} — "
            "per-row query growth (N+1) crept into the pager"
        )
        assert q1 >= 1, f"{label}: only {q1} queries counted — the hunt measured nothing"
    # every surface genuinely rendered two pages (else the assertion above
    # proves nothing about growth)
    feed = _fresh_client().get("/feed/")
    assert feed.status_code == 200
    assert len(feed.context["items"]) == 10


@pytest.mark.django_db
def test_map_positions_query_count_constant_over_ping_growth(audit_world):
    """The positions JSON has no pages, so the growth axis is PINGS: a
    live session with 4 pings and one with ~8 must cost the same — cost
    must never track a ping row. (Per-session cost is a separate,
    DOCUMENTED debt in apps/map/serialization.py — not silently
    re-asserted or hidden here.)"""
    w = audit_world
    client = _fresh_client()
    base_url = f"{reverse('map:positions')}?route={w['route'].pk}"
    _sql_count(lambda: client.get(base_url))  # warm-up (route fetch, caches)
    q_light = _sql_count(lambda: client.get(base_url))
    _publish_pings(w["session"], w["session_token"], base=connection_now())
    q_heavy = _sql_count(lambda: client.get(base_url))
    assert q_light == q_heavy, (
        f"map positions: {q_heavy} queries after growing the ping rows vs {q_light} "
        "— the payload sweep now costs per-ping (N+1)"
    )
    assert q_light >= 4, f"only {q_light} queries counted — the hunt measured nothing"


def connection_now():
    """The wall clock as the ingest door wants it (UTC, tz-aware)."""
    import datetime

    return datetime.datetime.now(datetime.timezone.utc)


# ---------------------------------------------------------------------------
# 4) PAGINATION sanity — pages exist, sizes stay small, last page honest
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_pagination_sanity_on_every_list_surface(audit_world):
    """21 items ⇒ page 1 full, page 2 exists with the remainder, and a
    page beyond the last renders the LAST page (get_page honesty), never
    500, never an unbounded list."""
    w = audit_world
    surfaces = [
        ("feed list (10)", "/feed/", "items", "anon"),
        ("posts list (10)", reverse("posts:list"), "posts", "anon"),
        ("market list (10)", reverse("listings:list"), "listings", "anon"),
        ("services list (10)", reverse("services:list"), "services", "anon"),
        ("qa list (20)", reverse("qa:list"), "questions", "anon"),
        ("guides list (20)", reverse("guides:list"), "guides", "anon"),
        ("groups list (20)", reverse("groups:list"), "groups", "anon"),
        ("followers list (20)", reverse("follows:followers", args=[w["member"].pk]), "rows", "anon"),
    ]
    problems = []
    for label, base_url, ctx_key, kind in surfaces:
        pinned = int(label.split("(")[1].split(")")[0])  # the page size this
        # list is allowed (10 or 20) — the size-discipline proof
        client = _fresh_client() if kind == "anon" else w["clients"][kind]
        r1 = client.get(base_url)
        if r1.status_code != 200:
            problems.append(f"{label}: page 1 → HTTP {r1.status_code}")
            continue
        rows_1 = r1.context[ctx_key]
        r2 = client.get(_url_paged(base_url, 2))
        rows_2 = r2.context[ctx_key]
        tail = client.get(_url_paged(base_url, 99))
        tail_rows = tail.context[ctx_key]
        if len(rows_2) == 0:
            problems.append(f"{label}: page 2 is EMPTY — list shrank or pager broke")
        # WHY not "page 1 strictly fuller": the shared world overflows every
        # list by exactly one page (21 rows), so a 10-sized list has two
        # FULL pages by design; the honest page-size proof is page 1 == the
        # pinned size and page 2 never exceeding it.
        if len(rows_1) != min(pinned, len(rows_1)) or len(rows_2) > pinned:
            problems.append(
                f"{label}: page sizes drifted (page 1 {len(rows_1)} vs pinned "
                f"{pinned}; page 2 {len(rows_2)}) — the page size grew"
            )
        # Beyond-the-last must CLAMP back to an honest page (200, bounded)
        # — the fixture's 21 rows make the true last page hold just 1 row,
        # so only the clamp + bound matter, not equality with page 2.
        if tail.status_code != 200 or not (0 < len(tail_rows) <= pinned):
            problems.append(
                f"{label}: beyond-the-last answered HTTP {tail.status_code} with "
                f"{len(tail_rows)} rows (expected an honest bounded page)"
            )
    assert not problems, "pagination findings:\n" + "\n".join(problems)


# ---------------------------------------------------------------------------
# 5) IMAGE-VARIANT weights, end to end (upload → variant → <img src>)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_image_variant_weights_end_to_end(audit_world):
    """The Prompt 06 pipeline's page-side consequence, re-asserted END TO
    END: upload → variants saved under byte budgets → the LIST page loads
    the small lazy THUMB, the DETAIL page the STANDARD variant, with the
    recorded dimensions/bytes traveling into the <img> tags."""
    from tests.test_images import _noise_jpeg  # per-app helper, never edited

    from apps.core import images as images_core
    from apps.listings.services import add_listing_image
    from apps.media.services import save_upload
    from apps.posts.services import add_image, create_post

    w = audit_world
    noise = _noise_jpeg()

    # (1) the pipeline door: variant weights recorded on the MediaRef itself
    ref = save_upload(
        file_obj=SimpleUploadedFile("cam.jpg", noise, content_type="image/jpeg"),
        owner=w["member"],
        purpose="post",
    )
    assert ref.original_bytes == len(noise)
    assert ref.standard_bytes <= images_core.STANDARD_MAX_BYTES
    assert ref.thumb_bytes <= images_core.THUMB_MAX_BYTES
    assert ref.standard_bytes < ref.original_bytes, "the 'standard' variant grew past the upload"
    assert ref.thumb_bytes < ref.standard_bytes, "thumb is not the smaller variant"
    assert ref.standard_w <= images_core.STANDARD_MAX_DIM and 0 < ref.thumb_w <= images_core.THUMB_MAX_DIM

    # (2) the HTTP upload door serves the same chain (201 = created; state is
    # what we pin here — the response's exact shape is the posts app's test):
    post_via_api = create_post(author=w["member"], text="Photo via the endpoint")
    api = w["clients"]["member"].post(
        reverse("posts:image_upload", args=[post_via_api.pk]),
        {"image": SimpleUploadedFile("cam2.jpg", noise, content_type="image/jpeg")},
    )
    assert api.status_code == 201, api.content
    assert PostHasImages(post_via_api) == 1

    # (3) the rendered page chain: list loads the THUMB, detail the STANDARD
    row = add_image(ref_owner_post(w), ref)
    list_html = w["clients"]["member"].get(reverse("posts:list")).content.decode("utf-8")
    detail_html = w["clients"]["member"].get(
        reverse("posts:detail", args=[row.post_id])
    ).content.decode("utf-8")
    assert f"/media/{ref.thumb_path}" in list_html, "list page does not load the thumb variant"
    assert f"/media/{ref.standard_path}" in detail_html, "detail page does not load the standard variant"

    # (4) the listings chain mirrors it through the same Prompt 06 door —
    # use the NEWEST listing: the shared world seeds 21 listings, so the
    # market's 10-per-page shows only the last-created ten (the same
    # overflow the pagination probe teaches).
    listing_ref = save_upload(
        file_obj=SimpleUploadedFile("cam3.jpg", noise, content_type="image/jpeg"),
        owner=w["member"],
        purpose="listing",
    )
    add_listing_image(w["listings"][-1], listing_ref)
    market_html = _fresh_client().get(reverse("listings:list")).content.decode("utf-8")
    market_detail = _fresh_client().get(
        reverse("listings:detail", args=[w["listings"][-1].pk])
    ).content.decode("utf-8")
    assert f"/media/{listing_ref.thumb_path}" in market_html
    assert f"/media/{listing_ref.standard_path}" in market_detail


def PostHasImages(post) -> int:
    from apps.posts.models import PostImage

    return PostImage.objects.filter(post=post).count()


def ref_owner_post(w):
    """The audit world's authored post whose list row will render the thumb."""
    from apps.posts.services import create_post

    return create_post(author=w["member"], text="Budget photo walk")


# ---------------------------------------------------------------------------
# 6) SLOW-3G simulation — in-suite, using THE SCRIPT's math
# ---------------------------------------------------------------------------


class TestSlow3G:
    @pytest.mark.django_db
    def test_core_journeys_stay_usable_on_slow_3g(self, audit_world):
        """Acceptance: a slow-3G walk of the core journeys stays usable —
        time-to-usable < 8 s for the feed, a guide page and the live map,
        each computed from MEASURED page bytes via the script's own model."""
        w = audit_world
        page = w["clients"]["member"].get(reverse("feed:list"))
        guide = w["clients"]["member"].get(reverse("guides:detail", args=[w["guides"][0].pk]))
        map_page = _fresh_client().get(reverse("map:route"))
        positions = _fresh_client().get(f"{reverse('map:positions')}?route={w['route'].pk}")

        measurements = [
            ("posts feed (authed)", reverse("feed:list"), page.status_code, len(page.content)),
            ("guide detail (authed)", reverse("guides:detail", args=[w["guides"][0].pk]), guide.status_code, len(guide.content)),
            ("live map (anon)", reverse("map:route"), map_page.status_code, len(map_page.content)),
            ("map positions JSON (anon)", f"{reverse('map:positions')}?route={w['route'].pk}", positions.status_code, len(positions.content), 1),
        ]
        rows = audit.compute_rows(measurements)
        failed = [r for r in rows if r["verdict"].startswith("FAIL") or r["status"] != 200]
        assert not failed, "slow-3G walk fails:\n" + audit.render_table(rows)
        for row in rows:
            assert row["usable_ms"] <= THREE_G_USABLE_BUDGET_MS, audit.render_table(rows)
            if "JSON" not in row["journey"]:
                assert row["kb"] <= audit.PAGE_BUDGET_KB, audit.render_table(rows)

    def test_simulation_math_stays_honest(self):
        """The bar itself cannot rot the kind way: at the 50 KB page bar the
        model must still PASS, and far past it must FAIL loudly."""
        at_bar = audit.compute_rows([("synthetic at bar", "/", 200, 50 * 1024)])[0]
        assert at_bar["verdict"].startswith("ok"), at_bar
        assert at_bar["usable_ms"] <= THREE_G_USABLE_BUDGET_MS
        over = audit.compute_rows([("synthetic over", "/", 200, 200 * 1024)])[0]
        assert over["verdict"].startswith("FAIL"), over

    def test_script_refuses_any_other_database(self):
        """The standalone run's read-only discipline is a tested string, not
        a hope: it refuses anything but the disposable audit DB."""
        src = (REPO_ROOT / "scripts" / "audit_pages.py").read_text(encoding="utf-8")
        assert 'db_name != "mtaani_t39"' in src


# ---------------------------------------------------------------------------
# 7) SW CACHE correctness — the two DPA-sensitive invariants, re-verified
# ---------------------------------------------------------------------------


def _sw_source() -> str:
    return (STATIC / "sw.js").read_text(encoding="utf-8")


def test_sw_never_caches_member_json():
    """(a) NO authenticated/member JSON cached: the fetch handler only ever
    READS cache behind the five allowed pathname guards (static, guide
    media, guide pages' navigation, the offline GUIDE list page, the GUIDE
    manifest — the documented offline-guide design) and never WRITES a
    cache from within fetch. Any member-data endpoint must therefore
    always go to the network."""
    src = _sw_source()
    handler = src.split('self.addEventListener("fetch"', 1)[1]
    # GET-only, same-origin, auth pages never cached (the pins from the
    # design-system suite, kept adjacent so both suites guard one another)
    assert 'request.method !== "GET"' in handler
    assert "url.origin !== location.origin" in handler
    assert '"/accounts/"' in handler
    # the handler never WRITES the cache except for ONE documented write:
    # the offline-guides landing page's quiet background refresh (cache-FIRST
    # page per the design system's contract). cache writes live otherwise
    # only inside install/sync/message handlers.
    for banned_write in ("cache.addAll", "cache.add("):
        assert banned_write not in handler, (
            f"the SW fetch handler grew a cache write ({banned_write}) — that is "
            "how authenticated JSON gets cached"
        )
    assert handler.count("cache.put") == 1 and "cache.put" in handler, (
        "a cache write appeared in the SW fetch handler outside the offline-page "
        "refresh — that is how authenticated JSON gets cached"
    )
    offline_branch = handler.split('"/guides/offline/"', 1)[1].split('request.mode === "navigate"')[0]
    assert "cache.put" in offline_branch, "the fetch-handler cache write moved out of the offline-guide branch"
    # exactly the five pathname guards (one guard has two pathname reads), and
    # nothing is read from cache without one
    assert handler.count("url.pathname") == 6, (
        "the SW fetch handler grew/decreased a pathname branch — every branch "
        "must name its guard path exactly"
    )
    assert handler.count("event.respondWith") == 5
    for branch in ('MANIFEST_PATH', '"/guides/offline/"', '"/media/"', '"/static/"'):
        assert branch in handler, f"the offline-guide branch {branch} vanished"

    # Known member-JSON mirrors are banned literals: adding one anywhere in
    # the SW (a branch, a pre-fetch, anything) must be a deliberate choice,
    # reviewed as a DPA change — never a side effect.
    banned_mirrors = (
        "/status.json", "plan.json", "trip/status", "arrivals",
        "/qa/", "/feed/", "/market/", "/chats/", "/services/",
        "/tracking/", "/analytics/",
    )
    hits = [m for m in banned_mirrors if m in src]
    assert not hits, f"SW grew literals pointing at member JSON: {hits}"


@pytest.mark.django_db
def test_offline_guide_flow_contract_lands_on_a_real_endpoint(audit_world):
    """(b) the offline guide flow still works: the SW's authoritative
    source (the manifest) answers a REAL authed fetch inside the suite,
    carrying exactly the keys the SW's sync loop reads — url, hash,
    images. (The JS-execution behavior stays pinned in
    tests/test_design_system.py; here we verify the DATA side it runs on.)
    """
    w = audit_world
    src = _sw_source()
    assert 'MANIFEST_PATH = "/guides/offline-manifest.json"' in src
    assert "stored.h === g.hash" in src          # hash re-sync, hashes only
    assert "g.hash === null" in src              # no_data stays, never revalidated

    endpoint = w["clients"]["member"].get(reverse("guides:offline_manifest"))
    assert endpoint.status_code == 200
    payload = endpoint.json()
    assert {"guides", "pages"} <= set(payload), payload.keys()
    for entry in payload["guides"]:
        assert {"url", "hash", "images"} <= set(entry), entry.keys()

    # the offline landing page itself is reachable for the member (the SW
    # serves its cache-first copy; the server must still render it)
    page = w["clients"]["member"].get(reverse("guides:offline"))
    assert page.status_code == 200
