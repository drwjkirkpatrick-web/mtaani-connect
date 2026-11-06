"""tests/test_crowd.py — Prompt 30: passenger crowd reports (own file).

Acceptance coverage: dedupe/merge (3 riders → ONE honest weighted signal,
same rider → not two riders), recency decay (pure, clock-injected, exact
values at 0/45/90/120 min), trust weighting (+ ordering), type validation
(model gate + form gate), the map-overlay seam for Prompt 28 (mocked
consumer + no-positions/no-identities payload contract), honest display
counts, view flows (anti-flood, login, my-reports) and page budgets.
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from django.core.exceptions import ValidationError
from django.urls import reverse
from django.utils import timezone

from apps.core.testing import assert_under_budget
from apps.crowd import display, serialization, services, weighting
from apps.crowd.models import CrowdReport, CrowdReporterLink, ReportOutcome, ReportType
from apps.routes.models import Route, Stage

# ------------------------------------------------------------------ fixtures


@pytest.fixture
def stage(db):
    return Stage.objects.create(name="Kasarani")


@pytest.fixture
def route(db):
    return Route.objects.create(name_en="CBD - Ruiru")


def rider(n: int):
    from django.contrib.auth import get_user_model

    User = get_user_model()
    return User.objects.create_user(
        username=f"rider{n}", email=f"rider{n}@example.com", password="***"
    )


def fixed_scorer_by_pk(scores: dict[int, float]):
    """A MOCK Prompt-16 scorer: fixed 0..1 per user pk (pure, DB-free)."""
    from django.contrib.auth import get_user_model

    User = get_user_model()

    def score(reporter):
        if reporter is None or not getattr(reporter, "is_authenticated", False):
            return 0.0
        return scores.get(reporter.pk, 0.0)

    return score


def pk_scorer(scores: dict[int, float]):
    """Mock scorer over ANY reporter-ish object (no auth contract) — used
    with the FAKE reporter objects (plain classes) in purity tests."""

    def score(reporter):
        if reporter is None:
            return 0.0
        return scores.get(reporter.pk, 0.0)

    return score


# ------------------------------------------------------- type validation


@pytest.mark.django_db
def test_model_clean_refuses_unknown_type(stage, route):
    """The ORM gate: an unknown kind NEVER reaches the table (any path)."""
    report = CrowdReport(
        reporter=rider(1), stage=stage, route=route, kind="BLACKOUT"
    )
    with pytest.raises(ValidationError) as exc:
        report.full_clean()
    assert "kind" in exc.value.error_dict


@pytest.mark.django_db
def test_model_clean_refuses_destination_hint_outside_sighting(stage):
    """The hint is ONLY a "matatu here now" field — other kinds refuse it."""
    report = CrowdReport(
        reporter=rider(2), stage=stage, kind=ReportType.REROUTE.value,
        destination_hint="Ruiru Rd",
    )
    with pytest.raises(ValidationError) as exc:
        report.full_clean()
    assert "destination_hint" in exc.value.error_dict


@pytest.mark.django_db
def test_matatu_here_is_positive_sighting_with_optional_hint(stage):
    """MATATU_HERE is still an event AT the stage — hint is plain text."""
    report, outcome = services.file_report(
        reporter=rider(3), stage=stage, kind=ReportType.MATATU_HERE.value,
        destination_hint="towards CBD",
    )
    assert outcome == ReportOutcome.CREATED
    assert report.destination_hint == "towards CBD"
    assert report.stage_id == stage.pk  # anchored to the known stage


@pytest.mark.django_db
def test_form_refuses_unknown_type(stage):
    """The form gate — inline error text, same vocabulary as the model."""
    from apps.crowd.forms import ReportForm

    form = ReportForm(
        data={"kind": "BLACKOUT", "stage": stage.pk, "note": "", "destination_hint": ""}
    )
    assert not form.is_valid()
    assert "Select a valid choice" in str(form.errors)


def test_report_type_values_are_stable_vocabulary():
    """Hard rule: values persisted, labels translatable — freeze both."""
    assert sorted(ReportType.values) == [
        "ACCIDENT", "DELAY", "FLOODING", "MATATU_HERE", "REROUTE", "STAGE_CLOSED"
    ]


# --------------------------------------------------------- merge / dedupe


@pytest.mark.django_db
def test_three_riders_become_one_weighted_row(stage):
    """ACCEPTANCE: 3 riders reporting the same delay → ONE row, count 3."""
    first, _ = services.file_report(
        reporter=rider(1), stage=stage, kind=ReportType.DELAY.value,
        note="no matatu for 40 minutes", now=timezone.now(),
    )
    second_report, second_outcome = services.file_report(
        reporter=rider(2), stage=stage, kind=ReportType.DELAY.value, now=timezone.now()
    )
    third_report, third_outcome = services.file_report(
        reporter=rider(3), stage=stage, kind=ReportType.DELAY.value, now=timezone.now()
    )
    assert CrowdReport.objects.count() == 1
    # file_report re-fetches the target under lock — refetch to see the bump.
    row = CrowdReport.objects.get(stage=stage)
    assert row.pk == first.pk
    assert row.merged_count == 3
    assert row.rider_links.count() == row.merged_count  # ledger == cache
    assert row.note == "no matatu for 40 minutes"  # surviving first note
    assert second_outcome == ReportOutcome.MERGED
    assert third_outcome == ReportOutcome.MERGED


@pytest.mark.django_db
def test_same_rider_twice_counts_once_not_two_riders(stage):
    """Dedupe fairness: A, A, B → STILL 2 riders, and the duplicate cannot
    re-freshen the signal (the window is not extended by re-filing)."""
    u1 = rider(1)  # the SAME reporter object for both of their filings
    services.file_report(reporter=u1, stage=stage,
                         kind=ReportType.FLOODING.value, now=timezone.now())
    again = timezone.now() + timedelta(seconds=90)
    dup_report, dup_outcome = services.file_report(
        reporter=u1, stage=stage, kind=ReportType.FLOODING.value, now=again
    )
    assert dup_outcome == ReportOutcome.DUPLICATE
    row = CrowdReport.objects.get(stage=stage)
    assert row.merged_count == 1
    assert row.last_bumped_at is None  # NO window extension from a repeat
    rider_b = rider(2)
    merged, outcome = services.file_report(
        reporter=rider_b, stage=stage, kind=ReportType.FLOODING.value, now=again
    )
    assert merged.merged_count == 2  # 3 filings, 2 distinct riders
    assert outcome == ReportOutcome.MERGED
    assert CrowdReporterLink.objects.filter(report=row).count() == 2


@pytest.mark.django_db
def test_merge_window_closes_after_ten_minutes(stage):
    """Within 10 min → merge; after 11 min (injected) → an honest new row."""
    services.file_report(reporter=rider(1), stage=stage,
                         kind=ReportType.ACCIDENT.value, now=timezone.now())
    base = timezone.now()
    inside = services.file_report(
        reporter=rider(2), stage=stage, kind=ReportType.ACCIDENT.value,
        now=base + timedelta(minutes=9),
    )
    assert inside[1] == ReportOutcome.MERGED
    assert CrowdReport.objects.count() == 1

    services.file_report(reporter=rider(3), stage=stage,
                         kind=ReportType.REROUTE.value, now=timezone.now())
    # 20 min: that is 11 min after the ROW's last activity (rider 2's bump
    # at +9), so the window around the last bump has closed.
    outside = services.file_report(
        reporter=rider(4), stage=stage, kind=ReportType.ACCIDENT.value,
        now=base + timedelta(minutes=20),
    )
    assert outside[1] == ReportOutcome.CREATED  # window closed
    assert CrowdReport.objects.filter(kind=ReportType.ACCIDENT.value).count() == 2


@pytest.mark.django_db
def test_open_reports_for_stage_respects_window(stage):
    """The helper used by the future resolve flow reads the same window."""
    services.file_report(reporter=rider(1), stage=stage,
                         kind=ReportType.DELAY.value, now=timezone.now())
    assert services.open_reports_for_stage(stage, now=timezone.now()).count() == 1
    later = timezone.now() + timedelta(minutes=11)
    assert services.open_reports_for_stage(stage, now=later).count() == 0


# ------------------------------------------------- decay (pure + injected)


def test_decay_values_exact_at_0_45_90_120_minutes():
    """Pure math on dict rows: exact factors via the module constants."""
    base = timezone.now()
    row = {"merged_count": 1, "last_seen_at": base, "reporter": None}
    scorer = lambda reporter: 1.0  # noqa: E731 — a pinned test scorer

    assert weighting.weight(row, trust_scorer=scorer, now=base) == 1.25
    now_45 = base + timedelta(minutes=45)
    assert weighting.weight(row, trust_scorer=scorer, now=now_45) == 1.25
    now_90 = base + timedelta(minutes=90)
    assert weighting.weight(row, trust_scorer=scorer, now=now_90) == 0.5
    now_120 = base + timedelta(minutes=120)
    assert weighting.weight(row, trust_scorer=scorer, now=now_120) == 0.0
    # Zero-trust floor keeps beginner reports visible but tempered:
    assert weighting.weight(row, now=base) == 0.5  # default zero_trust


def test_weight_is_pure_deterministic_and_source_honest():
    """Same rows + same injected now → same floats; no DB/object mutation."""
    base = timezone.now()
    rows = [
        {"merged_count": 3, "last_seen_at": base, "reporter": None, "pk": 1},
        {"merged_count": 1, "last_seen_at": base, "reporter": None, "pk": 2},
    ]
    frozen = [dict(r) for r in rows]
    out1 = weighting.weight_rows(rows, now=base)
    out2 = weighting.weight_rows(rows, now=base)
    assert [r["weight"] for r in out1] == [r["weight"] for r in out2]
    # Zero-trust floor (default scorer): count1 → 0.5, count3 → ×(1+log2 3).
    assert out1[1]["weight"] == 0.5
    # Each doubling adds one unit: 3 riders = 1 + log2(3) ≈ 2.585×.
    ratio = out1[0]["weight"] / out1[1]["weight"]
    assert ratio == pytest.approx(2.58496, abs=0.001)
    assert rows == frozen  # inputs untouched (pure)


def test_decay_ordering_freshest_merged_first_and_expiry_silence():
    """Score ordering IS freshness ordering at equal trust; expired → 0.0."""
    base = timezone.now()
    old = {"merged_count": 1, "last_seen_at": base - timedelta(minutes=60),
           "reporter": None, "pk": 1}
    fresh = {"merged_count": 1, "last_seen_at": base - timedelta(minutes=10),
             "reporter": None, "pk": 2}
    expired = {"merged_count": 1, "last_seen_at": base - timedelta(minutes=130),
               "reporter": None, "pk": 3}
    out = weighting.weight_rows([old, expired, fresh], now=base)
    assert [r["pk"] for r in out] == [2, 1, 3]  # freshest first
    assert out[2]["weight"] == 0.0  # past the 120-min end: honest silence


def test_trust_weighting_higher_trust_reporter_gets_more_weight():
    """Prompt 16 plug: a trusted reporter's signal outranks a fresh one."""
    seen = timezone.now()

    class High:  # mock reporters — pure weighting never needed REAL users
        pk = 7

    class Low:
        pk = 1

    high = {"merged_count": 1, "last_seen_at": seen, "reporter": High(), "pk": 10}
    low = {"merged_count": 1, "last_seen_at": seen, "reporter": Low(), "pk": 11}
    scorer = pk_scorer({7: 0.9, 1: 0.1})  # injected mock — DB-free by design
    out = weighting.weight_rows([low, high], trust_scorer=scorer, now=seen)
    assert out[0]["pk"] == 10  # the trusted voice sorts first
    assert out[0]["weight"] == pytest.approx(0.5 + 0.9 * 0.75)  # 1.175
    assert out[1]["weight"] == pytest.approx(0.5 + 0.1 * 0.75)  # 0.575


