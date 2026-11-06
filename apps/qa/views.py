"""Q&A views (Prompt 12) — server-rendered, templates in apps/qa/templates/qa/.

Read routes (list/detail) are public for published questions; every write
route (ask, answer, accept, vote) is login_required. The accept route is
further gated to the asker inside the service (asker-accepted semantics).

BADGE CONTRACT: the detail template renders the AI badge from the row's
is_machine boolean and NOTHING else (data-driven — see models docstring).
"""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db.models import Count
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from apps.core.types import SafetyCategory

from . import services
from .forms import AnswerForm, QuestionForm
from .models import Answer, Question

# HARD RULE (PROMPTS.md budgets): a list surface shows at most 20 items per
# page — keeps the page inside the 50 KB 3G budget. The detail page applies
# the same cap to answers on one thread.
PAGE_SIZE = 20


def _show_validation_error(request, exc: ValidationError) -> None:
    """Flatten a ValidationError into one human message (field-dict or bare)."""
    messages.error(request, " ".join(getattr(exc, "messages", None) or [str(exc)]))


def question_list(request):
    """/qa/ — published questions, newest first, with free GET search
    (?q=) and safety-category filter (?category=)."""
    q = request.GET.get("q", "").strip()
    category = request.GET.get("category", "")
    listing = services.search_questions(services.visible_questions(request.user), q=q)
    if category:
        listing = listing.filter(safety_category=category)
    # annotate the per-question answer count AND order explicitly — a paginator
    # needs a deterministic order (Meta ordering is not enough after annotate)
    listing = listing.annotate(answer_count=Count("answers")).order_by("-created_at")
    page_obj = Paginator(listing, PAGE_SIZE).get_page(request.GET.get("page"))
    return render(
        request,
        "qa/list.html",
        {
            "questions": page_obj.object_list,
            "page_obj": page_obj,
            "q": q,
            "active_category": category,
            "categories": SafetyCategory.choices,
        },
    )


def question_detail(request, pk):
    """One thread: question + up to 20 answers with provenance labels —
    accepted badge, AI badge (from is_machine), verification count/vote state."""
    question = get_object_or_404(services.visible_questions(request.user), pk=pk)
    accepted = question.accepted_answer
    answers = list(
        question.answers.annotate(vote_count=Count("votes")).order_by("created_at")
    )
    # Sort in Python for a deterministic provenance order: accepted answer
    # first, then most-verified, then oldest. Django F() conditions can't
    # express "the OneToOne row first" as crisply as three key clauses.
    answers.sort(
        key=lambda a: (
            0 if accepted is not None and a.pk == accepted.pk else 1,
            -a.vote_count,
            a.created_at,
        )
    )
    answers = answers[:PAGE_SIZE]
    my_votes: set[int] = set()
    if request.user.is_authenticated:
        my_votes = set(
            request.user.qa_answer_votes.filter(answer__question=question)
            .values_list("answer_id", flat=True)
        )
    # Prompt 16 parent wiring: each answer's author carries their earned
    # badges next to the answer (public service; memo per unique author).
    from apps.trust.services import helpfulness_badges
    badges_by_author: dict[int, list] = {}
    for a in answers:
        if a.author_id not in badges_by_author:
            badges_by_author[a.author_id] = helpfulness_badges(a.author)
        # NOTE: display-only dynamic attr — nothing is persisted on Answer.
        a.trust_badges = badges_by_author[a.author_id]
    return render(
        request,
        "qa/detail.html",
        {
            "question": question,
            "answers": answers,
            "accepted_pk": accepted.pk if accepted is not None else None,
            "my_votes": my_votes,
            "can_accept": (
                request.user.is_authenticated
                and request.user.pk == question.asked_by_id
            ),
        },
    )


@login_required
def question_ask(request):
    """Ask a question (publish-on-ask — see services.ask_question)."""
    if request.method == "POST":
        form = QuestionForm(request.POST)
        if form.is_valid():
            try:
                question = services.ask_question(
                    author=request.user, **form.cleaned_data
                )
            except ValidationError as exc:
                _show_validation_error(request, exc)
            else:
                messages.success(request, _("Question posted — the community can now answer."))
                return redirect("qa:detail", pk=question.pk)
        # invalid input falls through to the re-render with inline errors
    else:
        form = QuestionForm()
    return render(request, "qa/ask.html", {"form": form})


@login_required
def answer_question(request, pk):
    """Answer a question. Risky questions show the required-safety-warning
    field; the model clean() (via the service) enforces the warning anyway."""
    question = get_object_or_404(services.visible_questions(request.user), pk=pk)
    if request.method == "POST":
        form = AnswerForm(request.POST)
        if form.is_valid():
            try:
                services.post_answer(
                    question=question,
                    author=request.user,
                    body=form.cleaned_data["body"],
                    safety_warning=form.cleaned_data.get("safety_warning", ""),
                )
            except ValidationError as exc:
                _show_validation_error(request, exc)
            else:
                messages.success(request, _("Answer posted — thank you for helping."))
                return redirect("qa:detail", pk=question.pk)
        # invalid input falls through to the re-render with inline errors
    else:
        form = AnswerForm()
    return render(
        request, "qa/answer_form.html", {"question": question, "form": form}
    )


@login_required
@require_POST
def accept_answer(request, q_pk, a_pk):
    """Asker-only (or staff) POST: accept an answer; the service applies the
    MEDICAL/ELECTRICAL human-verified-evidence gate with plain-language copy."""
    question = get_object_or_404(
        Question.objects.exclude(status="REMOVED"), pk=q_pk
    )
    answer = get_object_or_404(Answer.objects, pk=a_pk, question=question)
    try:
        services.accept_answer(question=question, answer=answer, by_user=request.user)
    except ValidationError as exc:
        _show_validation_error(request, exc)
    return redirect("qa:detail", pk=question.pk)


@login_required
@require_POST
def answer_vote(request, pk):
    """Verify/un-verify an answer: one vote row per member per answer.
    POST with undo=1 removes the vote; without it, create-or-keep (idempotent)."""
    answer = get_object_or_404(services.visible_answers(request.user), pk=pk)
    if request.POST.get("undo"):
        if services.remove_vote(answer=answer, user=request.user):
            messages.success(request, _("Verification removed."))
    else:
        try:
            services.record_vote(answer=answer, user=request.user)
        except ValidationError as exc:
            _show_validation_error(request, exc)  # e.g. verifying your own answer
        else:
            messages.success(request, _("Answer marked as verified."))
    return redirect("qa:detail", pk=answer.question_id)
