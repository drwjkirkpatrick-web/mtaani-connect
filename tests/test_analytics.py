"""Prompt 37 tests — privacy-safe analytics (aggregate-only).

The pitfall test here is structural: the aggregate tables' REFLECTED columns
must equal exactly {id, date, metric, value} (and the sacco table's {id,
date, sacco_id, sacco_name, value}) — a contributor who adds user_id /
session_ref / ip breaks the suite, not the DPA. Aggregate math is asserted
exact against door-seeded data; role scoping (moderator vs SACCO operator)
is pinned on the dashboards.
"""
from datetime import date, datetime, timedelta, timezone as dt_timezone
from zoneinfo import ZoneInfo

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse

from apps.analytics import aggregator
from apps.analytics.models import DailyAggregate, DailySaccoAggregate, Metric

pytestmark = pytest.mark.django_db

NBO = ZoneInfo("Africa/Nairobi")
User = get_user_model()

SACCO_A = "Matatu Owners A"
SACCO_B = "Matatu Owners B"


def at(day: date, hour: int = 12) -> datetime:
    """An aware Nairobi moment on `day` (UTC storage converts __date back
    to this local day — TIME_ZONE=Africa/Nairobi)."""
    return datetime(day.year, day.month, day.day, hour, tzinfo=NBO)


def today() -> date:
    from django.utils import timezone

    return timezone.localdate()


def make_user(username: str, *, staff: bool = False):
    return User.objects.create_user(
        username=username, email=f"{username}@example.com", password="***",
        is_staff=staff,
    )


# ------------------------------------------------------------------ helpers
def seed_operational_day(n_saccos: int = 1, hours_b: float = 8.0) -> dict:
    """Seed one operational day through the owning apps' PUBLIC doors.

    2 posts (1 linked to a guide), 1 published guide, 3 listings, 1 member
    whose last_login is today, n_saccos vehicles each with one closed
    tracking session, 2 OPEN reports. Returns the seeded objects.
    """
    from apps.fleet.services import grant_sacco_admin, register_vehicle
    from apps.guides.services import create_guide
    from apps.listings.services import create_listing
    from apps.moderation.services import report_content
    from apps.posts.services import create_post
    from apps.routes.models import Sacco
    from apps.tracking.services import end_session, start_session

    author = make_user("author")
    reporter = make_user("reporter")
    staff0 = make_user("staff0", staff=True)
    saccos = [Sacco.objects.create(name=SACCO_A), Sacco.objects.create(name=SACCO_B)]

    op_a = make_user("op_a")
    grant_sacco_admin(user=op_a, sacco=saccos[0], granter=staff0)
    veh_a = register_vehicle(
        sacco=saccos[0], raw_plate="KDA 100X", capacity=14, actor=op_a
    )
    op_b = make_user("op_b")
    grant_sacco_admin(user=op_b, sacco=saccos[1], granter=make_user("staff1", staff=True))
    veh_b = register_vehicle(
        sacco=saccos[1], raw_plate="KDB 200Y", capacity=33, actor=op_b
    )

    now = at(today(), 12)
    plain_post = create_post(author=author, text="hello mtaani")
    guide = create_guide(
        author=author, title_en="Sukuma wiki in 3 steps",
        category="cooking", skill_level="beginner",
    )
    linked_post = create_post(author=author, text="read the guide", ref_guide=guide)
    for i in range(3):
        create_listing(
            seller=author, title=f"jiko {i}", description="a clean jiko",
            category="new", ward="Kitisuru", price_negotiable=True,
        )
    User.objects.filter(username="author").update(last_login=now)

    session, _ = start_session(veh_a, by=op_a, now=now)
    end_session(session, by=op_a, now=now + timedelta(hours=2))
    if n_saccos > 1:
        session_b, _ = start_session(veh_b, by=op_b, now=now)
        end_session(session_b, by=op_b, now=now + timedelta(hours=int(hours_b)))

    report_content(reporter=reporter, target=plain_post, reason="SPAM")
    report_content(reporter=reporter, target=guide, reason="SCAM")
    return {
        "saccos": saccos, "op_a": op_a, "op_b": op_b,
        "vehicle": veh_a, "plane_post": plain_post, "linked_post": linked_post,
        "guide": guide, "author": author,
    }


# ------------------------------------------- 1. THE schema (privacy) pitfall
def test_dailyaggregate_column_contract_exact():
    """THE pitfall pinned: the table can hold NOTHING but a daily count.
    A contributor adding user_id (or session_ref, or ip) fails this test
    before a migration can ever carry personal data."""
    names = {f.name for f in DailyAggregate._meta.concrete_fields}
    assert names == {"id", "date", "metric", "value"}, names