# --------------------------------------------------- overlay (28 contract)


@pytest.mark.django_db
def test_overlay_payload_has_no_positions_and_no_identities(stage, route):
    """KDPA/anti-fabrication: payload keys never carry coordinates, and no
    key leaks a reporter identity — riders are a COUNT."""
    services.file_report(
        reporter=rider(1), stage=stage, route=route,
        kind=ReportType.FLOODING.value, now=timezone.now(),
    )
    payload = serialization.crowd_overlay_for_stage(
        stage, now=timezone.now(), scorer=fixed_scorer_by_pk({})
    )
    assert payload, "a filed report must surface"
    banned_coordinate_keys = {
        "lat", "lon", "latitude", "longitude", "coordinate", "coordinates",
        "position", "x", "y", "point", "geom", "geo",
    }
    banned_identity_keys = {"reporter", "reporter_id", "username", "user", "name"}
    for row in payload:
        assert not banned_coordinate_keys & set(row), f"position leak: {set(row)}"
        assert not banned_identity_keys & set(row), f"identity leak: {set(row)}"
        assert row["confidence"] == "COMMUNITY_REPORT"


@pytest.mark.django_db
def test_overlay_payload_contract_keys_and_weight_order(stage, route):
    """THE documented 28 seam: exact keys, sorted weight DESC."""
    services.file_report(
        reporter=rider(1), stage=stage, route=route,
        kind=ReportType.DELAY.value, now=timezone.now(),
    )
    services.file_report(
        reporter=rider(2), stage=stage,
        kind=ReportType.MATATU_HERE.value, destination_hint="towards Ruiru",
        now=timezone.now(),
    )
    now = timezone.now()
    payload = serialization.crowd_overlay_for_stage(
        stage, now=now, scorer=fixed_scorer_by_pk({})
    )
    assert len(payload) == 2
    expected_keys = {
        "stage_id", "stage_name", "type", "type_label", "count", "age_min",
        "weight", "confidence", "first_seen_at", "last_seen_at", "note",
        "destination_hint", "route_id", "route_name", "display",
    }
    assert set(payload[0]) == expected_keys
    assert payload[0]["weight"] >= payload[1]["weight"]  # strongest signal first
    assert {row["type"] for row in payload} == {"DELAY", "MATATU_HERE"}
    sighting = next(r for r in payload if r["type"] == "MATATU_HERE")
    assert sighting["destination_hint"] == "towards Ruiru"


