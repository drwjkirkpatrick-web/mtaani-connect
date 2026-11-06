"""Moderation models (PROMPTS.md Prompt 15): Report, Block, SuspensionDraft,
ModeratorAudit.

Design decisions documented for the next cold-start reader:
- Removal is a TARGET-OBJECT status flip (ContentStatus.REMOVED), never a row
  deletion — evidence retention for disputes (apps/moderation/services.py).
- Reports/audit reference targets through Django's generic ContentType FK, so
  one moderation surface covers posts, guides, Q&A and profiles without
  importing any sibling app (cross-app reads stay in their public APIs).
- ModeratorAudit rows are append-only: save() refuses updates, delete() and
  queryset update()/delete() raise — corrections are recorded as NEW rows.
"""
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxLengthValidator
from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.core.types import ReportReason, TimestampedModel

# App labels whose objects may be reported TODAY. The verbatim Prompt 15 spec
# names posts, guides, Q&A, listings, profiles, messages — Phase 2 landed
# listings (Prompt 17 auto-flag) and messaging (Prompt 20 report flow);
# add further apps as they land (values are app_labels, no migration needed).
ALLOWED_TARGET_APPS = ("posts", "guides", "qa", "profiles", "listings", "messaging")

MAX_NOTE_CHARS = 500


class ReportStatus(models.TextChoices):
    """Queue state of one report (kept in-app; it is moderation-only state)."""

    OPEN = "OPEN", _("Open")
    UNDER_REVIEW = "UNDER_REVIEW", _("Under review")
    RESOLVED = "RESOLVED", _("Resolved")
    DISMISSED = "DISMISSED", _("Dismissed")


class SuspensionStatus(models.TextChoices):
    """Two-actor gate state: drafted by one human, confirmed by another."""

    DRAFT = "DRAFT", _("Draft")
    CONFIRMED = "CONFIRMED", _("Confirmed")
    CANCELLED = "CANCELLED", _("Cancelled")


class AuditAction(models.TextChoices):
    """Frozen vocabulary for audit rows (persist .value, never labels)."""

    REPORT_CREATED = "REPORT_CREATED", _("Report created")
    REVIEW_STARTED = "REVIEW_STARTED", _("Review started")
    REMOVED = "REMOVED", _("Content removed")
    RESTORED = "RESTORED", _("Content restored")
    DISMISSED = "DISMISSED", _("Report dismissed")
    BLOCKED = "BLOCKED", _("User blocked")
    UNBLOCKED = "UNBLOCKED", _("User unblocked")
    SUSPENSION_DRAFTED = "SUSPENSION_DRAFTED", _("Suspension drafted")
    SUSPENSION_CONFIRMED = "SUSPENSION_CONFIRMED", _("Suspension confirmed")
    SUSPENSION_CANCELLED = "SUSPENSION_CANCELLED", _("Suspension cancelled")


class Report(TimestampedModel):
    """One member's complaint about one content object (generic target).

    NOTE: reports survive target deletion (target_ct.SET_NULL) — the row keeps
    reporter/reason/note/timestamps for dispute evidence even if the offending
    object is later hard-deleted by its author.
    """

    reporter = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="moderation_reports",
    )
    reason = models.CharField(_("Reason"), max_length=32, choices=ReportReason.choices)
    note = models.TextField(
        _("Note"),
        blank=True,
        default="",
        max_length=MAX_NOTE_CHARS,
        validators=[MaxLengthValidator(MAX_NOTE_CHARS)],
    )
    target_ct = models.ForeignKey(
        ContentType,
        null=True,
        on_delete=models.PROTECT,
        related_name="+",
        limit_choices_to={"app_label__in": ALLOWED_TARGET_APPS},
    )
    target_id = models.PositiveBigIntegerField(null=True, blank=True)
    target = GenericForeignKey("target_ct", "target_id")
    status = models.CharField(
        _("Status"),
        max_length=16,
        choices=ReportStatus.choices,
        default=ReportStatus.OPEN,
        db_index=True,
    )
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="moderation_decisions",
    )
    decided_at = models.DateTimeField(_("Decided at"), null=True, blank=True)
    # Moderator-facing prose (NOT copied into audit rows — KDPA minimization:
    # audit rows carry only actor/action/target/reason-enum/timestamp).
    decision_note = models.TextField(_("Decision note"), blank=True, default="")

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["reporter", "created_at"], name="modreport_reporter_when"),
            models.Index(fields=["status", "-created_at"], name="modreport_queue"),
        ]

    def __str__(self) -> str:
        return f"{self.reason} — {self.target_summary}"

    @property
    def target_summary(self) -> str:
        """Short human label of the target for the queue page."""
        target = self.target
        return str(getattr(target, "display_title", None) or target or "?")[:80]

    def clean(self):
        # The allow-list gate lives on the MODEL, not just the form, so every
        # future write path (services, admin, scripts) inherits it.
        if self.target_ct_id is None:
            raise ValidationError({"target_id": _("A report needs a target.")})
        if self.target_ct.app_label not in ALLOWED_TARGET_APPS:
            raise ValidationError(
                {
                    "target_id": _(
                        "Only posts, guides, Q&A and profiles can be reported."
                    )
                }
            )


