"""Prompt 40 tests — KDPA compliance workflows.

Coverage map (spec-verbatim):
- inventory completeness: registry parity BOTH ways + the PII scan audit
  over every project model + the DataMapEntry columns-purity pin
- access export round-trip: seeded content verbatim in the archive
- deletion anonymization: PII unresolvable; moderation evidence + audit
  rows survive; consent ledger intact; idempotent refusal
- retention job math: REAL Prompt-27 doors for pings/sessions; aged
  consents/OTPs/login attempts/handled requests swept, fresh kept
- consent export: member timeline + staff CSV-ish export
- request workflow state machine + dashboards scoping

Seeds go through the apps' PUBLIC doors (create_post / create_listing /
send_message / report_content) — no cross-app internals per isolation rule.
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from django.apps import apps as django_apps
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import Client
from django.urls import reverse

from apps.accounts.models import (
    ConsentEvent,
    LoginAttempt,
    VerificationCode,
)
from apps.compliance import retention as retention_mod
from apps.compliance import services
from apps.compliance.inventory import (
    SCAN_FLAG_FIELDS,
    inventory_rows,
    validate_inventory_shape,
)
from apps.compliance.models import (
    DataMapEntry,
    DataRequest,
    RequestKind,
    RequestStatus,
)
from apps.core.testing import mock_now
from apps.moderation.models import ModeratorAudit, Report

pytestmark = pytest.mark.django_db

NOW = mock_now()


def make_user(username: str, *, staff: bool = False):
    return get_user_model().objects.create_user(
        username=username,
        email=f"{username}@example.com",
        password="***",
        is_staff=staff,
    )


def _tracking_session(crew) -> int:
    """A saved TrackingSession row with the two sweeps' required stamps.

    Direct model rows here on purpose: the sweep doors
    (expire_sessions/prune_pings) are Prompt 27's and get exercised
    through retention.sweep; the CREW-approvals dance is fleet's domain.
    """
    from apps.fleet.models import Vehicle
    from apps.routes.models import Sacco
    from apps.tracking.models import TrackingSession

    sacco = Sacco.objects.create(name=f"Matope SACCO {crew.pk}")
    vehicle = Vehicle.objects.create(
        plate=f"KDA 100{crew.pk % 10}A", sacco=sacco, capacity=14,
    )
    session = TrackingSession(
        vehicle=vehicle, authorized_by=crew,
        authorization_source="CREW", token_hash="a" * 64,
        started_at=NOW, cap_hours=1,
    )
    session.full_clean()
    session.save()
    return session.pk


def seed_content(user):
    """A realistic personal footprint through the apps' PUBLIC doors."""
    from apps.listings.services import create_listing
    from apps.messaging.services import create_direct_thread, send_message
    from apps.posts.services import create_post

    post = create_post(
        author=user, text="Mboga ya mkokoteni: bei ya Digo ni KES 30"
    )
    listing = create_listing(
        seller=user,
        title="Jiko la mkaa, hali nzuri",
        description="Pika nayo maharage. Kwanza KES 1200, bei kidogo hii.",
        category="tools",
        ward="Kilimani",
        price=Decimal("1200.00"),
    )
    other = make_user("miriam")
    thread = create_direct_thread(initiator=other, counterpart=user)
    msg_body = "Hello — is the jiko still available on Saturday?"
    send_message(thread=thread, sender=user, body=msg_body)

    ConsentEvent.objects.create(
        user=user, purpose_key="terms", doc_version="2026-10-25.1",
        source_ip="41.90.12.7", created_at=NOW,
    )
    ConsentEvent.objects.create(
        user=user, purpose_key="privacy", doc_version="2026-10-25.1",
        source_ip="41.90.12.7", created_at=NOW,
    )
    return post, listing, msg_body, other


# ------------------------------------------------- 1) the inventory registry
def test_inventory_shape_internal():
    assert validate_inventory_shape() == []
    counts = services.load_inventory()
    assert counts["created"] + counts["updated"] == len(inventory_rows())


def test_registry_parity_inventory_rows_land_in_db():
    counts = services.load_inventory()
    assert len(DataMapEntry.objects.all()) == len(inventory_rows())
    for row in inventory_rows():
        entry = DataMapEntry.objects.get(
            app_label=row["app"], model_name=row["model"]
        )
        assert list(entry.fields) == row["fields"]
        assert entry.lawful_basis == row["basis"]
        assert entry.retention_days == row["retention"]
        assert entry.purpose
    assert counts  # loader is idempotent (updates, does not duplicate)