class MockMapOverlaySerializer:
    """A Prompt 28 STAND-IN: consumes the crowd payload as the map would.

    Mocked per the PROMPTS.md rule (28 lands later; my test owns the fake):
    it reads ONLY the documented keys and produces markers pinned to the
    stage it was given — proving the payload is consumed without ever
    needing a position from us.
    """

    def __init__(self):
        self.stage_markers: dict[int, list] = {}

    def consume(self, stage, payload):
        self.stage_markers[stage.pk] = [
            {"type": r["type"], "count": r["count"], "weight": r["weight"]}
            for r in payload
        ]
        return self.stage_markers


@pytest.mark.django_db
def test_feed_integrates_into_map_overlay_via_mocked_28(stage):
    """Seam test: an overlay serializer mock builds markers from our payload."""
    services.file_report(
        reporter=rider(1), stage=stage,
        kind=ReportType.STAGE_CLOSED.value, now=timezone.now(),
    )
    services.file_report(
        reporter=rider(2), stage=stage,
        kind=ReportType.STAGE_CLOSED.value, now=timezone.now(),
    )
    payload = serialization.crowd_overlay_for_stage(
        stage, now=timezone.now(), scorer=fixed_scorer_by_pk({})
    )
    mock_28 = MockMapOverlaySerializer()
    markers = mock_28.consume(stage, payload)
    # count 2 → multiplier 2, unknown trust floor 0.5 → weight exactly 1.0.
    assert markers[stage.pk] == [{"type": "STAGE_CLOSED", "count": 2, "weight": 1.0}]


