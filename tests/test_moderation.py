"""Prompt 15 tests: report lifecycle, blocking visibility, role gates, audit
completeness + immutability, report rate limit, and REMOVED-content handling
(excluded from public read surfaces, preserved in the database).

Cross-app integration is against guides (public query APIs only) plus a stub
target for the generic status-flip contract.
"""
import pytest
from django.core.exceptions import ValidationError
from django.db.models.deletion import ProtectedError
from django.test import Client
from django.urls import reverse

from apps.accounts.models import User
from apps.core.testing import assert_under_budget, mock_now
from apps.core.types import ContentStatus, ReportReason
from apps.guides import services as guides_services
from apps.guides.models import Guide, GuideRevision
from apps.moderation import services
from apps.moderation.models import (
    AuditAction,
    Block,
    ModeratorAudit,
    Report,
    ReportStatus,
    SuspensionDraft,
)

# Minimal valid guide payload (mirrors test_guides.py shape).
VALID_GUIDE = {
    "title_en": "Cook ugali",
    "title_sw": "",
    "category": "cooking",
    "skill_level": "beginner",
    "safety_category": "NONE",
    "safety_warning": "",
}
VALID_STEPS = [
    {"position": 1, "text": "Boil two cups of water.", "image": None},
    {"position": 2, "text": "Stir in maize flour until firm.", "image": None},
]


@pytest.fixture
def moderator(db):
    return User.objects.create_user(
        username="modmike", email="mike@example.com", password="***", is_staff=True
    )


@pytest.fixture
def second_moderator(db):
    return User.objects.create_user(
        username="modasharose", email="asha@example.com", password="***", is_staff=True
    )


@pytest.fixture
def other_user(db):
    return User.objects.create_user(username="other", email="o@example.com", password="***")


@pytest.fixture
def guide_from_author(fresh_user):
    """A real published guide by a third member (guides is public API only)."""
    return guides_services.create_guide(
        author=fresh_user, steps=VALID_STEPS, **VALID_GUIDE
    )


@pytest.fixture
def client_mod(client, moderator):
    client.force_login(moderator)
    return client


def _report_guide_post(guide, **overrides):
    from django.contrib.contenttypes.models import ContentType

    data = {
        "content_type": ContentType.objects.get_for_model(guide).pk,
        "object_id": guide.pk,
        "reason": ReportReason.SPAM.value,
        "note": "Too many links.",
        "next": "/",
    }
    data.update(overrides)
    return data


def _make_report(client_auth, guide, **overrides):
    """Shorthand: a logged-in member POSTs a report and returns the response."""
    return client_auth.post(reverse("moderation:report_create"), _report_guide_post(guide, **overrides))


# ---------------------------------------------------------------- role gates

@pytest.mark.django_db
def test_is_moderator_role_matrix(fresh_user, moderator):
    assert not services.is_moderator(None)
    assert not services.is_moderator(type("Anon", (), {"is_authenticated": False})())
    assert not services.is_moderator(fresh_user)
    assert services.is_moderator(moderator)
    assert services.is_moderator(type("S", (), {"is_authenticated": True, "is_staff": False, "is_superuser": True})())


@pytest.mark.django_db
def test_queue_requires_login_then_moderator_role(other_user):
    url = reverse("moderation:queue")
    response = Client().get(url)  # anonymous: bounce to login
    assert response.status_code == 302

    client = Client()
    client.force_login(other_user)  # authenticated but not staff: hard 403
    assert client.get(url).status_code == 403


@pytest.mark.django_db
def test_resolve_draft_confirm_block_require_moderator_role(client_auth, other_user):
    # Every mutating route: plain members get 403, never just a redirect.
    assert client_auth.post(
        reverse("moderation:resolve", args=[1]), {"decision": "remove"}
    ).status_code == 403
    assert client_auth.post(
        reverse("moderation:draft"), {"user": other_user.pk, "reason": "spam"}
    ).status_code == 403
    assert client_auth.post(
        reverse("moderation:confirm", args=[1]), {}
    ).status_code == 403


# ------------------------------------------------------------ report surface

@pytest.mark.django_db
def test_report_create_requires_login(guide_from_author):
    response = Client().post(
        reverse("moderation:report_create"), _report_guide_post(guide_from_author)
    )
    assert response.status_code == 302  # login first, never anonymous reports


