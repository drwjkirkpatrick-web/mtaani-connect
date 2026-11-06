"""Q&A services (Prompt 12) — one door for every write + list/search reads.

Provenance invariants (the Prompt 12 pitfalls, enforced HERE so every other
module inherits them):
- The AI badge is data-driven: is_machine is a boolean on the answer row and
  is set ONLY through set_machine_answer (Prompt 33's AI layer). Forms,
  views and serializers never expose it, so the badge can't be forgotten —
  any reader of the row sees the boolean.
- Accept gate (MEDICAL/ELECTRICAL questions, spec verbatim): an answer can
  only become the accepted answer while a HUMAN-written answer
  (is_machine=False) for the same question holds at least one community
  verification vote. Plain-language ValidationError otherwise.
"""
from __future__ import annotations

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Count, Q
from django.utils.translation import gettext_lazy as _

from apps.core.types import ContentStatus, SafetyCategory

from .models import ACCEPT_GATE_CATEGORIES, Answer, AnswerVote, Question


def visible_questions(for_user):
    """List-surface queryset. REMOVED questions are never listed (soft
    delete for moderation evidence); everyone sees the rest (publish-on-ask)."""
    base = Question.objects.exclude(status=ContentStatus.REMOVED.value)
    if getattr(for_user, "is_authenticated", False):
        return base.filter(
            Q(status=ContentStatus.PUBLISHED.value) | Q(asked_by=for_user)
        )
    return base.filter(status=ContentStatus.PUBLISHED.value)


def search_questions(queryset, *, q: str = ""):
    """Simple text search over question titles, question bodies and answer
    bodies (en + sw strings sit in the same columns, so bilingual matching
    comes free). NOTE: Prompt 14 replaces this with a maintained SearchVector
    + trigrams; this stays the public read-door until then."""
    term = (q or "").strip()
    if term:
        queryset = queryset.filter(
            Q(title_en__icontains=term)
            | Q(title_sw__icontains=term)
            | Q(body__icontains=term)
            | Q(answers__body__icontains=term)
        ).distinct()
    return queryset


def visible_answers(for_user):
    """Read-door for answer surfaces outside qa (Prompt 14 search). Only
    answers whose question is not REMOVED; anonymous users see only answers
    on PUBLISHED questions."""
    base = _answers_base()
    if getattr(for_user, "is_authenticated", False):
        return base.filter(
            Q(question__status=ContentStatus.PUBLISHED.value)
            | Q(question__asked_by=for_user)
        )
    return base.filter(question__status=ContentStatus.PUBLISHED.value)


def _answers_base():
    """Answers with joins preloaded (question + author) — internal helper."""
    return Answer.objects.select_related("question", "author").exclude(
        question__status=ContentStatus.REMOVED.value
    )


def ask_question(
    *,
    author,
    title_en: str,
    title_sw: str = "",
    body: str = "",
    safety_category: str = SafetyCategory.NONE.value,
) -> Question:
    """Ask a question and publish it immediately (publish-on-ask decision,
    mirrored from guides v1: the asker approves their own first content;
    moderation layers on top in Prompt 15)."""
    question = Question(
        title_en=title_en.strip(),
        title_sw=(title_sw or "").strip(),
        body=(body or "").strip(),
        safety_category=safety_category,
        asked_by=author,
        status=ContentStatus.PUBLISHED.value,
    )
    question.full_clean()  # model gates, not just the form (PROMPTS.md pitfall)
    question.save()
    return question


def post_answer(
    *, question: Question, author, body: str, safety_warning: str = ""
) -> Answer:
    """Post one answer. The question's safety flag applies to the answer, so
    risky questions REQUIRE a safety_warning in the answer (model clean())."""
    answer = Answer(
        question=question,
        author=author,
        body=(body or "").strip(),
        # Snapshot the question's safety flag onto the answer row (see
        # models WHY note): the answer's gate is self-contained afterwards.
        safety_category=question.safety_category,
        safety_warning=(safety_warning or "").strip(),
        # NOTE: is_machine is NOT a parameter on purpose — service signatures
        # are the last line before a call site accidentally "creates an AI
        # answer". Prompt 33 must post an answer, then call set_machine_answer.
        is_machine=False,
    )
    answer.full_clean()
    answer.save()
    return answer


def set_machine_answer(answer: Answer, *, machine: bool = True) -> Answer:
    """THE ONLY way to set the AI label (Prompt 33's AI layer call).

    WHY queryset.update(): it writes the column deterministically without
    running the ordinary save()/clean() path that user edits go through, so
    this doorway and user surfaces can never collide. Human review note:
    staff can still correct a wrongly-set label in the admin afterwards.
    """
    Answer.objects.filter(pk=answer.pk).update(is_machine=machine)
    answer.refresh_from_db(fields=["is_machine"])
    return answer


def _verified_human_answer_exists(question: Question) -> bool:
    """A HUMAN answer (is_machine False) for this question holding at least
    one community verification vote. Thread-level check per Prompt 12:
    "never the accepted answer WITHOUT a human-verified community answer
    alongside" — so a verified human candidate satisfies it itself, while a
    machine candidate (excluded by is_machine=False) needs another one."""
    return (
        Answer.objects.filter(
            question=question,
            is_machine=False,
        )
        .annotate(verified_count=Count("votes"))
        .filter(verified_count__gte=1)
        .exists()
    )


def accept_answer(*, question: Question, answer: Answer, by_user) -> Question:
    """Asker (or staff) picks the answer that solved the question — subject
    to the safety gate for MEDICAL/ELECTRICAL questions (see module docstring;
    scope copied verbatim from Prompt 12, not from guides)."""
    if by_user != question.asked_by and not by_user.is_staff:
        raise PermissionDenied("Only the asker can accept an answer.")
    if answer.question_id != question.pk:
        raise ValidationError(_("That answer does not belong to this question."))
    if question.safety_category in ACCEPT_GATE_CATEGORIES:
        if not _verified_human_answer_exists(question):
            if answer.is_machine:
                hint = _(
                    "This AI answer cannot become the accepted answer until a "
                    "human-written answer for this question has been verified "
                    "by the community."
                )
            else:
                hint = _(
                    "This is a medical or electrical question: an answer can "
                    "only be accepted after a human-written answer has been "
                    "verified by the community."
                )
            raise ValidationError(hint)
    question.accepted_answer = answer
    question.full_clean()  # e.g. accepted answer must belong to this question
    question.save(update_fields=["accepted_answer", "updated_at"])
    return question


def record_vote(*, answer: Answer, user):
    """Verify an answer. One row per (answer, user) — voting again is an
    idempotent no-op, not a stacked vote."""
    if user == answer.author:
        raise ValidationError(
            _("You cannot verify your own answer — wait for the community.")
        )
    vote, created = AnswerVote.objects.get_or_create(answer=answer, user=user)
    return vote, created


def remove_vote(*, answer: Answer, user) -> bool:
    """Un-verify (vote reversal). Deleting a missing row is a harmless no-op."""
    deleted, _ = AnswerVote.objects.filter(answer=answer, user=user).delete()
    return deleted > 0