@pytest.mark.django_db
def test_expired_reports_dropped_from_overlay(stage):
    """A 2-hour-old condition is gone at read time — no stored decay state."""
    report, _ = services.file_report(
        reporter=rider(1), stage=stage, kind=ReportType.FLOODING.value,
        now=timezone.now(),
    )
    stale = timezone.now() - timedelta(minutes=125)
    report.last_bumped_at = stale
    report.save(update_fields=["last_bumped_at", "updated_at"])
    payload = serialization.crowd_overlay_for_stage(
        stage, now=timezone.now(), scorer=fixed_scorer_by_pk({})
    )
    assert payload == []


# ----------------------------------------------------- honest display


def test_honest_display_pluralization_and_route_citation():
    """'1 rider reports…' vs '2 riders report…'; route cited when present."""
    one = display.rider_line("FLOODING", 1, stage_name="Kasarani")
    three = display.rider_line("FLOODING", 3, stage_name="Kasarani",
                               route_name="Ruiru Rd")
    assert one == "1 rider reports Flooding at Kasarani"
    assert three == "3 riders report Flooding at Kasarani on route Ruiru Rd"


@pytest.mark.django_db
def test_merged_display_shows_three_riders_not_names(stage, client):
    """The stage page shows the merged count — never the reporters."""
    for n in (1, 2, 3):
        services.file_report(
            reporter=rider(n), stage=stage, kind=ReportType.DELAY.value,
            note="nothing moving", now=timezone.now(),
        )
    response = client.get(reverse("crowd:stage", args=[stage.pk]))
    html = response.content.decode()
    assert "3 riders report Long delays at this stage at Kasarani" in html
    assert "rider1" not in html and "wanjiku" not in html  # no identity leak


# --------------------------------------------------------------- views


@pytest.mark.django_db
def test_file_report_requires_authentication(stage):
    with pytest.raises(ValidationError):
        services.file_report(
            reporter=None, stage=stage, kind=ReportType.DELAY.value
        )


