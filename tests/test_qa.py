"""Prompt 12 acceptance: ask/answer round-trips, asker-only accept with the
MEDICAL/ELECTRICAL human-verified gate, idempotent reversible verification
votes, an AI label that is DATA-DRIVEN (boolean on the row, survives every
plain serialization and re-save, unreachable from forms), safety warnings
mirroring guides, and page budgets.

The model/service tests are the REAL gate: safety and provenance rules live
on the models and services, not just the forms (PROMPTS.md pitfall), so any
future write path (admin, API, script) inherits them automatically.
"""
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.forms.models import model_to_dict
from django.test import Client

import pytest

from apps.core.testing import assert_under_budget
from apps.core.types import ContentStatus, SafetyCategory
from apps.qa import services
from apps.qa.forms import AnswerForm, QuestionForm
from apps.qa.models import Answer, Question

AI_BADGE = "AI answer — verify before relying on it"
ACCEPTED_BADGE = "Accepted answer"

QUESTION = {
    "title_en": "How do I safely wire a cooker switch?",
    "title_sw": "",
    "body": "",
    "safety_category": SafetyCategory.NONE.value,
}


@pytest.fixture
def other_user(db):
    User = get_user_model()
    return User.objects.create_user(
        username="amina", email="amina@example.com", password="***"
    )


@pytest.fixture
def third_user(db):
    User = get_user_model()
    return User.objects.create_user(
        username="zawadi", email="zawadi@example.com", password="***"
    )


@pytest.fixture
def question(fresh_user):
    return services.ask_question(author=fresh_user, **QUESTION)


# ------------------------------------------------------------------ ask round-trip


@pytest.mark.django_db
def test_ask_round_trip_publishes_and_lists(client_auth):
    response = client_auth.post(
        "/qa/ask/",
        {
            "title_en": "Where do I buy seedlings?",
            "title_sw": "",
            "body": "",
            "safety_category": "NONE",
        },
    )
    assert response.status_code == 302
    question = Question.objects.get()
    # NOTE: publish-on-ask — no DRAFT step for Q&A (Prompt 12 keeps it simple).
    assert question.status == ContentStatus.PUBLISHED.value
    assert question.asked_by_id == response.wsgi_request.user.pk
    assert b"Where do I buy seedlings?" in client_auth.get("/qa/").content


@pytest.mark.django_db
def test_ask_requires_login(client):
    first = client.get("/qa/ask/")
    assert first.status_code == 302 and "/accounts/login" in first.url
    response = client.post(
        "/qa/ask/",
        {"title_en": "x", "title_sw": "", "body": "", "safety_category": "NONE"},
    )
    assert response.status_code == 302
    assert Question.objects.count() == 0


@pytest.mark.django_db
def test_question_body_cap_is_a_model_gate(fresh_user):
    # WHY service-level expectation: the cap must hold for admin/API paths too.
    with pytest.raises(ValidationError):
        services.ask_question(
            author=fresh_user, title_en="t", body="x" * 2001  # one over the cap
        )


@pytest.mark.django_db
def test_question_prefers_kiswahili_fallback(fresh_user):
    en_only = services.ask_question(author=fresh_user, **QUESTION)
    assert en_only.display_title == "How do I safely wire a cooker switch?"
    bilingual = services.ask_question(
        author=fresh_user,
        title_en="Cook ugali",
        title_sw="Unapikaje ugali?",
        safety_category=SafetyCategory.NONE.value,
    )
    assert bilingual.display_title == "Unapikaje ugali?"
    assert en_only.title_is_fallback and not bilingual.title_is_fallback


# ------------------------------------------------------------------ answers


@pytest.mark.django_db
def test_answer_round_trip_renders_on_detail(client_auth, question):
    assert client_auth.get(f"/qa/{question.pk}/").status_code == 200
    response = client_auth.post(f"/qa/{question.pk}/answer/", {"body": "Answer body."})
    assert response.status_code == 302
    answer = Answer.objects.get()
    assert answer.question_id == question.pk
    assert answer.is_machine is False  # user answers are never AI-labeled
    assert b"Answer body." in client_auth.get(f"/qa/{question.pk}/").content


@pytest.mark.django_db
def test_answer_requires_login(client, question):
    assert client.get(f"/qa/{question.pk}/answer/").status_code == 302
    assert client.post(f"/qa/{question.pk}/answer/", {"body": "hi"}).status_code == 302
    assert Answer.objects.count() == 0


@pytest.mark.django_db
def test_answer_body_cap(fresh_user, other_user, question):
    with pytest.raises(ValidationError):
        services.post_answer(question=question, author=other_user, body="x" * 2001)


