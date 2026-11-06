"""tests/test_fleet.py — Prompt 26: vehicle, SACCO & crew registration (own file).

Acceptance list covered, mapped to tests:
  * plate validation        → test_plate_* (accept/reject tables, normalization,
                              case-insensitive duplicate refusal)
  * capacity bounds         → test_capacity_bounds_table
  * crew approval gate      → test_crew_* (PENDING-only start; non-admin refused;
                              cross-SACCO admin refused; applicant can't
                              self-approve; activate→approve→revoke lifecycle)
  * tenant isolation        → test_visible_vehicles_*, test_dashboard_*,
                              test_cross_tenant_* (list/GET/POST all 403-scoped)
  * nickname search         → test_search_* (plate-prefix, nickname, route names,
                              retired excluded, honest empty state)
  * bootstrap admin flow    → test_grant_* (staff gate + idempotency)
  * accountability pitfall  → test_crew_accountability_* (crew keeps ordinary
                              user obligations: moderation rate limits, no
                              moderator status; authority arrives with approval)
  * i18n marks + budgets    → test_strings_are_gettext_marked, test_pages_budget
"""
from __future__ import annotations

from pathlib import Path

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.urls import reverse

from apps.core.testing import assert_under_budget
from apps.fleet import services
from apps.fleet.models import (
    CrewMember,
    CrewStatus,
    FleetAction,
    FleetAudit,
    Vehicle,
    VehicleStatus,
)
from apps.routes.crew import crew_role_of, is_approved_crew, vehicles_of_crew
from apps.routes.models import Route, Sacco

FLEET_DIR = Path(__file__).resolve().parents[1] / "apps" / "fleet"


# ----------------------------------------------------------------- fixtures
@pytest.fixture
def db_setup(db):
    """Two SACCOs (A + B), their vehicles and one route each."""
    User = get_user_model()
    staff = User.objects.create_superuser("staff_fleet", "staff_fleet@example.com", "***")
    sacco_a = Sacco.objects.create(name="Matata United")
    sacco_b = Sacco.objects.create(name="Kinoo Operators")
    admin_a = User.objects.create_user("admin_a", "admin_a@example.com", "***")
    admin_b = User.objects.create_user("admin_b", "admin_b@example.com", "***")
    crew_user = User.objects.create_user("driverjoe", "driverjoe@example.com", "***")
    plain = User.objects.create_user("plain_member", "plain_member@example.com", "***")
    services.grant_sacco_admin(user=admin_a, sacco=sacco_a, granter=staff)
    services.grant_sacco_admin(user=admin_b, sacco=sacco_b, granter=staff)
    route_a = Route.objects.create(name_en="CBD - Kinoo")
    route_b = Route.objects.create(name_en="Ambassadeur - Kasarani")
    veh_a = Vehicle.objects.create(plate="KCB 220P", nickname="Ndovu", capacity=33, sacco=sacco_a)
    veh_a.routes.add(route_a)
    veh_b = Vehicle.objects.create(plate="KBX 456M", nickname="Machozi", capacity=14, sacco=sacco_b)
    veh_b.routes.add(route_b)
    return {
        "staff": staff, "sacco_a": sacco_a, "sacco_b": sacco_b,
        "admin_a": admin_a, "admin_b": admin_b,
        "crew_user": crew_user, "plain": plain,
        "veh_a": veh_a, "veh_b": veh_b,
        "route_a": route_a, "route_b": route_b,
    }


def _client_for(client, user):
    """Login a test client as the given user (fresh session each call)."""
    client.force_login(user)
    return client


# ------------------------------------------------------------------- plates
@pytest.mark.django_db
@pytest.mark.parametrize("submitted,expected", [
    ("KCA 123K", "KCA-123-K"),          # modern matatu
    ("kca123k", "KCA-123-K"),           # lowercase + no separators, same metal
    ("KDA-123-AB", "KDA-123-AB"),       # newest format (2 trailing letters)
    ("MCX 123B", "MCX-123-B"),          # motorcycle family prefix
    ("KA 100X", "KA-100-X"),            # 2-letter legacy era
    ("KZ 8463", "KZ-8463"),             # legacy, no trailing letters at all
    ("GKA 001", "GKA-001"),             # legacy 3-letter, no trailing letters
])
def test_plate_acceptance_table(submitted, expected, db_setup):
    """Each accepted family is stored NORMALIZED (uppercase + hyphens)."""
    vehicle = Vehicle(plate=submitted, capacity=14, sacco=db_setup["sacco_a"])
    vehicle.full_clean()
    vehicle.save()
    vehicle.refresh_from_db()
    assert vehicle.plate == expected