@pytest.mark.django_db
def test_index_redirects_to_mine_and_mine_gates_login(client):
    response = client.get("/crowd/")
    assert response.status_code == 302
    assert response["Location"] == reverse("crowd:mine")
    gated = client.get(reverse("crowd:mine"))
    assert gated.status_code == 302  # to the login page


@pytest.mark.django_db
def test_report_new_creates_through_service(stage, client_auth):
    response = client_auth.post(
        reverse("crowd:report_new"),
        {
            "kind": ReportType.FLOODING.value, "stage": stage.pk,
            "note": "water across the bay", "destination_hint": "",
            "route": "",
        },
    )
    assert response.status_code == 302
    assert CrowdReport.objects.filter(stage=stage, kind="FLOODING").count() == 1
    row = CrowdReport.objects.get()
    assert row.note == "water across the bay"
    assert response["Location"] == reverse("crowd:stage", args=[stage.pk])


@pytest.mark.django_db
def test_report_new_view_refuses_unknown_type(stage, client_auth):
    response = client_auth.post(
        reverse("crowd:report_new"),
        {"kind": "BLACKOUT", "stage": stage.pk, "note": "", "destination_hint": ""},
    )
    assert response.status_code == 200
    assert CrowdReport.objects.count() == 0
    assert "Select a valid choice" in response.content.decode()


@pytest.mark.django_db
def test_mine_lists_only_own_reports(client_auth, stage):
    u9 = rider(9)
    services.file_report(
        reporter=u9, stage=stage, kind=ReportType.DELAY.value, now=timezone.now()
    )
    mine_response = client_auth.post(
        reverse("crowd:report_new"),
        {"kind": ReportType.REROUTE.value, "stage": stage.pk, "note": "",
         "destination_hint": "", "route": ""},
    )
    assert mine_response.status_code == 302
    response = client_auth.get(reverse("crowd:mine"))
    html = response.content.decode()
    assert "Buses rerouted" in html  # REROUTE's translatable label, rendered
    assert "Long delays" not in html  # another rider's report stays theirs


@pytest.mark.django_db
def test_anti_flood_ten_reported_signals_per_hour(client_auth, fresh_user):
    """10 counted actions (created rows) in the hour, the 11th bounces.

    Each filing here is a DISTINCT (stage, kind) signal, so each is a
    counted action (a link row) — duplicate re-filings of the same signal
    are honest no-ops by design and neither flood nor count."""
    stage_a = Stage.objects.create(name="Bunyala")
    stage_b = Stage.objects.create(name="Kawangware")
    kinds = list(ReportType.values)
    # 10 genuinely DISTINCT (stage, kind) signals: the same 5 kinds across
    # two stages — every filing creates one counted link row.
    pairs = [(k, stage_a) for k in kinds[:5]] + [(k, stage_b) for k in kinds[:5]]
    for kind, target in pairs:
        assert client_auth.post(
            reverse("crowd:report_new"),
            {"kind": kind, "stage": target.pk, "note": "",
             "destination_hint": "", "route": ""},
        ).status_code == 302
    blocked = client_auth.post(
        reverse("crowd:report_new"),
        {"kind": ReportType.DELAY.value, "stage": stage_a.pk, "note": "",
         "destination_hint": "", "route": ""},
        follow=True,  # the messages render on the bounced-to page
    )
    assert blocked.status_code == 200  # bounced to mine with an error message
    assert (
        f"hourly limit of {services.MAX_REPORTS_PER_HOUR}" in blocked.content.decode()
    )


@pytest.mark.django_db
def test_stage_page_budget(stage, client):
    stage2 = Stage.objects.create(name="Bunyala")
    for n in range(6):
        services.file_report(
            reporter=rider(n), stage=stage if n % 2 == 0 else stage2,
            kind=ReportType.MATATU_HERE.value, note="x" * 60, now=timezone.now(),
        )
    response = client.get(reverse("crowd:stage", args=[stage.pk]))
    assert response.status_code == 200
    assert_under_budget(response.content.decode(), kb=50)


@pytest.mark.django_db
def test_make_report_scorer_memoizes_and_adapts(monkeypatch, fresh_user):
    """The Prompt 16 adapter: one trust call per reporter, category GENERAL."""
    calls: list[str] = []

    def fake_scorer(user, category):
        calls.append((user.pk, category))
        return 0.42

    monkeypatch.setattr("apps.trust.services.usefulness_scorer", fake_scorer)
    scorer = services.make_report_scorer()
    assert scorer(fresh_user) == 0.42
    assert scorer(fresh_user) == 0.42
    assert scorer(None) == 0.0
    assert calls == [(fresh_user.pk, "GENERAL")]