@pytest.mark.django_db
def test_report_create_get_form_and_allowed_payloads(client_auth, guide_from_author):
    response = client_auth.get(reverse("moderation:report_create"))
    assert response.status_code == 200
    assert b"Report content" in response.content
    assert_under_budget(response.content.decode(), kb=10)


@pytest.mark.django_db
def test_report_form_rejects_unknown_object_and_app_label(client_auth, other_user):
    # object_id does not resolve -> 400, nothing written (no orphan reports)
    response = client_auth.post(reverse("moderation:report_create"), _report_form_post(pk_missing=True))
    assert response.status_code == 400
    # app_label outside the allow-list (accounts) -> form rejects (model also does)
    from django.contrib.contenttypes.models import ContentType

    user_ct = ContentType.objects.get_for_model(User)
    bad = client_auth.post(
        reverse("moderation:report_create"),
        {"content_type": user_ct.pk, "object_id": other_user.pk,
         "reason": ReportReason.SPAM.value, "note": ""},
    )
    assert bad.status_code == 400
    assert not Report.objects.exists()


def _report_form_post(pk_missing=False, **overrides):
    from django.contrib.contenttypes.models import ContentType
    from apps.guides.models import Guide as _Guide

    # reuse a guide CT; object_id may be non-existent for the missing case
    data = {
        "content_type": ContentType.objects.get_for_model(_Guide).pk,
        "object_id": 0 if pk_missing else 1,
        "reason": "SPAM",
        "note": "",
    }
    data.update(overrides)
    return data


# ----------------------------------------------------- report lifecycle (E2E)

@pytest.mark.django_db
def test_reported_post_flows_queue_to_action_to_audit(client_mod, moderator, guide_from_author, fresh_user):
    # NOTE: one client per actor — the shared `client` fixture would leave the
    # session logged in as the LAST fixture's user (moderator), so the member
    # gets a fresh Client here.
    member_client = Client()
    member_client.force_login(fresh_user)
    response = _make_report(member_client, guide_from_author)
    assert response.status_code == 302  # landing back on the next page
    report = Report.objects.get()
    assert report.status == ReportStatus.OPEN.value
    assert report.get_reason_display()  # translatable label resolves

    # The queue (moderator only) lists the fresh open report.
    queue_page = client_mod.get(reverse("moderation:queue"))
    assert queue_page.status_code == 200
    html = queue_page.content.decode()
    assert "SPAM" in html or any(
        c in html for c in ("Spam", "spam")
    ), "queue must surface the report reason"
    assert under_queue(html, "Cook ugali")

    # Moderator removes the content behind the report...
    decision = client_mod.post(
        reverse("moderation:resolve", args=[report.pk]),
        {"decision": "remove", "decision_note": "Spam confirmed"},
    )
    assert decision.status_code == 302
    report.refresh_from_db()
    guide_from_author.refresh_from_db()
    assert report.status == ReportStatus.RESOLVED.value
    assert report.decided_by_id == moderator.pk
    assert guide_from_author.status == ContentStatus.REMOVED.value
    # ...but the row survives (evidence retention — never hard-deleted).
    assert Guide.objects.filter(pk=guide_from_author.pk).exists()
    # Audit completeness: report creation + removal, actors distinct, same
    # target. pk order == write order — created_at ties are possible at µs
    # resolution, so NEVER rely on timestamp ordering within one test.
    actions = list(
        ModeratorAudit.objects.order_by("pk").values_list("action", flat=True)
    )
    assert actions == [AuditAction.REPORT_CREATED.value, AuditAction.REMOVED.value]
    created_row, removed_row = list(ModeratorAudit.objects.order_by("pk"))
    assert str(created_row.target_id) == str(guide_from_author.pk)
    assert str(removed_row.target_id) == str(guide_from_author.pk)
    assert created_row.actor_id == fresh_user.pk
    assert removed_row.actor_id == moderator.pk
    assert created_row.reason == ReportReason.SPAM.value
    assert removed_row.reason == ReportReason.SPAM.value
    assert created_row.created_at is not None and removed_row.created_at is not None