@pytest.mark.django_db
@pytest.mark.parametrize("garbage", [
    "123 ABC",          # digits first
    "KCA 12",           # too few digits
    "KCA 12345",        # too many digits
    "KCA 123K2",        # digit inside the trailing letters
    "ABCD 123K",        # 4-letter prefix
    "KCA 12A3",         # digit inside the digit group
    "hello",            # words, not plates
    "KCA_123K",         # underscore is not a separator we accept
    "KCA-123-K-",       # trailing separator
    "",                 # no plate at all
    "   ",              # whitespace only
])
def test_plate_rejection_table(garbage, db_setup):
    """Unknown/garbage plates are refused — never 'fixed' or stored."""
    vehicle = Vehicle(plate=garbage, capacity=14, sacco=db_setup["sacco_a"])
    with pytest.raises(ValidationError):
        vehicle.full_clean()


@pytest.mark.django_db
def test_plate_unique_case_insensitive(db_setup):
    """Same metal, different casing = duplicate (normalization, then unique)."""
    with pytest.raises(ValidationError):
        Vehicle(
            plate=db_setup["veh_a"].plate.lower().replace("-", " "),
            capacity=14, sacco=db_setup["sacco_a"],
        ).full_clean()
    assert Vehicle.objects.count() == 2  # nothing was re-registered


@pytest.mark.django_db
def test_plate_save_normalizes_without_form(db_setup):
    """Even a bare ORM write cannot park an un-normalized plate."""
    vehicle = Vehicle.objects.create(plate="kdx 777a", capacity=14, sacco=db_setup["sacco_a"])
    vehicle.refresh_from_db()
    assert vehicle.plate == "KDX-777-A"
    with pytest.raises(ValidationError):
        Vehicle.objects.create(plate="not a plate!", capacity=14, sacco=db_setup["sacco_a"])


# ---------------------------------------------------------------- capacity
@pytest.mark.django_db
@pytest.mark.parametrize("seats", [5, 14, 33, 51, 80])
def test_capacity_bounds_valid(seats, db_setup):
    Vehicle(plate="KDZ 900Z", capacity=seats, sacco=db_setup["sacco_a"]).full_clean()


@pytest.mark.django_db
@pytest.mark.parametrize("seats", [0, 4, 81, 200])
def test_capacity_bounds_refused(seats, db_setup):
    with pytest.raises(ValidationError):
        Vehicle(plate="KDZ 900Z", capacity=seats, sacco=db_setup["sacco_a"]).full_clean()


# ------------------------------------------------------------ nickname/search
@pytest.mark.django_db
def test_nickname_optional_and_searchable(db_setup):
    veh = Vehicle.objects.create(plate="KDW 111W", capacity=14, sacco=db_setup["sacco_a"])  # no nickname
    assert "KDW-111-W" in veh.__str__()  # honest plate-only display
    hit = services.search_vehicles("NDOVU")          # case-insensitive nickname
    assert [v.pk for v in hit] == [db_setup["veh_a"].pk]
    assert db_setup["veh_a"] in services.search_vehicles("ndov")


@pytest.mark.django_db
def test_search_plate_prefix_and_route_names(db_setup):
    by_plate = services.search_vehicles("kcb")             # plate prefix, icase
    assert [v.pk for v in by_plate] == [db_setup["veh_a"].pk]
    by_route = services.search_vehicles("Kinoo")           # assigned route name
    assert [v.pk for v in by_route] == [db_setup["veh_a"].pk]
    by_route_sw = services.search_vehicles("kasarani")     # Kiswahili route names count too
    assert [v.pk for v in by_route_sw] == [db_setup["veh_b"].pk]


