"""Q&A models (Prompt 12) — bilingual community questions, multiple answers,
an asker-accepted answer, per-answer verification votes, and AI-answer
labeling.

THE PITFALL guarded here (PROMPTS.md 12): the "AI answer" badge is
DATA-DRIVEN. `Answer.is_machine` is a plain boolean column on the row —
the single source of truth. Prompt 33's AI layer sets it via
apps.qa.services (set_machine_answer); NO form, serializer or view contains
the field, so a user can never set it. Admin (staff) may still correct it
— deliberate human override, per the "human review gates" hard rule.
Templates read `{% if a.is_machine %}` — never a position/username
convention that a future surface could forget.

Safety mirrors guides (Prompt 08): the QUESTION carries a SafetyCategory
flag (apps.core.types, `.value` stored); its ANSWERS inherit that flag, and
an answer to a medical/electrical/construction question MUST carry a
safety_warning (model clean() — not just the form, so admin/API/script
write paths inherit the gate). For MEDICAL/ELECTRICAL questions the accept
service refuses to accept an answer unless a human-written answer with at
least one community verification vote exists alongside.
"""
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxLengthValidator
from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.core.types import ContentStatus, SafetyCategory, TimestampedModel

# WHY a local copy of the risky tuple (guides has RISKY_SAFETY_CATEGORIES in
# its models): the guide pattern is mirrored, not shared, so qa keeps its own
# spec-exact gate and importing guide PRIVATES stays forbidden.
RISKY_SAFETY_CATEGORIES = (
    SafetyCategory.MEDICAL.value,
    SafetyCategory.ELECTRICAL.value,
    SafetyCategory.CONSTRUCTION.value,
)

# Accept-gate scope per Prompt 12 verbatim: "medical/electrical answers ...
# can never be the accepted answer without a human-verified community answer
# alongside". Construction answers carry a warning but are not accept-gated.
ACCEPT_GATE_CATEGORIES = (
    SafetyCategory.MEDICAL.value,
    SafetyCategory.ELECTRICAL.value,
)

# WHY a cap: the same 50 KB mobile budget as guides — runaway question or
# answer text is the cheapest way to blow it. Enforced in full_clean().
MAX_BODY_CHARS = 2000

_MAX_BODY_VALIDATOR = MaxLengthValidator(MAX_BODY_CHARS)


class Question(TimestampedModel):
    """A community question. `safety_category` is the QUESTION-context flag:
    answers get flagged through it (medical question → all answers risky)."""

    title_en = models.CharField(_("Question (English)"), max_length=200)
    title_sw = models.CharField(
        _("Question (Kiswahili)"), max_length=200, blank=True, default=""
    )
    # PLAIN TEXT only: templates render |linebreaks, never |safe.
    body = models.TextField(
        _("Question details"),
        blank=True,
        default="",
        validators=[_MAX_BODY_VALIDATOR],
    )
    safety_category = models.CharField(
        _("Safety handling"),
        max_length=16,
        choices=SafetyCategory.choices,
        default=SafetyCategory.NONE,
        db_index=True,
    )
    asked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="qa_questions"
    )
    # WHY OneToOne: at most one accepted answer per question, enforced by the
    # DB itself. Nullable because answering comes first; the ASKER sets it via
    # the accept service — no form ever writes this field directly.
    accepted_answer = models.OneToOneField(
        "Answer",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="accepted_by_question",
        verbose_name=_("Accepted answer"),
    )
    # NOTE: no revision flow (guides needed one; Q&A keeps it simple) — a
    # question is PUBLISHED the moment it is asked; moderation (Prompt 15)
    # layers REMOVED/UNDER_REVIEW on top later.
    status = models.CharField(
        _("Status"), max_length=16, choices=ContentStatus.choices,
        default=ContentStatus.PUBLISHED, db_index=True,
    )

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return self.display_title

    @property
    def display_title(self) -> str:
        """Kiswahili question when present, English otherwise (i18n fallback)."""
        return self.title_sw or self.title_en

    @property
    def title_is_fallback(self) -> bool:
        """True when the English title stands in for a missing Kiswahili one."""
        return not self.title_sw

    @property
    def risky(self) -> bool:
        """True when answers to this question must carry a safety warning."""
        return self.safety_category in RISKY_SAFETY_CATEGORIES

    def clean(self):
        # The gate lives on the MODEL (PITFALL pattern from guides): any
        # future write path (admin, API, script) inherits it automatically.
        if (
            self.accepted_answer_id is not None
            and self.accepted_answer.question_id != self.pk
        ):
            raise ValidationError(
                {
                    "accepted_answer": _(
                        "The accepted answer must belong to this question."
                    )
                }
            )


class Answer(TimestampedModel):
    """One answer to a community question.

    `is_machine` is the AI badge (see module docstring): False until the AI
    layer (Prompt 33) marks the answer. Never present in any form.
    """

    question = models.ForeignKey(
        Question, on_delete=models.CASCADE, related_name="answers"
    )
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="qa_answers"
    )
    # PLAIN TEXT only: |linebreaks, never |safe.
    body = models.TextField(_("Answer"), validators=[_MAX_BODY_VALIDATOR])
    # WHY a copy of the QUESTION's flag (snapshotted by services.post_answer):
    # clean() must never dereference an unresolved FK (form validation and
    # admin paths build half-built instances), and answers pin the safety
    # context at answer time — later question edits don't retroactively
    # redefine what an existing answer warned about.
    safety_category = models.CharField(
        _("Safety handling"), max_length=16, choices=SafetyCategory.choices,
        default=SafetyCategory.NONE, db_index=True,
    )
    safety_warning = models.TextField(_("Safety warning"), blank=True, default="")
    # WHY editable=True on purpose: it must keep showing up in every plain
    # serialization (model_to_dict, DRF ModelSerializer, forms' instance dump)
    # so future surfaces reading data CANNOT forget the badge. Nothing that
    # renders a form includes it — users cannot set it.
    is_machine = models.BooleanField(
        _("Machine-generated (AI) answer"), default=False, db_index=True
    )

    class Meta:
        ordering = ["created_at"]  # oldest first — a thread reads in order

    def __str__(self) -> str:
        return f"{self.author}: {self.body[:40]}"

    @property
    def risky(self) -> bool:
        """True when this answer must carry a safety warning (same scope as
        guides: medical, electrical or construction handling)."""
        return self.safety_category in RISKY_SAFETY_CATEGORIES

    @property
    def is_accepted(self) -> bool:
        return self.accepted_by_question_id is not None

    def clean(self):
        # Same model-level pattern as guides._safety_warning_missing, copied
        # locally on purpose (no private cross-app imports).
        if self.risky and not self.safety_warning.strip():
            raise ValidationError(
                {
                    "safety_warning": _(
                        "A safety warning is required for answers to medical, "
                        "electrical or construction questions."
                    )
                }
            )


class AnswerVote(TimestampedModel):
    """"I verified this answer works" — one row per member per answer.

    Reversible: record_vote creates/keeps one row (idempotent), remove_vote
    deletes it. Row existence is the vote — no counters to drift.
    """

    answer = models.ForeignKey(
        Answer, on_delete=models.CASCADE, related_name="votes"
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name="qa_answer_votes",
    )

    class Meta:
        ordering = ["created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["answer", "user"], name="uniq_qa_vote_per_user"
            )
        ]

    def __str__(self) -> str:
        return f"{self.user} verified answer {self.answer_id}"