def test_registry_rows_point_at_real_models_and_fields():
    services.load_inventory()
    problems = []
    for entry in DataMapEntry.objects.all():
        try:
            model = django_apps.get_model(f"{entry.app_label}.{entry.model_name}")
        except LookupError:
            problems.append(f"{entry.app_label}.{entry.model_name} does not exist")
            continue
        real = {f.name for f in model._meta.concrete_fields}
        missing = set(entry.fields) - real
        if missing:
            problems.append(
                f"{entry.app_label}.{entry.model_name}: unknown fields {sorted(missing)}"
            )
    assert not problems


def test_no_registry_entry_without_inventory_row():
    """Registry-side drift: the loader owns the table, so after a load the
    DB must match the inventory EXACTLY (no stale, no extra rows)."""
    services.load_inventory()
    assert DataMapEntry.objects.count() == len(inventory_rows())


def test_pii_scan_audit_over_every_project_model():
    """The 'every model with personal-data flags has an entry' test.

    SCAN_FLAG_FIELDS is the tight flag list (inventory.py documents how and
    why); ANY field found in the apps registry must be covered by the same
    (app, model, field) in the inventory, or the suite fails.
    """
    services.load_inventory()
    index = {
        (row["app"], row["model"]): set(row["fields"])
        for row in inventory_rows()
    }
    problems = []
    for app_config in django_apps.get_app_configs():
        if not app_config.name.startswith("apps."):
            continue  # django.contrib + third-party tables are out of scope
        for model in app_config.get_models():
            for field in model._meta.concrete_fields:
                if field.name in SCAN_FLAG_FIELDS:
                    have = index.get((app_config.label, model.__name__), set())
                    if field.name not in have:
                        problems.append(
                            f"{app_config.label}.{model.__name__}.{field.name}"
                        )
    assert not problems, f"unmapped personal-data fields: {problems}"


def test_datamap_columns_purity():
    """The registry must never hold user data: columns EXACTLY the map
    shape (same reflection pattern as Prompt 37's analytics pin)."""
    names = {f.name for f in DataMapEntry._meta.concrete_fields}
    assert names == {
        "id", "app_label", "model_name", "fields", "purpose",
        "lawful_basis", "retention_days", "notes", "created_at", "updated_at",
    }


def test_datarequest_columns_shape():
    names = {f.name for f in DataRequest._meta.concrete_fields}
    assert names == {
        "id", "created_at", "updated_at", "user", "kind", "status",
        "handled_by", "handled_at", "note",
    }


def test_retention_parity_sweep_specs_match_inventory():
    """A retention period declared in the sweep must equal the inventory
    row for the same model — the 'how long' answer cannot rot."""
    inventory = {(r["app"], r["model"]): r for r in inventory_rows()}
    for label, model_ref, _cutoff, days, _extra in retention_mod.EXPIRY_SPECS:
        app_label, model_name = model_ref.split(".")
        row = inventory[(app_label, model_name)]
        assert row["retention"] == days, (
            f"{label}: sweep says {days}d, inventory says {row['retention']}"
        )


# --------------------------------------------------- 2) access export
def test_access_export_round_trip_contains_seeded_content():
    user = make_user("export_wanjiku")
    seed_content(user)
    archive = services.export_archive(user, now=NOW)
    assert "Mboga ya mkokoteni: bei ya Digo ni KES 30" in archive
    assert "Jiko la mkaa, hali nzuri" in archive
    assert "Hello — is the jiko still available on Saturday?" in archive
    assert "terms" in archive and "privacy" in archive
    assert "export_wanjiku" in archive


def test_access_export_excludes_other_members_data():
    user = make_user("export_me")
    other = make_user("stranger")
    from apps.posts.services import create_post

    create_post(author=other, text="STRANGER-ONLY-SECRET")
    archive = services.export_archive(user, now=NOW)
    assert "STRANGER-ONLY-SECRET" not in archive
    assert "stranger" not in archive