@pytest.mark.django_db
def test_search_active_only_and_honest_empty(db_setup):
    services.retire_vehicle(db_setup["veh_a"], actor=db_setup["admin_a"])
    assert services.search_vehicles("Ndovu").count() == 0          # retired is not public metal
    assert services.search_vehicles("").first() is None            # empty query → honest empty
    assert services.search_vehicles("zzzz-nomatch").count() == 0


@pytest.mark.django_db
def test_search_page(client, db_setup):
    response = client.get(reverse("fleet:search") + "?q=Ndovu")
    body = response.content.decode()
    assert response.status_code == 200
    assert "KCB-220-P" in body and "Ndovu" in body
    assert "KBX-456-M" not in body  # query scoped, not a data leak
    empty = client.get(reverse("fleet:search") + "?q=zzz")
    assert "No matatu matches" in empty.content.decode()


# ------------------------------------------------------------ crew gate (service)
@pytest.mark.django_db
def test_crew_request_lands_pending_and_audited(db_setup):
    seat = services.request_crew(
        user=db_setup["crew_user"], vehicle=db_setup["veh_a"], role="DRIVER"
    )
    assert seat.status == CrewStatus.PENDING.value
    assert seat.approved_by_id is None and seat.approved_at is None
    assert not is_approved_crew(db_setup["crew_user"])  # NO authority yet
    assert crew_role_of(db_setup["crew_user"]) is None
    assert FleetAudit.objects.filter(
        action=FleetAction.CREW_REQUESTED.value, crew=seat
    ).exists()


@pytest.mark.django_db
def test_crew_request_idempotent_while_pending(db_setup):
    first = services.request_crew(
        user=db_setup["crew_user"], vehicle=db_setup["veh_a"], role="DRIVER"
    )
    again = services.request_crew(
        user=db_setup["crew_user"], vehicle=db_setup["veh_a"], role="DRIVER"
    )
    assert again.pk == first.pk          # same seat, no duplicate rows
    assert CrewMember.objects.count() == 1


@pytest.mark.django_db
def test_crew_request_roles_and_active_vehicle(db_setup):
    with pytest.raises(ValidationError):
        services.request_crew(user=db_setup["crew_user"], vehicle=db_setup["veh_a"], role="MECHANIC")
    services.retire_vehicle(db_setup["veh_b"], actor=db_setup["admin_b"])
    with pytest.raises(ValidationError):
        services.request_crew(user=db_setup["crew_user"], vehicle=db_setup["veh_b"], role="DRIVER")


@pytest.mark.django_db
def test_crew_approval_by_non_admin_refused(db_setup):
    """approve by NON-ADMIN refused — nobody self-serves the gate."""
    seat = services.request_crew(
        user=db_setup["crew_user"], vehicle=db_setup["veh_a"], role="DRIVER"
    )
    for stranger in (db_setup["plain"], None):
        with pytest.raises(ValidationError):
            services.approve_crew(seat, approver=stranger)
    seat.refresh_from_db()
    assert seat.status == CrewStatus.PENDING.value


@pytest.mark.django_db
def test_crew_approval_cross_sacco_refused(db_setup):
    """approve by SACCO A admin on a SACCO B vehicle → refused."""
    seat = services.request_crew(
        user=db_setup["crew_user"], vehicle=db_setup["veh_b"], role="DRIVER"
    )
    with pytest.raises(ValidationError):
        services.approve_crew(seat, approver=db_setup["admin_a"])
    seat.refresh_from_db()
    assert seat.status == CrewStatus.PENDING.value


@pytest.mark.django_db
def test_crew_two_party_gate_self_approval_refused(db_setup):
    """Human gate, two-party: the applicant cannot be the approver — even if
    they are themselves an admin of the vehicle's SACCO."""
    seat = services.request_crew(
        user=db_setup["admin_a"], vehicle=db_setup["veh_a"], role="DRIVER"
    )
    with pytest.raises(ValidationError):
        services.approve_crew(seat, approver=db_setup["admin_a"])
    seat.refresh_from_db()
    assert seat.status == CrewStatus.PENDING.value
    # A different human admin of the same SACCO CAN approve it — gate holds.
    User = get_user_model()
    second_admin = User.objects.create_user("admin_a2", "admin_a2@example.com", "***")
    services.grant_sacco_admin(user=second_admin, sacco=db_setup["sacco_a"], granter=db_setup["staff"])
    services.approve_crew(seat, approver=second_admin)
    assert seat.status == CrewStatus.APPROVED.value