class Block(TimestampedModel):
    """blocker hides blocked's content app-wide (for the blocker only).

    Revocable: unblock deletes the row (audit keeps the history). Self-blocks
    are refused in clean(); the (blocker, blocked) pair is unique.
    """

    blocker = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="blocks_made",
    )
    blocked = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="blocks_taken",
    )

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["blocker", "blocked"], name="uniq_block_pair"
            )
        ]

    def __str__(self) -> str:
        return f"{self.blocker} blocked {self.blocked}"

    def clean(self):
        if self.blocker_id and self.blocker_id == self.blocked_id:
            raise ValidationError(_("You cannot block yourself."))


class SuspensionDraft(TimestampedModel):
    """Two-actor account suspension gate (PROMPTS.md human review rule).

    One moderator DRAFTS (reason written down), a DIFFERENT moderator
    confirms; confirming deactivates the account (is_active=False). The
    drafter==confirmer refusal is enforced in the confirm service always —
    'where possible' is implemented as 'always', because a single-moderator
    community should surface the gap, not bypass the gate.
    """

    target = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="suspension_drafts",
    )
    drafted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="suspensions_drafted",
    )
    confirmed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="suspensions_confirmed",
    )
    # WHY no auto-prose caps beyond 500: an honest suspension reason needs
    # room, but it is moderator-written, never user input.
    reason = models.TextField(_("Reason"), max_length=500)
    status = models.CharField(
        _("Status"),
        max_length=16,
        choices=SuspensionStatus.choices,
        default=SuspensionStatus.DRAFT,
        db_index=True,
    )
    decided_at = models.DateTimeField(_("Decided at"), null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["status", "-created_at"], name="modsusp_pending")]

    def __str__(self) -> str:
        return f"Suspension of {self.target} ({self.status})"

    def clean(self):
        if self.target_id and self.target_id == self.drafted_by_id:
            raise ValidationError(_("You cannot suspend your own account."))


class ModeratorAuditQuerySet(models.QuerySet):
    """WHY a guard at ORM level too: accidental .update()/.delete() on audit
    rows must fail loudly, not silently rewrite evidence."""

    def update(self, *args, **kwargs):
        raise ValidationError(_("Audit rows are immutable (no bulk updates)."))

    def delete(self, *args, **kwargs):
        raise ValidationError(_("Audit rows are never deleted (evidence retention)."))


class ModeratorAuditManager(models.Manager.from_queryset(ModeratorAuditQuerySet)):
    """Default manager: read-only surface. bulk_create stays possible (new rows)."""


class ModeratorAudit(models.Model):
    """Append-only audit row for every moderation action (Prompt 15).

    Columns are exactly the KDPA minimum: actor, action, target, reason
    (enum .value or ""), created_at. NO IP, no prose, no device data.
    Never updated: corrections arrive as NEW rows via the follow-up services.
    """

    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,  # WHY: no user deletion while audit rows exist
        related_name="+",          # (Prompt 40 KDPA erasure will anonymize first)
    )
    action = models.CharField(_("Action"), max_length=32, choices=AuditAction.choices)
    target_ct = models.ForeignKey(
        ContentType, on_delete=models.PROTECT, related_name="+"
    )
    target_id = models.PositiveBigIntegerField()
    # enum .value (ReportReason or "") — never the translatable label.
    reason = models.CharField(_("Reason"), max_length=32, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    objects = ModeratorAuditManager()  # read-only guard on update()/delete()

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["action", "-created_at"], name="modaudit_action_when"),
            models.Index(
                fields=["target_ct", "target_id", "-created_at"],
                name="modaudit_target_when",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.actor} {self.action} -> {self.target_id} ({self.created_at:%Y-%m-%d})"

    def save(self, *args, **kwargs):
        # Read-only pattern: creation allowed, mutation refused. (Why not
        # update_fields tricks? Any second save() on a fetched row IS a
        # mutation attempt and must fail loudly.)
        if not self._state.adding:
            raise ValidationError(_("Audit rows are immutable; write a new row instead."))
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError(_("Audit rows are never deleted; evidence is retained."))

    def target_object(self):
        """Resolve the generic FK lazily; returns None if the target vanished."""
        try:
            return self.target_ct.get_object_for_this_type(pk=self.target_id)
        except Exception:
            return None