def under_queue(html: str, snippet: str) -> bool:
    """NOTE: the queue only shows what was reported; here the guide title."""
    return snippet in html


@pytest.mark.django_db
def test_report_dismiss_keeps_target_untouched(client_mod, fresh_user, guide_from_author):
    report = services.report_content(
        reporter=fresh_user, target=guide_from_author,
        reason=ReportReason.HARASSMENT.value, note="", now=mock_now(),
    )
    response = client_mod.post(
        reverse("moderation:resolve", args=[report.pk]), {"decision": "dismiss"}
    )
    assert response.status_code == 302
    report.refresh_from_db()
    guide_from_author.refresh_from_db()
    assert report.status == ReportStatus.DISMISSED.value
    assert guide_from_author.status == ContentStatus.PUBLISHED.value  # unharmed
    assert AuditAction.DISMISSED.value in list(ModeratorAudit.objects.values_list("action", flat=True))


@pytest.mark.django_db
def test_report_review_then_actions_order(client_mod, fresh_user, guide_from_author):
    report = services.report_content(
        reporter=fresh_user, target=guide_from_author,
        reason=ReportReason.MISINFORMATION.value, now=mock_now(),
    )
    client_mod.post(reverse("moderation:resolve", args=[report.pk]), {"decision": "review"})
    report.refresh_from_db()
    assert report.status == ReportStatus.UNDER_REVIEW.value
    assert AuditAction.REVIEW_STARTED.value in list(
        ModeratorAudit.objects.values_list("action", flat=True)
    )
    client_mod.post(reverse("moderation:resolve", args=[report.pk]), {"decision": "remove"})
    report.refresh_from_db()
    assert report.status == ReportStatus.RESOLVED.value


@pytest.mark.django_db
def test_report_restore_removed_content(client_mod, fresh_user, guide_from_author):
    services.apply_removal(guide_from_author)
    report = services.report_content(
        reporter=fresh_user, target=guide_from_author,
        reason=ReportReason.OFF_TOPIC.value, now=mock_now(),
    )
    client_mod.post(reverse("moderation:resolve", args=[report.pk]), {"decision": "restore"})
    guide_from_author.refresh_from_db()
    assert guide_from_author.status == ContentStatus.PUBLISHED.value
    assert AuditAction.RESTORED.value in list(ModeratorAudit.objects.values_list("action", flat=True))


@pytest.mark.django_db
def test_resolve_unknown_decision_and_non_removable_target(client_mod, fresh_user, other_user):
    from django.contrib.contenttypes.models import ContentType

    from apps.profiles.models import Profile

    profile = Profile.objects.create(user=other_user, ward="")
    report = Report.objects.create(
        reporter=fresh_user, reason=ReportReason.SAFETY.value,
        target_ct=ContentType.objects.get_for_model(profile.__class__),
        target_id=profile.pk,
    )
    response = client_mod.post(reverse("moderation:resolve", args=[report.pk]), {"decision": "nuke"})
    assert response.status_code == 400  # unknown decision: message, not crash
    # removing a status-less target (Profile) is refused, message not crash
    response = client_mod.post(reverse("moderation:resolve", args=[report.pk]), {"decision": "remove"})
    assert response.status_code == 400
    assert b"cannot be removed" in response.content


@pytest.mark.django_db
def test_resolve_requires_moderator(client_auth, fresh_user, guide_from_author):
    report = services.report_content(
        reporter=fresh_user, target=guide_from_author,
        reason=ReportReason.SPAM.value, now=mock_now(),
    )
    assert client_auth.post(
        reverse("moderation:resolve", args=[report.pk]), {"decision": "remove"}
    ).status_code == 403  # role enforced end-to-end


# ----------------------------------------------------------------- rate limit

@pytest.mark.django_db
def test_report_rate_limit_ten_per_hour(client_auth, fresh_user, guide_from_author):
    # The limiter accepts injectable time; tests drive it with mock_now.
    for _ in range(services.MAX_REPORTS_PER_HOUR):
        services.report_content(
            reporter=fresh_user, target=guide_from_author,
            reason=ReportReason.SPAM.value, now=mock_now(),
        )
    assert Report.objects.count() == services.MAX_REPORTS_PER_HOUR
    with pytest.raises(ValidationError) as excinfo:
        services.report_content(
            reporter=fresh_user, target=guide_from_author,
            reason=ReportReason.SPAM.value, now=mock_now(),
        )
    assert excinfo.value.code == "report_rate_limited"
    assert Report.objects.count() == services.MAX_REPORTS_PER_HOUR  # 11th refused


