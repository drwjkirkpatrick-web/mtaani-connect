"""Moderation services (Prompt 15) — one door for every write + shared reads.

SIBLING INTEGRATION (read this before building feeds/search):

    from apps.moderation.services import (
        is_blocked, excluding_blocked_for, visible, apply_removal,
    )

    # 1) Hide everything published by users the viewer has blocked:
    posts = excluding_blocked_for(viewer, Post.objects.all(),
                                  author_field="author")   # FK path to User
    # author_field accepts DOTTED paths, e.g. for guide revisions:
    revisions = excluding_blocked_for(viewer, GuideRevision.objects.all(),
                                      author_field="guide__created_by")

    # 2) One-call "public surface": excludes REMOVED content AND blocked
    #    authors (status skip is graceful when a model has no status field):
    qs = visible(viewer, Post.objects.all(), author_field="author")
    qs = visible(viewer, GuideRevision.objects.all(),
                 author_field="guide__created_by")

    # 3) Point-in-time pair check:
    is_blocked(viewer, other)  -> True when viewer has blocked `other`

    # 4) Generic removal for objects exposing a `status` field
    #    (ContentStatus): flips status to REMOVED and saves only the status
    #    column. Returns True when applied, False for status-less targets.

Blocking hides the BLOCKED user's content FROM the blocker only — it is not a
site-wide ban (that is what suspensions/REMOVED are for).
"""
from __future__ import annotations

from datetime import timedelta

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.core.types import ContentStatus

from .models import (
    AuditAction,
    Block,
    ModeratorAudit,
    Report,
    ReportStatus,
    SuspensionDraft,
    SuspensionStatus,
)

# Report-endpoint rate limit (PROMPTS.md pitfall): 10 reports/hour/user.
MAX_REPORTS_PER_HOUR = 10
RATE_WINDOW = timedelta(minutes=60)

REMOVE_DECISION = "remove"
RESTORE_DECISION = "restore"
DISMISS_DECISION = "dismiss"
REVIEW_DECISION = "review"
RESOLVE_DECISIONS = (
    REMOVE_DECISION,
    RESTORE_DECISION,
    DISMISS_DECISION,
    REVIEW_DECISION,
)
RESOLVE_DELETES_CONTENT = False  # WHY: never hard-delete — status flips only


class RateLimitError(ValidationError):
    """Raised for the 11th report in an hour; views answer HTTP 429."""


def is_moderator(user) -> bool:
    """Role gate for Prompt 15 tooling.

    CODE-ONLY RULE: reads accounts PUBLIC attributes only (is_staff /
    is_superuser) — no cross-app imports, no new tables. A dedicated
    moderator/group table arrives with Prompt 37 and only this function
    changes then.
    """
    if user is None:
        return False
    if not getattr(user, "is_authenticated", False):
        return False
    return bool(user.is_staff or user.is_superuser)


def _now(now):
    return timezone.now() if now is None else now


def _audit(*, actor, action: str, target, reason: str = "") -> ModeratorAudit:
    """Append one immutable audit row. Returns the row (tests assert on it)."""
    from django.contrib.contenttypes.models import ContentType

    return ModeratorAudit.objects.create(
        actor=actor,
        action=action,
        target_ct=ContentType.objects.get_for_model(target),
        target_id=target.pk,
        reason=reason or "",
    )


def _status_save(target, new_value: str):
    """Flip a status column on ANY model exposing `status`, touching only the
    status (and updated_at when present). Generic by contract: works with
    real models and test stubs alike."""
    update_fields = ["status"]
    target.status = new_value
    # include updated_at only if the concrete model has that column
    meta = getattr(target, "_meta", None)
    if meta is not None:
        try:
            meta.get_field("updated_at")
            update_fields.append("updated_at")
        except Exception:  # noqa: BLE001 — no updated_at column, nothing to add
            pass
    target.save(update_fields=update_fields)