@pytest.mark.django_db
def test_crew_approve_success_records_human_and_moment(db_setup):
    seat = services.request_crew(
        user=db_setup["crew_user"], vehicle=db_setup["veh_a"], role="CONDUCTOR"
    )
    approved = services.approve_crew(seat, approver=db_setup["admin_a"])
    assert approved.status == CrewStatus.APPROVED.value
    assert approved.approved_by_id == db_setup["admin_a"].pk
    assert approved.approved_at is not None
    assert FleetAudit.objects.filter(action=FleetAction.CREW_APPROVED.value, crew=seat).exists()
    # Authority appears ONLY with the approval:
    assert crew_role_of(db_setup["crew_user"]) == "conductor"
    assert is_approved_crew(db_setup["crew_user"]) is True


@pytest.mark.django_db
def test_crew_revocation_same_sacco_admin_only(db_setup):
    seat = services.request_crew(
        user=db_setup["crew_user"], vehicle=db_setup["veh_a"], role="DRIVER"
    )
    services.approve_crew(seat, approver=db_setup["admin_a"])
    with pytest.raises(ValidationError):
        services.revoke_crew(seat, revoker=db_setup["admin_b"])  # other SACCO
    with pytest.raises(ValidationError):
        services.revoke_crew(seat, revoker=db_setup["crew_user"])  # not an operator act
    services.revoke_crew(seat, revoker=db_setup["admin_a"])
    seat.refresh_from_db()
    assert seat.status == CrewStatus.REVOKED.value
    assert seat.revoked_by_id == db_setup["admin_a"].pk
    # Revoked ⇒ authority is GONE, not sleeping:
    assert not is_approved_crew(db_setup["crew_user"])
    assert crew_role_of(db_setup["crew_user"]) is None
    assert not vehicles_of_crew(db_setup["crew_user"]).exists()
    assert FleetAudit.objects.filter(action=FleetAction.CREW_REVOKED.value, crew=seat).exists()


@pytest.mark.django_db
def test_crew_re_request_after_revocation_reopens_as_pending(db_setup):
    seat = services.request_crew(user=db_setup["crew_user"], vehicle=db_setup["veh_a"], role="DRIVER")
    services.approve_crew(seat, approver=db_setup["admin_a"])
    services.revoke_crew(seat, revoker=db_setup["admin_a"])
    reopened = services.request_crew(user=db_setup["crew_user"], vehicle=db_setup["veh_a"], role="DRIVER")
    assert reopened.pk == seat.pk
    assert reopened.status == CrewStatus.PENDING.value
    assert reopened.revoked_by_id is None and opened_and_clean(reopened)
    assert not is_approved_crew(db_setup["crew_user"])  # pending again ⇒ no authority


def opened_and_clean(seat: CrewMember) -> bool:
    """Revocation facts were cleared on reopen."""
    return seat.revoked_at is None


@pytest.mark.django_db
def test_crew_role_of_latest_approved_semantics(db_setup):
    """With both roles approved, the LATEST approval wins (deterministic)."""
    driver_seat = services.request_crew(user=db_setup["crew_user"], vehicle=db_setup["veh_a"], role="DRIVER")
    conductor_seat = services.request_crew(user=db_setup["crew_user"], vehicle=db_setup["veh_a"], role="CONDUCTOR")
    services.approve_crew(driver_seat, approver=db_setup["admin_a"])
    services.approve_crew(conductor_seat, approver=db_setup["admin_a"])
    assert crew_role_of(db_setup["crew_user"]) == "conductor"  # approved last