@pytest.mark.django_db
def test_multiple_answers_ordered_oldest_first(
    fresh_user, other_user, third_user, question
):
    first = services.post_answer(question=question, author=other_user, body="First.")
    second = services.post_answer(question=question, author=third_user, body="Second.")
    assert list(question.answers.all()) == [first, second]


# ------------------------------------------------------------------ safety gating


@pytest.mark.django_db
@pytest.mark.parametrize("category", [
    SafetyCategory.MEDICAL.value,
    SafetyCategory.ELECTRICAL.value,
    SafetyCategory.CONSTRUCTION.value,
])
def test_risky_questions_require_answer_safety_warning(fresh_user, other_user, category):
    question = services.ask_question(
        author=fresh_user, title_en="q", safety_category=category
    )
    with pytest.raises(ValidationError):
        services.post_answer(
            question=question, author=other_user, body="Just do it.", safety_warning=""
        )
    answer = services.post_answer(
        question=question,
        author=other_user,
        body="Do it slowly.",
        safety_warning="Consult a certified technician first.",
    )
    assert answer.risky and answer.safety_category == category


@pytest.mark.django_db
@pytest.mark.parametrize("category", [
    SafetyCategory.MEDICAL.value,
    SafetyCategory.ELECTRICAL.value,
])
def test_accept_gate_machine_vs_human(fresh_user, other_user, third_user, category):
    """Spec verbatim: medical/electrical answers can never be THE accepted
    answer without a human-verified community answer alongside."""
    question = services.ask_question(
        author=fresh_user, title_en="q", safety_category=category
    )
    human = services.post_answer(
        question=question,
        author=other_user,
        body="Test with a screwdriver first.",
        safety_warning="Wear rubber gloves.",
    )
    machine = services.post_answer(
        question=question,
        author=other_user,
        body="Replace the whole board.",
        safety_warning="Main power off.",
    )
    services.set_machine_answer(machine)  # Prompt 33's doorway

    # 1) machine-only thread: acceptance refused
    with pytest.raises(ValidationError):
        services.accept_answer(question=question, answer=machine, by_user=fresh_user)
    question.refresh_from_db()
    assert question.accepted_answer is None

    # 2) a HUMAN answer exists but nobody verified it yet: still refused
    with pytest.raises(ValidationError):
        services.accept_answer(question=question, answer=human, by_user=fresh_user)
    question.refresh_from_db()
    assert question.accepted_answer is None

    # 3) community verifies the human answer -> acceptance now allowed
    services.record_vote(answer=human, user=third_user)
    services.accept_answer(question=question, answer=human, by_user=fresh_user)
    question.refresh_from_db()
    assert question.accepted_answer_id == human.pk

    # 4) the machine answer is acceptable only WITH the verified human alongside
    services.accept_answer(question=question, answer=machine, by_user=fresh_user)
    question.refresh_from_db()
    assert question.accepted_answer_id == machine.pk


@pytest.mark.django_db
def test_construction_accepts_without_community_answer(fresh_user, other_user):
    question = services.ask_question(
        author=fresh_user, title_en="q", safety_category=SafetyCategory.CONSTRUCTION.value
    )
    machine = services.post_answer(
        question=question, author=other_user, body="hmm.", safety_warning="Helmet on."
    )
    services.set_machine_answer(machine)
    # Prompt 12 scopes the accept gate to MEDICAL/ELECTRICAL — construction
    # answers need warnings (above) but are not community-answer-gated.
    services.accept_answer(question=question, answer=machine, by_user=fresh_user)
    question.refresh_from_db()
    assert question.accepted_answer_id == machine.pk


@pytest.mark.django_db
def test_accept_only_by_asker_or_staff(fresh_user, other_user, third_user):
    question = services.ask_question(author=fresh_user, title_en="q")
    answer = services.post_answer(question=question, author=other_user, body="b")
    with pytest.raises(PermissionDenied):
        services.accept_answer(question=question, answer=answer, by_user=other_user)
    with pytest.raises(PermissionDenied):
        services.accept_answer(question=question, answer=answer, by_user=third_user)
    # staff may act for the asker (moderation hooks, Prompt 15) — mirrors guides
    staff = get_user_model().objects.create_user(
        username="mod", email="m@example.com", password="***"
    )
    staff.is_staff = True
    staff.save()
    services.accept_answer(question=question, answer=answer, by_user=staff)


@pytest.mark.django_db
def test_accept_rejects_foreign_answer(fresh_user, other_user):
    q1 = services.ask_question(author=fresh_user, title_en="q1")
    q2 = services.ask_question(author=other_user, title_en="q2")
    stray = services.post_answer(question=q2, author=other_user, body="b")
    with pytest.raises(ValidationError):
        services.accept_answer(question=q1, answer=stray, by_user=fresh_user)