def apply_removal(target) -> bool:
    """Set ContentStatus.REMOVED on any target with a `status` field.

    WHY save(update_fields=...): the target's model may have clean() rules we
    must not re-run against a stale in-memory copy (guides' safety gate);
    touching one column is the only safe generic write.
    Returns False (no-op) for targets without a status lifecycle (e.g. Profile).
    """
    if getattr(target, "status", None) is None:
        return False
    _status_save(target, ContentStatus.REMOVED.value)
    return True


def restore_target(target) -> bool:
    """Undo a moderator removal: REMOVED -> PUBLISHED (rows were never gone)."""
    if getattr(target, "status", None) is None:
        return False
    if target.status != ContentStatus.REMOVED.value:
        return False
    _status_save(target, ContentStatus.PUBLISHED.value)
    return True


@transaction.atomic
def report_content(
    *, reporter, target, reason: str, note: str = "", now=None
) -> Report:
    """Record a member's report. Rate-limited to MAX_REPORTS_PER_HOUR per
    rolling hour (PROMPTS.md pitfall). `now` is injectable for tests.
    Raises RateLimitError (a ValidationError with code 'report_rate_limited')
    for the 11th report; the view turns that into HTTP 429.
    """
    from django.contrib.contenttypes.models import ContentType

    if not getattr(reporter, "is_authenticated", False):
        raise ValidationError(_("Log in to report content."))
    now = _now(now)
    # Rate limit first (fail cheap) — WHY lower bound only: rows written after
    # `now` (auto_now_add with a real clock during a test's injected clock)
    # still count, so the injected-clock tests stay deterministic.
    window_start = now - RATE_WINDOW
    recent = Report.objects.filter(
        reporter=reporter, created_at__gte=window_start
    ).count()
    if recent >= MAX_REPORTS_PER_HOUR:
        raise RateLimitError(
            _(
                "You have reached the hourly limit of %(limit)s reports. "
                "Please try again later."
            )
            % {"limit": MAX_REPORTS_PER_HOUR},
            code="report_rate_limited",
        )
    report = Report(
        reporter=reporter,
        reason=reason,
        note=(note or "").strip(),
        target_ct=ContentType.objects.get_for_model(target),
        target_id=target.pk,
        status=ReportStatus.OPEN.value,
    )
    report.full_clean()  # app-label allow-list + reason/note validation
    report.save()
    _audit(
        actor=reporter,
        action=AuditAction.REPORT_CREATED.value,
        target=target,
        reason=reason,
    )
    return report


def start_review(report: Report, *, moderator, now=None) -> Report:
    """OPEN -> UNDER_REVIEW (a moderator has picked the report up)."""
    if report.status != ReportStatus.OPEN.value:
        raise ValidationError(_("Only an open report can move under review."))
    report.status = ReportStatus.UNDER_REVIEW.value
    report.save(update_fields=["status", "updated_at"])
    _audit(
        actor=moderator,
        action=AuditAction.REVIEW_STARTED.value,
        target=report.target,
        reason=report.reason,
    )
    return report