@pytest.mark.django_db
def test_report_rate_limit_view_answers_429(client_auth, guide_from_author):
    for _ in range(services.MAX_REPORTS_PER_HOUR):
        response = _make_report(client_auth, guide_from_author)
        assert response.status_code == 302
    response = _make_report(client_auth, guide_from_author)
    assert response.status_code == 429  # localized message, not a crash
    assert Report.objects.count() == services.MAX_REPORTS_PER_HOUR


@pytest.mark.django_db
def test_report_rate_limit_window_is_per_user(fresh_user, other_user, guide_from_author):
    # fresh_user burns their quota; other_user is unaffected (per-user window).
    for _ in range(services.MAX_REPORTS_PER_HOUR):
        services.report_content(
            reporter=fresh_user, target=guide_from_author,
            reason=ReportReason.SPAM.value, now=mock_now(),
        )
    report = services.report_content(
        reporter=other_user, target=guide_from_author,
        reason=ReportReason.SCAM.value, now=mock_now(),
    )
    assert report.reporter_id == other_user.pk


# ------------------------------------------------------------- app-label guard

@pytest.mark.django_db
def test_service_refuses_disallowed_target_app(client_auth, fresh_user, other_user):
    with pytest.raises(ValidationError):
        services.report_content(
            reporter=fresh_user, target=other_user,  # accounts: not allowed
            reason=ReportReason.SPAM.value, now=mock_now(),
        )
    assert not Report.objects.exists()


# ------------------------------------------------------------------- blocking

@pytest.mark.django_db
def test_block_requires_login(client_auth):
    response = Client().post(reverse("moderation:block"), {"user": 1})
    assert response.status_code == 302


@pytest.mark.django_db
def test_block_self_refused(client_auth, fresh_user):
    me = fresh_user
    response = client_auth.post(reverse("moderation:block"), {"user": me.pk})
    assert response.status_code == 400
    assert not Block.objects.exists()


@pytest.mark.django_db
def test_block_unblock_cycle_with_audit(client_auth, fresh_user, other_user):
    response = client_auth.post(reverse("moderation:block"), {"user": other_user.pk})
    assert response.status_code == 302
    assert services.is_blocked(fresh_user, other_user)
    assert not services.is_blocked(other_user, fresh_user)  # asymmetric

    with pytest.raises(ValidationError):
        services.block_user(blocker=fresh_user, blocked=other_user)

    audit = list(ModeratorAudit.objects.order_by("pk").values_list("action", flat=True))
    assert audit == [AuditAction.BLOCKED.value]

    response = client_auth.post(reverse("moderation:unblock"), {"user": other_user.pk})
    assert response.status_code == 302
    assert not services.is_blocked(fresh_user, other_user)  # revocable
    audit = list(ModeratorAudit.objects.order_by("pk").values_list("action", flat=True))
    assert audit == [AuditAction.BLOCKED.value, AuditAction.UNBLOCKED.value]
    # Re-blocking works after revocation (revocable, not black hole).
    client_auth.post(reverse("moderation:block"), {"user": other_user.pk})
    assert services.is_blocked(fresh_user, other_user)

    with pytest.raises(ValidationError):
        services.unblock_user(blocker=other_user, blocked=fresh_user)


@pytest.mark.django_db
def test_excluding_blocked_for_filters_blocked_authors(fresh_user, other_user):
    authors_guide = guides_services.create_guide(
        author=other_user, steps=VALID_STEPS,
        **{**VALID_GUIDE, "title_en": "Fix a radio"},
    )
    mine = guides_services.create_guide(
        author=fresh_user, steps=VALID_STEPS, **VALID_GUIDE
    )
    fresh_user_queryset = services.excluding_blocked_for(
        fresh_user, Guide.objects.all(), author_field="created_by"
    )
    assert not services.is_blocked(fresh_user, other_user)
    services.block_user(blocker=fresh_user, blocked=other_user, now=mock_now())
    # NOTE: is_blocked is also asserted here via service-level block.
    fresh_user_queryset = services.excluding_blocked_for(
        fresh_user, Guide.objects.all(), author_field="created_by"
    )
    assert list(fresh_user_queryset) == [mine]
    # Anonymous viewers cannot have blocks: everything stays visible.
    anonymous_view = services.excluding_blocked_for(
        None, Guide.objects.all(), author_field="created_by"
    )
    assert anonymous_view.count() == 2