@pytest.mark.django_db
def test_accept_view_rejects_answer_of_another_thread(client_auth, question, other_user):
    stray_q = services.ask_question(author=other_user, title_en="q2")
    stray = services.post_answer(question=stray_q, author=other_user, body="b")
    assert client_auth.post(f"/qa/{question.pk}/accept/{stray.pk}/").status_code == 404
    question.refresh_from_db()
    assert question.accepted_answer is None


@pytest.mark.django_db
def test_accept_view_403_for_non_asker(question, other_user):
    answer = services.post_answer(
        question=question, author=other_user, body="b"
    )
    client = Client()
    client.force_login(other_user)
    assert client.post(f"/qa/{question.pk}/accept/{answer.pk}/").status_code == 403
    question.refresh_from_db()
    assert question.accepted_answer is None


@pytest.mark.django_db
def test_accept_view_shows_plain_gate_message(client_auth, fresh_user, other_user, third_user):
    question = services.ask_question(
        author=fresh_user, title_en="Medical q", safety_category=SafetyCategory.MEDICAL.value
    )
    machine = services.post_answer(
        question=question, author=other_user, body="AI draft.", safety_warning="Ask a clinician."
    )
    services.set_machine_answer(machine)
    response = client_auth.post(
        f"/qa/{question.pk}/accept/{machine.pk}/", follow=True
    )
    # the refusal explains itself in plain language to the asker
    assert "human-written answer" in response.content.decode()
    question.refresh_from_db()
    assert question.accepted_answer is None


# ------------------------------------------------------------------ verification votes


@pytest.mark.django_db
def test_vote_one_per_user_idempotent(client_auth, question, other_user, third_user):
    answer = services.post_answer(question=question, author=other_user, body="b")
    assert client_auth.post(f"/qa/answers/{answer.pk}/vote/", {}).status_code == 302
    client_auth.post(f"/qa/answers/{answer.pk}/vote/", {})
    # voting again (HTTP or service, race-safe get_or_create) never stacks rows
    assert answer.votes.count() == 1
    vote, created = services.record_vote(answer=answer, user=third_user)
    assert created is True and answer.votes.count() == 2
    vote, created = services.record_vote(answer=answer, user=third_user)
    assert created is False and answer.votes.count() == 2


@pytest.mark.django_db
def test_vote_requires_login_and_blocks_self(client_auth, fresh_user, question):
    answer = services.post_answer(question=question, author=fresh_user, body="mine")
    response = Client().post(f"/qa/answers/{answer.pk}/vote/", {})
    assert response.status_code == 302 and "/accounts/login" in response.url
    assert answer.votes.count() == 0
    # the author cannot verify their own answer (service + HTTP path)
    with pytest.raises(ValidationError):
        services.record_vote(answer=answer, user=fresh_user)
    page = client_auth.post(f"/qa/answers/{answer.pk}/vote/", {}, follow=True)
    assert "cannot verify your own answer" in page.content.decode()
    assert answer.votes.count() == 0


@pytest.mark.django_db
def test_vote_is_reversible(client_auth, question, other_user):
    answer = services.post_answer(question=question, author=other_user, body="b")
    client_auth.post(f"/qa/answers/{answer.pk}/vote/", {})
    vote = answer.votes.first()
    assert vote is not None
    assert client_auth.post(f"/qa/answers/{answer.pk}/vote/", {"undo": "1"}).status_code == 302
    assert answer.votes.count() == 0
    # removing an already-removed vote is a harmless no-op -> False
    assert services.remove_vote(answer=answer, user=vote.user) is False


@pytest.mark.django_db
def test_vote_reversal_via_service(client_auth, question, other_user, third_user):
    answer = services.post_answer(question=question, author=other_user, body="b")
    services.record_vote(answer=answer, user=third_user)
    assert services.remove_vote(answer=answer, user=third_user) is True
    assert answer.votes.count() == 0


# ------------------------------------------------------------------ AI badge (is_machine)


@pytest.mark.django_db
def test_ai_flag_survives_resave(question, other_user):
    answer = services.post_answer(
        question=question, author=other_user, body="Drafted by the AI layer."
    )
    services.set_machine_answer(answer)
    assert answer.is_machine is True
    # ordinary edits (full_clean + save after user changes) never drop the label
    answer.body = "Edited wording after review."
    answer.full_clean()
    answer.save()
    answer.refresh_from_db()
    assert answer.is_machine is True
    assert Answer.objects.get(pk=answer.pk).is_machine is True


@pytest.mark.django_db
def test_ai_flag_survives_plain_serialization(question, other_user):
    answer = services.post_answer(question=question, author=other_user, body="b")
    services.set_machine_answer(answer)
    dumped = model_to_dict(answer)
    assert dumped["is_machine"] is True  # data-driven: any serializer keeps it
    human = services.post_answer(question=question, author=other_user, body="b2")
    assert model_to_dict(human)["is_machine"] is False