@transaction.atomic
def settle_report(
    report: Report, *, moderator, decision: str, decision_note: str = "", now=None
) -> Report:
    """Queue decision on one report — the acceptance-chain 'moderator action'.

    decision:
      review   -> REPORT goes UNDER_REVIEW (no verdict yet)
      dismiss  -> REPORT dismissed; target untouched
      remove   -> target status -> REMOVED (never deleted); report RESOLVED
      restore  -> target REMOVED status -> PUBLISHED again; report RESOLVED
    Each decision appends its own ModeratorAudit row; the report's stored
    ReportReason is reused as the audit reason (completeness without PII).
    """
    if decision not in RESOLVE_DECISIONS:
        raise ValidationError(_("Unknown moderation decision."))
    report.decision_note = (decision_note or "").strip()
    if decision == REVIEW_DECISION:
        report.status = ReportStatus.UNDER_REVIEW.value
        report.save(update_fields=["status", "decision_note", "updated_at"])
        _audit(
            actor=moderator,
            action=AuditAction.REVIEW_STARTED.value,
            target=report.target,
            reason=report.reason,
        )
        return report
    if decision == DISMISS_DECISION:
        report.status = ReportStatus.DISMISSED.value
    else:
        target = report.target
        if target is None:
            raise ValidationError(_("The reported content no longer exists."))
        changed = (
            apply_removal(target)
            if decision == REMOVE_DECISION
            else restore_target(target)
        )
        if not changed:
            if decision == REMOVE_DECISION:
                raise ValidationError(_("This target cannot be removed (no status lifecycle)."))
            raise ValidationError(_("Only removed content can be restored."))
        report.status = ReportStatus.RESOLVED.value  # decided: action taken
        audit_action = (
            AuditAction.REMOVED.value
            if decision == REMOVE_DECISION
            else AuditAction.RESTORED.value
        )
        _audit(
            actor=moderator,
            action=audit_action,
            target=target,
            reason=report.reason,
        )
    report.decided_by = moderator
    report.decided_at = _now(now)
    report.save(update_fields=["status", "decided_by", "decided_at", "decision_note", "updated_at"])
    if decision == DISMISS_DECISION:
        _audit(
            actor=moderator,
            action=AuditAction.DISMISSED.value,
            target=report.target if report.target else report,
            reason=report.reason,
        )
    return report


@transaction.atomic
def block_user(*, blocker, blocked, now=None) -> Block:
    """The blocker stops seeing the blocked member's content app-wide."""
    if blocker == blocked or not blocked.pk:
        raise ValidationError(_("You cannot block yourself."))
    if Block.objects.filter(blocker=blocker, blocked=blocked).exists():
        raise ValidationError(_("You have already blocked this member."))
    block = Block(blocker=blocker, blocked=blocked)
    block.full_clean()
    block.save()
    _audit(actor=blocker, action=AuditAction.BLOCKED.value, target=blocked)
    return block


@transaction.atomic
def unblock_user(*, blocker, blocked, now=None) -> Block:
    """Revoke a block (unfollow-style: the active row is removed; the audit
    history of BLOCK and UNBLOCK remains)."""
    try:
        block = Block.objects.get(blocker=blocker, blocked=blocked)
    except Block.DoesNotExist as exc:
        raise ValidationError(_("You have not blocked this member.")) from exc
    _audit(actor=blocker, action=AuditAction.UNBLOCKED.value, target=blocked)
    block.delete()
    return block


def is_blocked(viewer, other) -> bool:
    """True when `viewer` has blocked `other`. Public helper for all apps."""
    if viewer is None or other is None:
        return False
    if not getattr(viewer, "is_authenticated", False):
        return False
    if viewer.pk == getattr(other, "pk", None):
        return False
    return Block.objects.filter(blocker=viewer, blocked_id=other.pk).exists()


def excluding_blocked_for(viewer, queryset, *, author_field: str = "author"):
    """Exclude objects whose author `viewer` has blocked (dotted FK path OK).

    Generic by contract: `author_field` names the FK that owns the content
    ("author", "created_by", "user", "guide__created_by", ...). Anonymous or
    None viewers see everything (they cannot have blocks).
    """
    if viewer is None or not getattr(viewer, "is_authenticated", False):
        return queryset
    blocked_ids = list(
        Block.objects.filter(blocker=viewer).values_list("blocked_id", flat=True)
    )
    if not blocked_ids:
        return queryset
    return queryset.exclude(**{f"{author_field}__in": blocked_ids})