@pytest.mark.django_db
def test_excluding_blocked_for_dotted_author_path(fresh_user, other_user, guide_from_author):
    services.block_user(blocker=fresh_user, blocked=other_user, now=mock_now())
    others_revision = guides_services.create_guide(
        author=other_user, steps=VALID_STEPS,
        **{**VALID_GUIDE, "title_en": "Repair a kerosene lamp"},
    ).revisions.first()
    revisions = services.excluding_blocked_for(
        fresh_user, GuideRevision.objects.all(), author_field="guide__created_by"
    )
    assert not revisions.filter(pk=others_revision.pk).exists()
    assert revisions.filter(pk=guide_from_author.revisions.first().pk).exists()


# ------------------------------------------- REMOVED excluded, rows preserved

@pytest.mark.django_db
def test_removed_content_excluded_but_preserved(fresh_user, other_user, moderator, guide_from_author):
    services.block_user(blocker=fresh_user, blocked=other_user, now=mock_now())
    others = guides_services.create_guide(
        author=other_user, steps=VALID_STEPS,
        **{**VALID_GUIDE, "title_en": "Start a poultry run"},
    )
    clean = guides_services.create_guide(
        author=moderator, steps=VALID_STEPS,
        **{**VALID_GUIDE, "title_en": "Harvest honey"},
    )
    services.apply_removal(guide_from_author)  # moderator removed one guide
    surface = services.visible(
        fresh_user, Guide.objects.all(), author_field="created_by"
    )
    titles = {guide.pk for guide in surface}
    assert titles == {clean.pk}                # one safe guide stays
    # ...and the rows are still in the DB (evidence retention):
    assert Guide.objects.count() == 3
    assert Guide.objects.get(pk=guide_from_author.pk).status == ContentStatus.REMOVED.value


@pytest.mark.django_db
def test_removed_revision_excluded_from_visible(fresh_user, guide_from_author):
    published = guide_from_author.revisions.first()
    scrubbed = GuideRevision.objects.create(
        guide=guide_from_author, version=2, title_en="Bad copy",
        status=ContentStatus.REMOVED.value,
    )
    surface = services.visible(
        fresh_user, GuideRevision.objects.all(), author_field="guide__created_by"
    )
    assert not surface.filter(pk=scrubbed.pk).exists()
    assert surface.filter(pk=published.pk).exists()
    assert GuideRevision.objects.filter(pk__in=[published.pk, scrubbed.pk]).count() == 2


def _dummy(saved_with=None):
    """Stub object pattern (no DB): exposes `status` and records save kwargs."""
    class DummyTarget:
        status = ContentStatus.PUBLISHED.value

        def save(self, update_fields=None):
            self.saved_with = update_fields

    return DummyTarget()


@pytest.mark.django_db
def test_apply_removal_and_restore_stub_objects():
    dummy = _dummy()
    assert services.apply_removal(dummy) is True
    assert dummy.status == ContentStatus.REMOVED.value
    assert "status" in dummy.saved_with

    class Statusless:
        pass  # NOTE: no .status at all — the generic no-op case

    assert services.apply_removal(Statusless()) is False
    removed_dummy = _dummy()
    services.apply_removal(removed_dummy)
    assert services.restore_target(removed_dummy) is True
    assert removed_dummy.status == ContentStatus.PUBLISHED.value
    already_published = _dummy()
    assert services.restore_target(already_published) is False  # nothing to undo


@pytest.mark.django_db
def test_visible_graceful_for_models_without_status(fresh_user):
    # Block has no `status` column: `visible` skips it instead of erroring.
    qs = services.visible(fresh_user, Block.objects.all(), author_field="blocked")
    assert qs.count() == 0


# --------------------------------------------------------------------- audit