def test_ai_flag_is_absent_from_every_form():
    # the PITFALL contract: no qa form exposes is_machine to users
    assert "is_machine" not in QuestionForm().fields
    assert "is_machine" not in AnswerForm().fields
    # even a hostile payload cannot smuggle the label in through the form
    hostile = AnswerForm({"body": "x", "is_machine": "True"})
    assert hostile.is_valid()
    assert hostile.save(commit=False).is_machine is False


@pytest.mark.django_db
def test_form_driven_answer_cannot_set_machine_label(client_auth, question):
    response = client_auth.post(
        f"/qa/{question.pk}/answer/",
        {"body": "Looks like a blown fuse.", "is_machine": "True"},
    )
    assert response.status_code == 302
    assert Answer.objects.get().is_machine is False


# ------------------------------------------------------------------ provenance rendering


@pytest.mark.django_db
def test_detail_renders_provenance_labels(client_auth, fresh_user, other_user, third_user):
    question = services.ask_question(
        author=fresh_user,
        title_en="Is this swelling normal?",
        safety_category=SafetyCategory.MEDICAL.value,
    )
    human = services.post_answer(
        question=question,
        author=other_user,
        body="See a clinic near you today.",
        safety_warning="This is general information, not a diagnosis.",
    )
    machine = services.post_answer(
        question=question,
        author=other_user,
        body="AI drafted text.",
        safety_warning="AI cannot see you — confirm with a provider.",
    )
    services.set_machine_answer(machine)
    services.record_vote(answer=human, user=third_user)
    services.accept_answer(question=question, answer=human, by_user=fresh_user)

    page = client_auth.get(f"/qa/{question.pk}/")
    html = page.content.decode()
    assert ACCEPTED_BADGE in html and page.context["accepted_pk"] == human.pk
    assert AI_BADGE in html  # rendered only from the is_machine boolean
    # provenance order: the accepted (verified) human answer comes first
    assert html.index("See a clinic") < html.index("AI drafted text")
    assert "1 community verification" in html


@pytest.mark.django_db
def test_detail_ai_badge_absent_for_human_answers(client_auth, question, other_user):
    services.post_answer(question=question, author=other_user, body="Human writes this.")
    html = client_auth.get(f"/qa/{question.pk}/").content.decode()
    assert AI_BADGE not in html  # no convention can mislabel a human answer


# ------------------------------------------------------------------ lists, budget, visibility


@pytest.mark.django_db
def test_list_pagination_caps_at_20(client_auth, fresh_user):
    for i in range(21):
        services.ask_question(author=fresh_user, title_en=f"Question {i:02d}")
    first = client_auth.get("/qa/")
    assert first.context["page_obj"].paginator.count == 21
    assert len(first.context["questions"]) == 20
    assert len(client_auth.get("/qa/?page=2").context["questions"]) == 1


@pytest.mark.django_db
def test_removed_question_is_hidden_from_everyone(client_auth, question):
    question.status = ContentStatus.REMOVED.value
    question.save()
    assert services.visible_questions(None).count() == 0
    assert b"safely wire a cooker" not in client_auth.get("/qa/").content
    assert client_auth.get(f"/qa/{question.pk}/").status_code == 404


@pytest.mark.django_db
def test_search_questions_finds_en_sw_and_answer_text(fresh_user, other_user):
    en = services.ask_question(author=fresh_user, title_en="Growing sukuma wiki")
    sw = services.ask_question(author=fresh_user, title_en="t2", title_sw="Uhuru wa soko")
    answered = services.ask_question(author=fresh_user, title_en="t3")
    services.post_answer(question=answered, author=other_user, body="Unga wa wimbi hapa")
    found_en = services.search_questions(services.visible_questions(None), q="sukuma")
    found_sw = services.search_questions(services.visible_questions(None), q="soko")
    found_answer = services.search_questions(services.visible_questions(None), q="wimbi")
    assert en in found_en and sw in found_sw and answered in found_answer
    assert found_en.count() == found_sw.count() == found_answer.count() == 1


@pytest.mark.django_db
def test_pages_under_budget(client_auth, fresh_user, other_user, third_user):
    question = services.ask_question(author=fresh_user, **QUESTION)
    for i in range(5):
        answer = services.post_answer(
            question=question, author=other_user, body=f"Advice {i}. " * 20
        )
        services.record_vote(answer=answer, user=third_user)
    assert_under_budget(client_auth.get("/qa/ask/").content.decode(), kb=50)
    assert_under_budget(client_auth.get("/qa/").content.decode(), kb=50)
    detail = client_auth.get(f"/qa/{question.pk}/")
    assert detail.status_code == 200
    assert_under_budget(detail.content.decode(), kb=50)