def visible(viewer, queryset, *, author_field: str = "author", status_field: str = "status"):
    """The public read-surface helper for feeds & search (Prompt 15 contract):
    excludes ContentStatus.REMOVED rows and viewers-blocked authors.

    WHY one helper: 'REMOVED excluded from feeds/search' fails as N app-local
    interpretations; every feed/search calls THIS and the rule stays one.
    """
    if status_field:
        try:
            queryset.model._meta.get_field(status_field)
        except Exception:  # noqa: BLE001 — no status column: skip gracefully
            pass
        else:
            queryset = queryset.exclude(**{f"{status_field}": ContentStatus.REMOVED.value})
    return excluding_blocked_for(viewer, queryset, author_field=author_field)


@transaction.atomic
def draft_suspension(*, moderator, target_user, reason: str, now=None) -> SuspensionDraft:
    """Step 1 of the two-actor gate: write the suspension down. Nothing is
    changed on the target account — only the draft + audit row."""
    if not is_moderator(moderator):
        raise ValidationError(_("Only moderators can draft suspensions."))
    if moderator.pk == target_user.pk:
        raise ValidationError(_("You cannot suspend your own account."))
    reason = (reason or "").strip()
    if not reason:
        raise ValidationError(_("A suspension needs a stated reason."))
    draft = SuspensionDraft(
        target=target_user, drafted_by=moderator, reason=reason,
        status=SuspensionStatus.DRAFT.value,
    )
    draft.full_clean()
    draft.save()
    _audit(
        actor=moderator,
        action=AuditAction.SUSPENSION_DRAFTED.value,
        target=target_user,
    )
    return draft


@transaction.atomic
def confirm_suspension(draft: SuspensionDraft, *, confirmer, now=None) -> SuspensionDraft:
    """Step 2 of the two-actor gate: a DIFFERENT human confirms; the account
    is deactivated (is_active=False — reversible, not a deletion)."""
    if not is_moderator(confirmer):
        raise ValidationError(_("Only moderators can confirm suspensions."))
    if draft.status != SuspensionStatus.DRAFT.value:
        raise ValidationError(_("Only a draft suspension can be confirmed."))
    if confirmer.pk == draft.drafted_by_id:
        # Two-actor rule, always: the human-review gate is code, not culture.
        raise ValidationError(
            _("A suspension must be confirmed by a different moderator.")
        )
    if draft.target.pk == confirmer.pk:
        raise ValidationError(_("You cannot confirm your own suspension."))
    draft.target.is_active = False
    draft.target.save(update_fields=["is_active"])
    draft.confirmed_by = confirmer
    draft.status = SuspensionStatus.CONFIRMED.value
    draft.decided_at = _now(now)
    draft.save(update_fields=["confirmed_by", "status", "decided_at", "updated_at"])
    _audit(
        actor=confirmer,
        action=AuditAction.SUSPENSION_CONFIRMED.value,
        target=draft.target,
    )
    return draft


@transaction.atomic
def cancel_suspension(draft: SuspensionDraft, *, moderator, now=None) -> SuspensionDraft:
    """A pending draft can be cancelled (e.g. target already suspended, or
    the reviewer disagrees). Reversible by design."""
    if not is_moderator(moderator):
        raise ValidationError(_("Only moderators can cancel suspensions."))
    if draft.status != SuspensionStatus.DRAFT.value:
        raise ValidationError(_("Only a draft suspension can be cancelled."))
    draft.status = SuspensionStatus.CANCELLED.value
    draft.decided_at = _now(now)
    draft.save(update_fields=["status", "decided_at", "updated_at"])
    _audit(
        actor=moderator,
        action=AuditAction.SUSPENSION_CANCELLED.value,
        target=draft.target,
    )
    return draft


def queue_reports():
    """The moderation queue surface: open + under-review reports, oldest
    attention first (queue freshness). Public so tests/other apps read it."""
    return Report.objects.filter(
        status__in=[ReportStatus.OPEN.value, ReportStatus.UNDER_REVIEW.value]
    ).order_by("created_at")


def pending_suspensions():
    return SuspensionDraft.objects.filter(
        status=SuspensionStatus.DRAFT.value
    ).order_by("created_at")