@pytest.mark.django_db
def test_vehicles_of_crew_scopes_active_and_approved(db_setup):
    seat = services.request_crew(user=db_setup["crew_user"], vehicle=db_setup["veh_a"], role="DRIVER")
    assert not vehicles_of_crew(db_setup["crew_user"]).exists()  # pending ⇒ nothing
    services.approve_crew(seat, approver=db_setup["admin_a"])
    found = list(vehicles_of_crew(db_setup["crew_user"]))
    assert [v.plate for v in found] == ["KCB-220-P"]
    services.retire_vehicle(db_setup["veh_a"], actor=db_setup["admin_a"])  # retired drops out
    assert not vehicles_of_crew(db_setup["crew_user"]).exists()


@pytest.mark.django_db
def test_crew_approval_requires_active_vehicle(db_setup):
    seat = services.request_crew(user=db_setup["crew_user"], vehicle=db_setup["veh_a"], role="DRIVER")
    services.retire_vehicle(db_setup["veh_a"], actor=db_setup["admin_a"])
    with pytest.raises(ValidationError):
        services.approve_crew(seat, approver=db_setup["admin_a"])  # parked metal gains no crew


# ------------------------------------------------------- accountability pitfall
@pytest.mark.django_db
def test_crew_accountability_authority_only(db_setup):
    """THE PITFALL, tested: crew role adds authority, never removes
    accountability — ordinary-user obligations are all still there."""
    seat = services.request_crew(user=db_setup["crew_user"], vehicle=db_setup["veh_a"], role="DRIVER")
    services.approve_crew(seat, approver=db_setup["admin_a"])

    from apps.moderation.services import is_moderator as mod_is_moderator

    assert mod_is_moderator(db_setup["crew_user"]) is False  # crew ≠ moderator

    # The SAME user can file moderation reports — duties intact, and the
    # ordinary-user rate limit applies to them too (no blind spot inherited).
    from apps.moderation.models import Report
    from apps.moderation.services import report_content

    report = report_content(
        reporter=db_setup["crew_user"], target=db_setup["route_a"],
        reason="SPAM", note="route listing looks wrong",
    )
    assert Report.objects.get(pk=report.pk).reporter_id == db_setup["crew_user"].pk


@pytest.mark.django_db
def test_crew_keeps_moderation_rate_limit(db_setup, monkeypatch):
    """Reports-by-crew rate limits identically (crew keeps ordinary duties)."""
    monkeypatch.setattr("apps.moderation.services.MAX_REPORTS_PER_HOUR", 1)
    from apps.moderation.services import (
        MAX_REPORTS_PER_HOUR as patched,
        RateLimitError,
        report_content,
    )
    assert patched == 1  # sanity: the constant the service reads is patched
    seat = services.request_crew(user=db_setup["crew_user"], vehicle=db_setup["veh_a"], role="DRIVER")
    services.approve_crew(seat, approver=db_setup["admin_a"])
    report_content(
        reporter=db_setup["crew_user"], target=db_setup["route_a"], reason="SAFETY", note="first"
    )
    with pytest.raises(RateLimitError):
        report_content(
            reporter=db_setup["crew_user"], target=db_setup["route_a"], reason="SPAM", note="second"
        )


@pytest.mark.django_db
def test_blocks_apply_to_crew_members_equally(db_setup):
    """Blocking a crew member works exactly like any member (no crew shield)."""
    from apps.moderation.services import block_user, is_blocked
    seat = services.request_crew(user=db_setup["crew_user"], vehicle=db_setup["veh_a"], role="DRIVER")
    services.approve_crew(seat, approver=db_setup["admin_a"])
    block = block_user(blocker=db_setup["plain"], blocked=db_setup["crew_user"])
    assert is_blocked(db_setup["plain"], db_setup["crew_user"]) is True
    assert block.blocked_id == db_setup["crew_user"].pk  # crew role irrelevant