@pytest.mark.django_db
def test_audit_rows_are_immutable_everywhere(client_mod, moderator, fresh_user, guide_from_author):
    report = services.report_content(
        reporter=fresh_user, target=guide_from_author,
        reason=ReportReason.SPAM.value, now=mock_now(),
    )
    services.settle_report(
        report, moderator=moderator,
        decision="remove", now=mock_now(),
    )
    row = ModeratorAudit.objects.order_by("pk").last()
    row.reason = ReportReason.SCAM.value
    with pytest.raises(ValidationError):
        row.save()
    row_pk = row.pk
    with pytest.raises(ValidationError):
        row.delete()
    with pytest.raises(ValidationError):
        ModeratorAudit.objects.filter(pk=row_pk).update(reason="X")
    with pytest.raises(ValidationError):
        ModeratorAudit.objects.all().delete()
    assert ModeratorAudit.objects.filter(pk=row_pk).exists()


@pytest.mark.django_db
def test_suspending_actor_is_protected_from_deletion(fresh_user, guide_from_author):
    services.report_content(
        reporter=fresh_user, target=guide_from_author,
        reason=ReportReason.SPAM.value, now=mock_now(),
    )
    with pytest.raises(ProtectedError):
        fresh_user.delete()  # audit rows exist -> no deletion, by design
        # (Prompt 40 KDPA erasure will anonymize actors first; deletion stays blocked until then.)


# ------------------------------------------------------ suspension two-actor

@pytest.mark.django_db
def test_suspension_needs_two_different_humans(moderator, second_moderator, other_user):
    draft = services.draft_suspension(
        moderator=moderator, target_user=other_user,
        reason="Serial scam listings", now=mock_now(),
    )
    assert draft.status == "DRAFT"
    # The drafter cannot confirm their own action (the human gate is code).
    with pytest.raises(ValidationError):
        services.confirm_suspension(draft, confirmer=moderator)
    assert other_user.is_active  # still untouched
    services.confirm_suspension(draft, confirmer=second_moderator, now=mock_now())
    other_user.refresh_from_db()
    draft.refresh_from_db()
    assert other_user.is_active is False  # reversible deactivation
    assert draft.status == "CONFIRMED"
    assert draft.confirmed_by_id == second_moderator.pk
    actions = list(
        ModeratorAudit.objects.order_by("pk").values_list("action", "actor__username")
    )
    assert actions == [
        (AuditAction.SUSPENSION_DRAFTED.value, moderator.username),
        (AuditAction.SUSPENSION_CONFIRMED.value, second_moderator.username),
    ]


@pytest.mark.django_db
def test_suspension_view_role_gate_and_self_guards(client_mod, moderator, other_user):
    url = reverse("moderation:draft")
    # Suspending yourself is refused...
    response = client_mod.post(url, {"user": moderator.pk, "reason": "test"})
    assert response.status_code == 400
    # ...and an empty reason is refused (KDPA-style minimality: say why).
    response = client_mod.post(url, {"user": other_user.pk, "reason": "  "})
    assert response.status_code == 400
    response = client_mod.post(url, {"user": other_user.pk, "reason": "Scam wave"})
    assert response.status_code == 302
    draft = SuspensionDraft.objects.get()
    # Same moderator confirming via the endpoint is refused (service rule).
    response = client_mod.post(reverse("moderation:confirm", args=[draft.pk]), {})
    assert response.status_code == 400
    assert other_user.is_active  # nothing slipped through the gate


# ------------------------------------------------------------------- budget

@pytest.mark.django_db
def test_queue_budget_with_20_reports(client_mod, fresh_user, guide_from_author):
    """20-item queue fixture must stay inside the 50 KB mobile budget."""
    from django.contrib.contenttypes.models import ContentType

    ct = ContentType.objects.get_for_model(Guide)
    for _ in range(20):  # ORM directly: budget fixture is not quota-affected
        Report.objects.create(
            reporter=fresh_user, reason=ReportReason.SPAM.value,
            target_ct=ct, target_id=guide_from_author.pk,
        )
    response = client_mod.get(f"{reverse('moderation:queue')}?page=1")
    assert response.status_code == 200
    assert_under_budget(response.content.decode(), kb=50)