def test_daily_sacco_column_contract_exact():
    """Same structural lock for the per-sacco table (sacco = business data)."""
    names = {f.name for f in DailySaccoAggregate._meta.concrete_fields}
    assert names == {"id", "date", "sacco_id", "sacco_name", "value"}, names


def test_aggregate_models_have_no_relation_fields_at_all():
    """Reflection: NO ForeignKey/OneToOne/M2M exists on either aggregate
    model — a per-user reference is not expressible without deleting this
    test. (sacco_id is a plain integer, never a Django FK.)"""
    for model in (DailyAggregate, DailySaccoAggregate):
        rel = [f.name for f in model._meta.get_fields() if f.is_relation]
        assert rel == [], (model, rel)


def test_no_surveillance_column_names():
    """No IP / device / agent / token / session / user-shaped column NAMES."""
    import re

    forbidden = re.compile(r"ip$|device|agent|token|session|fingerprint|user")
    for model in (DailyAggregate, DailySaccoAggregate):
        for f in model._meta.concrete_fields:
            assert not forbidden.search(f.name), (model, f.name)


# ------------------------------------------------------- 2. aggregate queries
def test_rebuild_exact_values_full_day():
    facts = seed_operational_day(n_saccos=2)
    rows = aggregator.rebuild_daily(today())
    values = {r.metric: r.value for r in rows}
    metrics = {m.value for m in Metric}
    assert set(values) == metrics
    assert values[Metric.POSTS_CREATED.value] == 2
    assert values[Metric.GUIDES_PUBLISHED.value] == 1
    assert values[Metric.LISTINGS_CREATED.value] == 3
    assert values[Metric.MODERATION_QUEUE_OPEN.value] == 2
    assert values[Metric.TRACKING_SESSIONS_STARTED.value] == 2
    assert values[Metric.DAU_ESTIMATE.value] == 1
    assert values[Metric.FEED_LOCAL_SHARE_PCT.value] == 50  # 1 linked of 2 posts
    # per-sacco: one row each, exactly one session; NO second sacco's value
    # is merged anywhere else.
    sacco_rows = list(DailySaccoAggregate.objects.filter(date=today()))
    assert {(r.sacco_name, r.value) for r in sacco_rows} == {(SACCO_A, 1), (SACCO_B, 1)}


def test_rebuild_idempotent_overwrites_not_duplicates():
    seed_operational_day()
    aggregator.rebuild_daily(today())
    assert DailyAggregate.objects.count() == len(Metric)
    aggregator.rebuild_daily(today())
    assert DailyAggregate.objects.count() == len(Metric)  # overwritten, not appended
    # A change on the day updates the SAME row.
    from apps.posts.services import create_post

    create_post(author=User.objects.get(username="author"), text="one more")
    aggregator.rebuild_daily(today())
    assert DailyAggregate.objects.count() == len(Metric)
    assert (
        DailyAggregate.objects.get(date=today(), metric=Metric.POSTS_CREATED.value).value
        == 3
    )


def test_empty_day_reports_none_not_fabricated_zero():
    """No feed rows that day → the share is None ("no data"), never 0%.
    Zeros are still honest for COUNT metrics (nothing happened = 0)."""
    from django.utils import timezone

    empty_day = today() - timedelta(days=100)
    aggregator.rebuild_daily(empty_day)
    row = DailyAggregate.objects.get(
        date=empty_day, metric=Metric.FEED_LOCAL_SHARE_PCT.value
    )
    assert row.value is None
    zero_honest = DailyAggregate.objects.get(
        date=empty_day, metric=Metric.POSTS_CREATED.value
    ).value
    assert zero_honest == 0
    # Dashboard window shows — (None), not a 0% lie.
    window_sums = aggregator.window_metrics(7, today=timezone.localdate())
    assert window_sums[Metric.FEED_LOCAL_SHARE_PCT.value] is None


def test_sacco_rows_stay_in_their_own_rows():
    facts = seed_operational_day(n_saccos=1)
    aggregator.rebuild_daily(today())
    assert DailySaccoAggregate.objects.count() == 1
    row = DailySaccoAggregate.objects.get(date=today())
    assert row.sacco_name == SACCO_A and row.value == 1
    assert row.sacco_id == facts["saccos"][0].pk