# --------------------------------------------------------- tenant isolation
@pytest.mark.django_db
def test_visible_vehicles_tenant_scoped(db_setup):
    """THE query-layer wall: only MY SACCO's vehicles — and staff get NO
    bypass (staff works through /admin/, never a SACCO's operator pages)."""
    User = get_user_model()
    plat = services.visible_vehicles(db_setup["admin_a"])
    assert set(plat.values_list("plate", flat=True)) == {"KCB-220-P"}
    plbt = services.visible_vehicles(db_setup["admin_b"])
    assert set(plbt.values_list("plate", flat=True)) == {"KBX-456-M"}
    assert services.visible_vehicles(None).count() == 0
    assert services.visible_vehicles(db_setup["plain"]).count() == 0
    assert services.visible_vehicles(db_setup["staff"]).count() == 0
    assert services.saccos_of_admin(db_setup["admin_b"]).get().name == "Kinoo Operators"


@pytest.mark.django_db
def test_dashboard_lists_only_own_sacco(client, db_setup):
    """A-admin's dashboard NEVER lists B's vehicles (spec test)."""
    client.force_login(db_setup["admin_a"])
    body = client.get(reverse("fleet:dashboard")).content.decode()
    assert "KCB-220-P" in body
    assert "KBX-456-M" not in body
    # And vice versa:
    client.force_login(db_setup["admin_b"])
    bbody = client.get(reverse("fleet:dashboard")).content.decode()
    assert "KBX-456-M" in bbody and "KCB-220-P" not in bbody


@pytest.mark.django_db
def test_dashboard_requires_operator_role(client, db_setup):
    """Non-operators (plain user logged in) get 403; anon gets the login redirect."""
    client.force_login(db_setup["plain"])
    assert client.get(reverse("fleet:dashboard")).status_code == 403
    assert client.get(reverse("fleet:vehicle_register")).status_code == 403
    assert client.get(reverse("fleet:crew_pending")).status_code == 403
    client.logout()  # separate the anonymous check from the logged-in one
    anon = client.get(reverse("fleet:dashboard"))
    assert anon.status_code == 302 and "/accounts/login/" in anon.url


@pytest.mark.django_db
def test_cross_tenant_reads_and_writes_403(client, db_setup):
    """SACCO A admin cannot list/act on SACCO B: detail GET and ALL mutatting
    POSTs (retire / route assign / crew approve) answer 403 — and B's data
    is untouched afterwards."""
    seat_b = services.request_crew(user=db_setup["plain"], vehicle=db_setup["veh_b"], role="CONDUCTOR")
    client.force_login(db_setup["admin_a"])
    assert client.get(reverse("fleet:vehicle_detail", args=[db_setup["veh_b"].pk])).status_code == 403
    assert client.get(reverse("fleet:crew_roster", args=[db_setup["veh_b"].pk])).status_code == 403
    assert client.post(reverse("fleet:vehicle_retire", args=[db_setup["veh_b"].pk])).status_code == 403
    assert client.post(reverse("fleet:vehicle_routes", args=[db_setup["veh_b"].pk])).status_code == 403
    assert client.post(reverse("fleet:crew_approve", args=[seat_b.pk])).status_code == 403
    assert client.post(reverse("fleet:crew_revoke", args=[seat_b.pk])).status_code == 403
    db_setup["veh_b"].refresh_from_db()
    assert db_setup["veh_b"].status == VehicleStatus.ACTIVE.value   # nothing changed
    seat_b.refresh_from_db()
    assert seat_b.status == CrewStatus.PENDING.value               # still pending


@pytest.mark.django_db
def test_forged_sacco_in_register_post_refused(client, db_setup):
    """A-admin's register form cannot forge SACCO B (queryset-validated)."""
    client.force_login(db_setup["admin_a"])
    response = client.post(reverse("fleet:vehicle_register"), {
        "sacco": str(db_setup["sacco_b"].pk),       # NOT theirs — forged choice
        "plate": "KDX 111F",
        "nickname": "Forged",
        "capacity": "33",
    })
    assert response.status_code == 200
    assert not Vehicle.objects.filter(plate="KDX-111-F").exists()
    ok = client.post(reverse("fleet:vehicle_register"), {
        "sacco": str(db_setup["sacco_a"].pk),
        "plate": "KDX 111F",
        "nickname": "Honest",
        "capacity": "33",
    }, follow=True)
    assert ok.status_code == 200
    created = Vehicle.objects.get(plate="KDX-111-F")
    assert created.sacco_id == db_setup["sacco_a"].pk
    assert created.nickname == "Honest"