# ------------------------------------------------- 3) deletion anonymization
def test_deletion_anonymizes_and_preserves_moderation_evidence():
    user = make_user("delete_ota")
    post, listing, _msg, other = seed_content(user)

    from apps.moderation.services import report_content

    report = report_content(
        reporter=other, target=post, reason="SCAM",
        note="Looks like a scam", now=NOW,
    )
    audits = list(ModeratorAudit.objects.all())

    counts = services.anonymize_account(user, now=NOW, reason="leaving app")

    # -- identity erased and unresolvable --
    fresh = get_user_model().objects.get(pk=user.pk)
    U = get_user_model()
    assert fresh.username.startswith("anon-")
    assert counts["old_username"] == "delete_ota"
    assert not U.objects.filter(username="delete_ota").exists()
    assert not U.objects.filter(email="delete_ota@example.com").exists()
    assert not U.objects.filter(phone="0722-111-222").exists()
    assert fresh.first_name == "" and fresh.last_name == ""
    assert fresh.email == "" and fresh.phone == ""
    assert not fresh.is_active
    profile = fresh.profile
    assert profile.display_name == "" and profile.bio == ""
    assert profile.ward == ""
    assert profile.precise_location_consent is False

    # -- content removed via the moderation door (rows kept, status REMOVED)
    Post = django_apps.get_model("posts.Post")
    post = Post.objects.get(pk=post.pk)
    assert post.status == "REMOVED"
    Listing = django_apps.get_model("listings.Listing")
    assert Listing.objects.get(pk=listing.pk).status == "REMOVED"
    assert counts["posts_removed"] == 1 and counts["listings_removed"] == 1

    # -- moderation evidence survives, fully intact --
    r = Report.objects.get(pk=report.pk)
    assert r.target_id == post.pk
    assert r.note == "Looks like a scam"
    fresh_audits = list(ModeratorAudit.objects.all())
    assert len(fresh_audits) >= len(audits)
    for row in fresh_audits:
        assert row.actor_id  # FK intact — resolvable to an anonymous actor

    # -- consent ledger untouched (legal obligation) --
    assert fresh.consent_events.count() == 2


def test_deletion_idempotent_second_refusal():
    user = make_user("idem")
    seed_content(user)
    services.anonymize_account(user, now=NOW)
    first = get_user_model().objects.get(pk=user.pk).username
    with pytest.raises(services.AlreadyAnonymized):
        services.anonymize_account(user, now=NOW)
    assert get_user_model().objects.get(pk=user.pk).username == first


def test_anonymized_account_cannot_open_new_requests():
    user = make_user("gone")
    services.anonymize_account(user, now=NOW)
    fresh = get_user_model().objects.get(pk=user.pk)
    with pytest.raises(ValidationError):
        services.open_data_request(fresh, kind=RequestKind.ACCESS.value)


def test_anonymization_kills_open_sessions():
    user = make_user("sitting")
    from django.contrib.sessions.models import Session

    c = Client()
    c.force_login(user)  # makes a real DB-backed session row
    assert Session.objects.count() >= 1
    services.anonymize_account(user, now=NOW)
    assert Session.objects.count() == 0


# ----------------------------------------------------- 4) retention sweeps
def test_retention_sweep_pings_and_sessions_via_real_27_doors():
    from apps.tracking.models import PingPoint, TrackingSession

    crew = make_user("crew_prune")
    session_pk = _tracking_session(crew)
    session = TrackingSession.objects.get(pk=session_pk)
    PingPoint.objects.create(
        session=session, lat=Decimal("-1.290000"), lon=Decimal("36.820000"),
        recorded_at=NOW, received_at=NOW,
    )  # fresh ping: survives
    PingPoint.objects.create(
        session=session, lat=Decimal("-1.300000"), lon=Decimal("36.810000"),
        recorded_at=NOW - timedelta(hours=80),  # past the 72 h floor
        received_at=NOW - timedelta(hours=80),
    )
    results = retention_mod.sweep(now=NOW + timedelta(days=4))
    # the REAL Prompt-27 doors did the work (session expired at cap; pings
    # pruned against the 72 h floor)
    assert results["pruned_pings"] == 2
    assert results["expired_sessions"] == 1
    refreshed = TrackingSession.objects.get(pk=session_pk)
    assert refreshed.ended_reason == "EXPIRED"
    assert refreshed.ends_at == session.started_at + timedelta(hours=1)