# ----------------------------------------------------------- 3. dashboards
def test_dashboard_requires_staff(client):
    r_anon = client.get(reverse("analytics:dashboard"))
    assert r_anon.status_code == 302  # login first
    client.force_login(make_user("wanjiku"))
    r_member = client.get(reverse("analytics:dashboard"))
    assert r_member.status_code == 302  # not staff → sent away
    client.force_login(make_user("staffer", staff=True))
    assert client.get(reverse("analytics:dashboard")).status_code == 200
    # the status JSON mirror is staff-gated separately below


def test_dashboard_window_selectable(client):
    from django.utils import timezone

    DailyAggregate.objects.create(
        date=today() - timedelta(days=10),
        metric=Metric.POSTS_CREATED.value, value=5,
    )
    client.force_login(make_user("staffer", staff=True))
    url = reverse("analytics:dashboard")
    r30 = client.get(url, {"days": 30})
    assert "window: 5" in r30.content.decode()
    r7 = client.get(url)  # default 7
    assert "window: 5" not in r7.content.decode()
    r999 = client.get(url, {"days": 999})  # invalid → honest default 7
    assert "window: 5" not in r999.content.decode()
    # The utility line: local share appears; empty share shows no-data
    assert "Feed local share" in r7.content.decode()


def test_queue_requires_login_then_moderator_only(client):
    url = reverse("analytics:queue")
    assert client.get(url).status_code == 302  # anonymous → login
    client.force_login(make_user("wanjiku"))  # ordinary member
    assert client.get(url).status_code == 403
    # Moderator (staff role, per moderation's PUBLIC is_moderator door):
    client.force_login(make_user("mod", staff=True))
    facts = seed_operational_day()
    from datetime import datetime as dt

    from apps.moderation.models import Report

    Report.objects.filter(reason="SCAM").update(created_at=dt.now(dt_timezone.utc) - timedelta(hours=72))
    status = aggregator.queue_status()
    assert status["open"] == 2
    assert status["under_review"] == 0
    assert status["oldest_open_age_hours"] > 70
    r = client.get(url)
    assert r.status_code == 200
    html = r.content.decode()
    assert "Open reports" in html and "<p>2</p>" in html
    assert " hours" in html  # the queue-age card rendered
    # Daily volume table shows the stored daily value (4 on an older day)
    DailyAggregate.objects.create(
        date=today() - timedelta(days=2),
        metric=Metric.MODERATION_QUEUE_OPEN.value, value=4,
    )
    html2 = client.get(url).content.decode()
    assert "4</td>" in html2


def test_status_json_mirror(client):
    seed_operational_day()
    aggregator.rebuild_daily(today())
    client.force_login(make_user("staffer", staff=True))
    payload = client.get(reverse("analytics:status"), {"days": 7}).json()
    from django.utils import timezone

    assert payload["metrics"] == aggregator.window_metrics(
        7, today=timezone.localdate()
    )  # exact mirror of the dashboard's numbers
    assert payload["metrics"][Metric.POSTS_CREATED.value] == 2
    assert payload["queue"]["open"] == 2
    assert payload["note"].startswith("Aggregate-only")
    # and a member is turned away from the staff JSON too
    client.force_login(make_user("wanjiku"))
    assert client.get(reverse("analytics:status")).status_code == 302


# ------------------------------------------- 4. SACCO operator role scoping
def test_sacco_operator_sees_only_own_sacco_hours(client):
    facts = seed_operational_day(n_saccos=2, hours_b=8)
    aggregator.rebuild_daily(today())
    client.force_login(facts["op_a"])
    r = client.get(reverse("analytics:sacco"))
    assert r.status_code == 200
    html = r.content.decode()
    assert SACCO_A in html
    assert SACCO_B not in html  # the other sacco's numbers are unreachable
    agg = aggregator.sacco_session_hours(facts["saccos"][0], 7)
    assert agg["hours"] == 2.0 and agg["closed"] == 1
    assert "closed" in html  # the card rendered at all
    by_day = agg["by_day"]
    assert [row.value for row in by_day] == [1]


def test_sacco_operator_probe_of_other_sacco_is_403(client):
    facts = seed_operational_day(n_saccos=2)
    client.force_login(facts["op_a"])
    other_pk = facts["saccos"][1].pk
    r = client.get(reverse("analytics:sacco"), {"sacco": other_pk})
    assert r.status_code == 403
    assert SACCO_B not in r.content.decode()
    # a non-operator staff member (no SACCO seat) sees no fleet page
    client.force_login(make_user("staffer", staff=True))
    assert client.get(reverse("analytics:sacco")).status_code == 403