@pytest.mark.django_db
def test_operator_pages_budget(client, db_setup):
    """Authenticated operator pages stay under the 50 KB HTML budget."""
    client.force_login(db_setup["admin_a"])
    for name, args in [
        ("fleet:dashboard", []), ("fleet:vehicle_register", []),
        ("fleet:vehicle_detail", [db_setup["veh_a"].pk]),
        ("fleet:crew_pending", []), ("fleet:crew_roster", [db_setup["veh_a"].pk]),
    ]:
        assert_under_budget(client.get(reverse(name, args=args)).content.decode("utf-8"), kb=50)


@pytest.mark.django_db
def test_crew_public_pages_flow(client, db_setup):
    """A member requests a seat through the UI → PENDING; admin approves through
    the UI → APPROVED (the acceptance flow, end to end)."""
    client.force_login(db_setup["crew_user"])
    response = client.post(reverse("fleet:crew_request"), {
        "vehicle": str(db_setup["veh_a"].pk), "role": "DRIVER",
    }, follow=True)
    assert response.status_code == 200
    seat = CrewMember.objects.get(user=db_setup["crew_user"], vehicle=db_setup["veh_a"])
    assert seat.status == CrewStatus.PENDING.value
    client.force_login(db_setup["admin_a"])
    queue = client.get(reverse("fleet:crew_pending"))
    assert seat.user.username in queue.content.decode()
    approve = client.post(reverse("fleet:crew_approve", args=[seat.pk]), follow=True)
    assert approve.status_code == 200
    seat.refresh_from_db()
    assert seat.status == CrewStatus.APPROVED.value
    roster = client.get(reverse("fleet:crew_roster", args=[db_setup["veh_a"].pk]))
    assert seat.user.username in roster.content.decode()


# -------------------------------------------------------------- bootstrap gate
@pytest.mark.django_db
def test_grant_first_sacco_admin_via_staff_gate(db_setup):
    """The FIRST operator seat is minted by platform staff (bootstrap)."""
    User = get_user_model()
    sacco_c = Sacco.objects.create(name="Utawala Coop")
    admin_c = User.objects.create_user("admin_c", "admin_c@example.com", "***")
    row = services.grant_sacco_admin(user=admin_c, sacco=sacco_c, granter=db_setup["staff"])
    assert row.added_by_id == db_setup["staff"].pk
    assert services.is_sacco_admin(admin_c, sacco_c) is True
    # Non-staff humans CANNOT mint operator seats at all:
    with pytest.raises(ValidationError):
        services.grant_sacco_admin(user=admin_c, sacco=Sacco.objects.create(name="X"), granter=db_setup["admin_a"])
    # Idempotent re-grant returns the same row (no audit spam):
    same = services.grant_sacco_admin(user=admin_c, sacco=sacco_c, granter=db_setup["staff"])
    assert same.pk == row.pk
    assert FleetAudit.objects.filter(action=FleetAction.SACCO_ADMIN_GRANTED.value, sacco=sacco_c).exists()


# --------------------------------------------------------------------- i18n
def test_strings_are_gettext_marked():
    """Hard rule: user-facing strings go through gettext — mechanically probed."""
    for name in ("models.py", "services.py", "views.py", "forms.py", "admin.py"):
        source = (FLEET_DIR / name).read_text(encoding="utf-8")
        assert '_("' in source, f"{name} lacks gettext markers"
    for template in FLEET_DIR.glob("templates/fleet/*.html"):
        text = template.read_text(encoding="utf-8")
        assert "{% trans" in text or "{% blocktranslate" in text, \
            f"template {template.name} lacks i18n marks"


def test_transport_facade_delegates():
    """routes.crew.py is the documented transport-side keyhole (facade only)."""
    from apps.routes import crew as routes_crew

    assert routes_crew.crew_role_of(None) is None
    assert routes_crew.is_approved_crew(None) is False
    assert not routes_crew.vehicles_of_crew(None).exists()
    module_doc = (routes_crew.__doc__ or "").lower()
    assert "authority" in module_doc and "accountability" in module_doc