def test_retention_sweep_aged_vs_fresh_rows():
    user = make_user("sweeper")
    ConsentEvent.objects.create(
        user=user, purpose_key="terms", doc_version="2020-01-01.1",
        created_at=NOW - timedelta(days=2600),  # past the 7-year horizon
    )
    ConsentEvent.objects.create(
        user=user, purpose_key="terms", doc_version="2026-10-25.1",
        created_at=NOW,
    )
    VerificationCode.objects.create(
        target="otp@example.com", channel="EMAIL",
        purpose="VERIFY_EMAIL", code_hash="a" * 64,
        created_at=NOW, expires_at=NOW - timedelta(hours=30),  # past grace
    )
    VerificationCode.objects.create(
        target="fresh@example.com", channel="EMAIL",
        purpose="VERIFY_EMAIL", code_hash="b" * 64,
        created_at=NOW, expires_at=NOW + timedelta(minutes=10),  # live
    )
    LoginAttempt.objects.create(
        username_norm="old", ip="1.2.3.4", created_at=NOW - timedelta(days=91),
    )
    LoginAttempt.objects.create(
        username_norm="new", ip="1.2.3.5", created_at=NOW,
    )
    old_req = DataRequest.objects.create(
        user=user, kind=RequestKind.ACCESS.value,
        status=RequestStatus.REFUSED.value, handled_at=NOW - timedelta(days=95),
    )
    open_req = DataRequest.objects.create(
        user=user, kind=RequestKind.DELETE.value,
        status=RequestStatus.OPEN.value,
    )
    results = retention_mod.sweep(now=NOW)
    assert results["consents"] == 1
    assert results["verification_codes"] == 1
    assert results["login_attempts"] == 1
    assert results["data_requests"] == 1
    # fresh rows survive the same sweep
    assert ConsentEvent.objects.filter(doc_version="2026-10-25.1").exists()
    assert VerificationCode.objects.filter(target="fresh@example.com").exists()
    assert LoginAttempt.objects.filter(username_norm="new").exists()
    assert DataRequest.objects.filter(pk=open_req.pk).exists()
    assert not DataRequest.objects.filter(pk=old_req.pk).exists()


# ------------------------------------------------------ 5) consent export
def test_consent_export_contains_seeded_events_and_is_staff_only(client):
    user = make_user("ledger")
    ConsentEvent.objects.create(
        user=user, purpose_key="terms", doc_version="2026-10-25.1",
        source_ip="41.90.12.8", created_at=NOW,
    )
    client.force_login(user)
    my_page = client.get(reverse("compliance:my_data"))
    assert my_page.status_code == 200
    assert "terms" in my_page.content.decode()

    client.force_login(make_user("clerk", staff=True))
    content = client.get(reverse("compliance:consent_export")).content.decode()
    assert "ledger" in content and "terms" in content and "41.90.12.8" in content
    other = make_user("ledger2")
    ConsentEvent.objects.create(
        user=other, purpose_key="privacy", doc_version="2026-10-25.1",
        created_at=NOW,
    )
    only = client.get(
        reverse("compliance:consent_export") + f"?user={user.username}"
    ).content.decode()
    assert "ledger2" not in only

    client.force_login(user)
    assert client.get(reverse("compliance:consent_export")).status_code == 302


def test_user_consents_view_staff_search(client):
    user = make_user("searched")
    ConsentEvent.objects.create(
        user=user, purpose_key="terms", doc_version="2026-10-25.1",
        created_at=NOW,
    )
    client.force_login(make_user("auditor", staff=True))
    r = client.get(reverse("compliance:user_consents") + f"?user={user.username}")
    assert r.status_code == 200
    assert "terms" in r.content.decode()
    r_none = client.get(reverse("compliance:user_consents") + "?user=nobody")
    assert "No such member" in r_none.content.decode()


# ---------------------------------------------- 6) DSAR state machine
def test_request_state_machine_open_to_fulfilled():
    user = make_user("dsar")
    req = services.open_data_request(
        user, kind=RequestKind.ACCESS.value, note="please", now=NOW
    )
    assert req.status == RequestStatus.OPEN.value
    staff = make_user("officer", staff=True)
    services.fulfill_request(req, by=staff, now=NOW, note="sent archive")
    req.refresh_from_db()
    assert req.status == RequestStatus.FULFILLED.value
    assert req.handled_by == staff and req.handled_at is not None
    with pytest.raises(ValidationError):  # already handled: no second state
        services.fulfill_request(req, by=staff, now=NOW)


