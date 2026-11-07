"""Compliance models (PROMPTS.md Prompt 40) — the KDPA registry + DSAR queue.

WHAT this module is: Prompt 40 makes the DPA (appended to research-notes
§KDPA) operational. Two tables live here:

* DataMapEntry — the ``DataMap`` registry: for every (app, model) that
  holds personal data, the personal-data field names, the purpose of
  processing, the lawful basis (the DPA vocabulary) and the retention
  period. It is SELF-DOCUMENTING: rows are upserted from the
  hand-maintained dict in apps/compliance/inventory.py and test-audited
  against framework reflection (tests/test_compliance.py) — the
  inventory completeness tests fail the suite when code and registry
  drift apart in EITHER direction.
* DataRequest — the data-subject access/deletion/correction queue
  (DSAR): who asked, which kind, what state, who handled, when, why.

NO USER DATA IS STORED HERE — the registry DESCRIBES data, it does not
hold any (rows carry table/field NAMES, not values; see the columns
purity test). DataRequest rows are the paper trail required to *prove*
a request was answered — that is their only personal data (a user FK).
"""
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.core.types import TimestampedModel


class LawfulBasis(models.TextChoices):
    """The DPA vocabulary for lawful bases (exactly four values).

    These are the enum values stored on DataMapEntry rows and rendered on
    the data map — an ODPC auditor reads purpose + basis + retention per
    table directly off /compliance/ (the acceptance is that the auditor's
    "what data, why" ask is answerable from the app itself).
    """

    CONSENT = "CONSENT", _("Consent")
    CONTRACT = "CONTRACT", _("Contract")
    LEGITIMATE_INTEREST = "LEGITIMATE_INTEREST", _("Legitimate interest")
    LEGAL_OBLIGATION = "LEGAL_OBLIGATION", _("Legal obligation")


class RequestKind(models.TextChoices):
    """DSAR kinds. CORRECT is self-serve (profiles:me) — the request row
    still exists so the ledger shows the ask was answered."""

    ACCESS = "ACCESS", _("Access (export my data)")
    DELETE = "DELETE", _("Deletion (anonymize me)")
    CORRECT = "CORRECT", _("Correction (point me at profile edit)")


class RequestStatus(models.TextChoices):
    """DSAR queue state: OPEN → FULFILLED | REFUSED (no re-open: a refused
    request is a decision with a reason, recorded for the ODPC file)."""

    OPEN = "OPEN", _("Open")
    FULFILLED = "FULFILLED", _("Fulfilled")
    REFUSED = "REFUSED", _("Refused")


class DataMapEntry(TimestampedModel):
    """One row of the data map: (app_label, model_name) → personal fields.

    NOTE: columns are EXACTLY {id, app_label, model_name, fields, purpose,
    lawful_basis, retention_days, notes, created_at, updated_at} — no user
    FK, no user-identifiable column — the registry must never itself
    become a personal-data store (tests/test_compliance.py pins this the
    same way apps/analytics pins its aggregate tables).
    """

    app_label = models.CharField(_("App"), max_length=32, db_index=True)
    model_name = models.CharField(_("Model"), max_length=64)
    fields = models.JSONField(_("Personal-data fields"), default=list)
    purpose = models.CharField(
        _("Purpose of processing"), max_length=400,
        help_text=_("WHY this processing exists (the DPA purpose statement)."),
    )
    lawful_basis = models.CharField(
        _("Lawful basis"), max_length=24, choices=LawfulBasis.choices, db_index=True,
    )
    retention_days = models.PositiveIntegerField(
        _("Retention (days)"), null=True, blank=True,
        help_text=_("None = kept while the account lives, then follows the "
                    "documented deletion trade-off. A number = swept rows "
                    "older than this (retention.py sweep)."),
    )
    notes = models.TextField(
        _("Notes"), blank=True, default="",
        help_text=_("Deletion trade-off, exclusions, and anything the DPA "
                    "balances — written where the auditor reads it."),
    )

    class Meta:
        ordering = ["app_label", "model_name"]
        constraints = [
            models.UniqueConstraint(
                fields=["app_label", "model_name"], name="uniq_datamap_app_model",
            ),
        ]
        verbose_name = _("Data map entry")
        verbose_name_plural = _("Data map entries")

    def __str__(self) -> str:
        return f"{self.app_label}.{self.model_name} ({self.lawful_basis})"

    def clean(self):
        """Registry sanity: lawful basis from the enum, fields a real list,
        retention a non-negative number. Keeping this on the MODEL means
        the admin and the loader both inherit the same floor."""
        super().clean()
        errors = {}
        if self.lawful_basis not in LawfulBasis.values:
            errors["lawful_basis"] = _("Pick a lawful basis from the DPA vocabulary.")
        names = self.fields if isinstance(self.fields, list) else None
        if (
            not names
            or not all(isinstance(f, str) and f for f in names)
            or len(set(names)) != len(names)
        ):
            errors["fields"] = _("List non-empty, distinct field names.")
        if errors:
            raise ValidationError(errors)


class DataRequest(TimestampedModel):
    """One data-subject request (DSAR) and its answer.

    NOTE: ``user`` FK is on_delete=PROTECT — WHY: a fulfilled deletion
    request must never be erased when the account row later changes hands;
    the request ledger is the ODPC evidence that a right was exercised and
    answered. handled_by is SET_NULL (a staff member may leave; the
    decision stands, the name may go).
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="data_requests",
        verbose_name=_("Requester"),
    )
    kind = models.CharField(_("Kind"), max_length=10, choices=RequestKind.choices)
    status = models.CharField(
        _("Status"), max_length=12, choices=RequestStatus.choices,
        default=RequestStatus.OPEN, db_index=True,
    )
    handled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="dsars_handled",
        verbose_name=_("Handled by"),
    )
    handled_at = models.DateTimeField(_("Handled at"), null=True, blank=True)
    note = models.TextField(
        _("Note"), blank=True, default="",
        help_text=_("Member context when opening; the refusal REASON (or "
                    "fulfilment pointer) when handled."),
    )

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["user", "status"], name="dsar_user_status_idx")]
        verbose_name = _("Data request")
        verbose_name_plural = _("Data requests")

    def __str__(self) -> str:
        return f"{self.kind} · {self.user} · {self.status}"

    def clean(self):
        errors = {}
        if self.kind not in RequestKind.values:
            errors["kind"] = _("Unknown request kind.")
        if self.status not in RequestStatus.values:
            errors["status"] = _("Unknown request status.")
        if (self.status != RequestStatus.OPEN) and not self.handled_at:
            errors["handled_at"] = _("Stamp when it was handled.")
        if errors:
            raise ValidationError(errors)