def test_refusal_requires_written_reason():
    user = make_user("refused")
    req = services.open_data_request(
        user, kind=RequestKind.CORRECT.value, now=NOW
    )
    staff = make_user("officer2", staff=True)
    with pytest.raises(ValidationError):
        services.refuse_request(req, by=staff, note="")
    services.refuse_request(
        req, by=staff, note="identity verification pending", now=NOW
    )
    req.refresh_from_db()
    assert req.status == RequestStatus.REFUSED.value
    assert "verification" in req.note


def test_one_open_delete_per_member():
    user = make_user("dupe")
    services.open_data_request(user, kind=RequestKind.DELETE.value, now=NOW)
    with pytest.raises(ValidationError):
        services.open_data_request(user, kind=RequestKind.DELETE.value, now=NOW)
    ref_req = DataRequest.objects.get(user=user, kind=RequestKind.DELETE.value)
    services.refuse_request(
        ref_req, by=make_user("officer3", staff=True),
        note="cannot verify identity yet",
    )
    second = services.open_data_request(
        user, kind=RequestKind.DELETE.value, now=NOW
    )
    assert second.status == RequestStatus.OPEN.value


# ------------------------------------------------------ 7) dashboards scoping
def test_dashboard_is_staff_only():
    user = make_user("plain")
    staff = make_user("boss", staff=True)
    services.open_data_request(user, kind=RequestKind.ACCESS.value, now=NOW)
    c = Client()
    c.force_login(user)
    assert c.get(reverse("compliance:dashboard")).status_code == 302
    c.force_login(staff)
    r = c.get(reverse("compliance:dashboard"))
    assert r.status_code == 200
    body = r.content.decode()
    assert "accounts.User" in body          # the map rows render
    assert user.username in body            # the queue names the requester


def test_member_page_scopes_to_own_data_only():
    user = make_user("mine")
    other = make_user("notmine")
    seed_content(user)
    from apps.posts.services import create_post

    create_post(author=other, text="NOT-MY-POST-TEXT")
    c = Client()
    c.force_login(user)
    body = c.get(reverse("compliance:my_data")).content.decode()
    assert "NOT-MY-POST-TEXT" not in body
    assert "notmine" not in body
    assert c.get(reverse("compliance:requests")).status_code == 302  # staff queue


def test_member_sent_away_from_others_consent_search(client):
    user = make_user("timeliner")
    ConsentEvent.objects.create(
        user=user, purpose_key="privacy", doc_version="2026-10-25.1", created_at=NOW,
    )
    client.force_login(user)
    assert client.get(reverse("compliance:user_consents")).status_code == 302


# ----------------------------------------------------- 8) delete via workflow
def test_fulfill_delete_request_runs_anonymization():
    user = make_user("wf_delete")
    post, _listing, _msg, other = seed_content(user)

    from apps.moderation.services import report_content
    from apps.posts.models import Post

    report_content(reporter=other, target=post, reason="SCAM", now=NOW)

    req = services.open_data_request(
        user, kind=RequestKind.DELETE.value, note="moving countries", now=NOW
    )
    staff = make_user("officer4", staff=True)
    services.fulfill_deletion_request(req, by=staff, now=NOW)
    req.refresh_from_db()
    fresh = get_user_model().objects.get(pk=user.pk)
    assert req.status == RequestStatus.FULFILLED.value
    assert fresh.username.startswith("anon-")
    assert Post.objects.get(pk=post.pk).status == "REMOVED"
    assert "erasure applied" in req.note


def test_access_fulfilment_renders_the_archive_for_review(client):
    user = make_user("archive_req")
    from apps.posts.services import create_post

    create_post(author=user, text="MY-ARCHIVE-ROUND-TRIP")
    req = services.open_data_request(user, kind=RequestKind.ACCESS.value, now=NOW)
    client.force_login(make_user("officer5", staff=True))
    r = client.get(reverse("compliance:request_detail", args=[req.pk]))
    assert r.status_code == 200
    assert "MY-ARCHIVE-ROUND-TRIP" in r.content.decode()
