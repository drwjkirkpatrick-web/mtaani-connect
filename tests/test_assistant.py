"""Prompt 34 acceptance tests: the "Ask Mtaani" local-adaptation assistant.

The spec bullet list verbatim: adaptation output cites guides, the badge
contract (Prompt 12), safety-gate append, quota enforcement (per user,
brief §8), retrieval determinism with a fixture corpus. Also pinned: the
brief's acceptance flow ("I have a sufuria, a jiko, and ughali flour"
produces an adapted, cited, labelled plan), honest degradation (quota /
unavailable states render 200 with honest copy and NO AI content — hence
no badge), exactly ONE ledger row (purpose 'assistant-adaptation') + ONE
AssistantRun sidecar row per successful run, and NOTHING written at all
for refused or unavailable attempts.

Conventions: injected clock (apps.core.testing.mock_now) for day math;
fixtures seeded through MockLLM.fixture_key — Prompt 33's sha256 seam is
PUBLIC (systems+messages canonicalization, no clock, no RNG).
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from django.core.management import call_command
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from apps.ai import assistant, ledger, prompts, providers, retrieval
from apps.ai.labeling import AI_BADGE_CLASS, ai_label
from apps.ai.models import AICall, AssistantRun
from apps.core.testing import assert_under_budget, mock_now
from apps.core.types import SafetyCategory
from apps.guides import services as guide_services
from apps.guides.models import RISKY_SAFETY_CATEGORIES

# Injected-clock anchor (2026-01-15 09:30 UTC; Africa/Nairobi is UTC+3 —
# unambiguous calendar day, mirrors test_ai_layer.py).
TODAY = mock_now()
TOMORROW = TODAY + timedelta(days=1)

HAVE = "I have a sufuria, a jiko, and ughali flour"
NEED = "cook ugali"
ADAPTED = (
    'Plan adapted from "Cook ugali on a jiko": heat water in the sufuria '
    "on the jiko, then stir in the ugali flour until it firms up."
)
MEDICAL_WARNING = (
    "Do not take any medicine before a qualified clinician confirms the "
    "wound is clean and the bleeding has stopped."
)

# A PURE fixture corpus (retrieval.py's documented row contract) — no DB.
FIXTURE_CORPUS = [
    {
        "pk": 1, "title": "Cook ugali on a jiko", "category": "cooking",
        "keywords": "sufuria jiko ugali maize flour", "tags": "",
        "safety_category": "NONE", "safety_warning": "",
    },
    {
        "pk": 2, "title": "Planting sukuma wiki in a sack", "category": "farming",
        "keywords": "sack seedlings manure water", "tags": "",
        "safety_category": "NONE", "safety_warning": "",
    },
    {
        "pk": 3, "title": "First aid for a cut hand", "category": "health-education",
        "keywords": "wound bandage clean bleed", "tags": "MEDICAL",
        "safety_category": "MEDICAL", "safety_warning": MEDICAL_WARNING,
    },
]


# ----------------------------------------------------------------- helpers
def make_guide(
    user,
    *,
    title_en: str,
    category: str,
    steps: list[str],
    safety: str = SafetyCategory.NONE.value,
    warning: str = "",
):
    """Seed one guide through the guides PUBLIC write door (services)."""
    return guide_services.create_guide(
        author=user,
        title_en=title_en,
        category=category,
        skill_level="beginner",
        safety_category=safety,
        safety_warning=warning,
        steps=[{"position": n, "text": text} for n, text in enumerate(steps, start=1)],
    )


def corpus_top(user, have_text: str, need_text: str):
    """Mirror ask()'s retrieval exactly: same public door, same pure fns.
    Returns (index, hits, cited_rows) — cited_rows are the RAW index rows
    (what ask() composes messages from)."""
    index = retrieval.build_index(assistant.corpus_rows(user))
    hits = retrieval.retrieve(
        index,
        assistant.query_text(have_text, need_text),
        limit=assistant.MAX_CITED_GUIDES,
    )
    return index, hits, [index["rows"][pk] for pk, _score, _cat in hits]


def seeded_provider(cited_rows, *, reply: str = ADAPTED) -> providers.MockLLM:
    """A fixture-keyed MockLLM for THIS exact intake+corpus: the key comes
    from providers.MockLLM.fixture_key (the sha256 seam) so the run hits
    the fixture deterministically — no clock, no RNG."""
    messages = assistant.compose_intake_message(HAVE, NEED, cited_rows)
    key = providers.MockLLM.fixture_key(
        prompts.prompt_text(assistant.ASSISTANT_PROMPT_VERSION), messages
    )
    return providers.MockLLM({key: reply})


def seed_runs(user, count: int, *, at=None) -> None:
    """Pin ``count`` quota rows against the member (honest empties)."""
    for _ in range(count):
        row = AssistantRun.objects.create(user=user, cited=[])
        if at is not None:
            AssistantRun.objects.filter(pk=row.pk).update(created_at=at)


def seed_tokens(tokens: int, at=TODAY) -> None:
    """Put tokens into TODAY'S ledger (mirror of test_ai_layer's helper)."""
    row = ledger.log_raw(
        provider="seed", model="seed", tokens_in=tokens, purpose="seed"
    )
    AICall.objects.filter(pk=row.pk).update(created_at=at)


@pytest.fixture
def member(django_user_model):
    return django_user_model.objects.create_user(
        username="wanjiku34", email="wanjiku34@example.ke", password="***"
    )


@pytest.fixture
def stranger(django_user_model):
    return django_user_model.objects.create_user(
        username="mjue34", email="mjue34@example.ke", password="***"
    )


@pytest.fixture
def client_member(client, member):
    client.force_login(member)
    return client


@pytest.fixture
def ugali_guide(member):
    return make_guide(
        member,
        title_en="Cook ugali on a jiko",
        category="cooking",
        steps=[
            "Heat clean water in the sufuria on the jiko.",
            "Add ugali flour slowly, stirring as it thickens.",
        ],
    )


# ---------------------------------------------- retrieval determinism (pure)
def test_retrieval_determinism_same_corpus_query_twice():
    """THE determinism test (fixture corpus): same corpus + query → the
    IDENTICAL top-k — across freshly built indexes and repeat calls."""
    query = "I have a sufuria, a jiko, and ughali flour"
    first = retrieval.retrieve(retrieval.build_index(FIXTURE_CORPUS), query, limit=3)
    second = retrieval.retrieve(retrieval.build_index(FIXTURE_CORPUS), query, limit=3)
    third = retrieval.retrieve(retrieval.build_index(FIXTURE_CORPUS), query, limit=3)
    assert first == second == third
    assert first and first[0][0] == 1  # the ugali/jiko guide tops the k


def test_retrieval_scores_tokens_plus_category_bonus():
    """Overlap scores rank ties by category match (no embeddings, no net)."""
    corpus = [
        {"pk": 10, "title": "Watering the shamba", "category": "farming",
         "keywords": "pump hose water", "tags": "", "safety_category": "NONE",
         "safety_warning": ""},
        {"pk": 11, "title": "Pump care", "category": "transport",
         "keywords": "pump hose", "tags": "", "safety_category": "NONE",
         "safety_warning": ""},
    ]
    hits = retrieval.retrieve(
        retrieval.build_index(corpus), "pump hose farming", limit=2
    )
    assert [hit[0] for hit in hits] == [10, 11]  # category bonus orders the tie
    assert hits[0][1] - hits[1][1] == retrieval.CATEGORY_MATCH_BONUS
    assert (hits[0][2], hits[1][2]) == ("farming", "transport")


def test_retrieval_empty_query_and_unknown_terms_return_nothing():
    index = retrieval.build_index(FIXTURE_CORPUS)
    assert retrieval.retrieve(index, "", limit=3) == []
    assert retrieval.retrieve(index, "zzz qqq", limit=3) == []


# -------------------------------------------------- the safety gate (pure)
def test_gated_categories_import_the_guides_enum_not_a_copy():
    """MEDICAL/ELECTRICAL/CONSTRUCTION — IMPORTED from apps.core.types
    (frozen vocabulary), and mirroring guides' risky set exactly."""
    assert assistant.GATED_CATEGORIES == (
        SafetyCategory.MEDICAL.value,
        SafetyCategory.ELECTRICAL.value,
        SafetyCategory.CONSTRUCTION.value,
    )
    assert set(assistant.GATED_CATEGORIES) == set(RISKY_SAFETY_CATEGORIES)


def test_safety_gate_appends_guide_warning_verbatim():
    """Gated category ALWAYS appends the guide's own safety_warning
    VERBATIM — and the gate never modifies the draft."""
    warning = "NEVER touch a live conductor — switch the main switch off first."
    payload = assistant.assistant_payload(
        category=SafetyCategory.ELECTRICAL.value,
        guide_warnings=[("Rural house wiring fix", warning)],
        draft_text="A plainly-worded draft.",
    )
    assert payload["gated"] is True
    assert warning in payload["safety_block"]  # verbatim, untouched
    assert payload["draft"] == "A plainly-worded draft."  # draft never replaced


def test_safety_gate_ungated_has_no_block():
    payload = assistant.assistant_payload(
        category="cooking",
        guide_warnings=[("Cook ugali on a jiko", "")],
        draft_text="d",
    )
    assert payload["gated"] is False
    assert payload["safety_block"] == ""
    assert payload["draft"] == "d"


def test_v2_prompt_carries_the_strict_guardrail_paragraph():
    """The versioned file carries the strict paragraph the gated topics
    route through (version-pinned header, Prompt 33's loader)."""
    text = prompts.prompt_text("assistant-v2")
    assert text.startswith(
        "<!-- mtg-prompt | name: assistant-v2 | version: 2 | purpose:"
    )
    for keyword in (
        "MEDICAL", "ELECTRICAL", "CONSTRUCTION", "verbatim", "never overrides",
    ):
        assert keyword in text
    assert "safety warning" in text
    assert "qualified" in text


# ------------------------------- the ask() door: ledger + sidecar + cites
@pytest.mark.django_db
def test_run_cites_retrieval_topk_and_writes_one_row_each(member, ugali_guide):
    """ONE run_call (exactly one AICall, purpose 'assistant-adaptation')
    + ONE sidecar row (with the member); cited pks == retrieval top-k."""
    assert AICall.objects.count() == 0 and AssistantRun.objects.count() == 0
    _index, hits, cited_rows = corpus_top(member, HAVE, NEED)
    provider = seeded_provider(cited_rows)
    result = assistant.ask(
        member, have_text=HAVE, need_text=NEED, provider=provider
    )
    # Cited guides == retrieval top-k, EXACTLY (no sneaky filter changes).
    assert [c["pk"] for c in result["cited"]] == [pk for pk, _s, _c in hits]
    assert result["cited"][0]["title"] == "Cook ugali on a jiko"
    # the ledger row: ONE, provider 'mock', the Prompt 34 purpose
    rows = AICall.objects.filter(purpose=assistant.ASSISTANT_LEDGER_PURPOSE)
    assert rows.count() == 1 and rows.get().provider == "mock"
    # the sidecar: one row, the member, honest ungated cooking state
    assert AssistantRun.objects.count() == 1
    run = result["run"]
    assert run.user == member and run.gated is False and run.safety_block == ""
    assert result["draft"] == ADAPTED and run.draft_text == ADAPTED
    # the run really consumed the fixture (determinism, not luck)
    assert provider.calls[0]["fixture_hit"] is True


@pytest.mark.django_db
def test_acceptance_sufuria_jiko_ughali_adapted_cited_labeled(member, ugali_guide):
    """PROMPTS.md Prompt 34 acceptance: 'I have a sufuria, a jiko, and
    ughali flour' produces an adapted, cited, safety-labelled plan —
    labeled as AI (the badge pair from ai_label())."""
    _index, _hits, cited_rows = corpus_top(member, HAVE, NEED)
    result = assistant.ask(
        member, have_text=HAVE, need_text=NEED, provider=seeded_provider(cited_rows)
    )
    text, css = result["label"]
    assert result["draft"] == ADAPTED  # the adapted plan
    assert css == AI_BADGE_CLASS == "badge-ai" and "AI-assisted" in text
    assert [c["pk"] for c in result["cited"]] == [ugali_guide.pk]  # cited
    assert result["cited"] == result["run"].cited  # pinned on the run row


@pytest.mark.django_db
def test_gated_run_appends_medical_warning_verbatim(member):
    """A MEDICAL-cited run: the guide's own warning rides VERBATIM in the
    appended block + on the run row; the draft stays separate."""
    make_guide(
        member,
        title_en="First aid for a cut hand",
        category="health-education",
        steps=[
            "Wash the wound with clean running water.",
            "Cover with a clean bandage.",
        ],
        safety=SafetyCategory.MEDICAL.value,
        warning=MEDICAL_WARNING,
    )
    have, need = "I cut my hand and I have a clean bandage", "first aid"
    _index, _hits, cited_rows = corpus_top(member, have, need)
    provider = seeded_provider(
        cited_rows,
        reply='Plan adapted from "First aid for a cut hand": wash, cover.',
    )
    result = assistant.ask(member, have_text=have, need_text=need, provider=provider)
    assert result["gated"] is True
    assert MEDICAL_WARNING in result["safety_block"]       # verbatim append
    assert result["run"].gated is True
    assert MEDICAL_WARNING in result["run"].safety_block   # pinned on the row
    assert MEDICAL_WARNING not in result["draft"]          # draft never merged


# --------------------------------------------------- per-user daily quota
@pytest.mark.django_db
def test_quota_five_then_refused_without_writing_anything(member, ugali_guide):
    """5 runs ok; the 6th hits AssistantQuotaExceeded and writes NOTHING
    (no ledger row, no sidecar row) — the honest free-tier state."""
    _index, _hits, cited_rows = corpus_top(member, HAVE, NEED)
    provider = seeded_provider(cited_rows)
    for _ in range(assistant.ASSISTANT_DAILY_RUNS_PER_USER):
        assistant.ask(member, have_text=HAVE, need_text=NEED, provider=provider)
    assert assistant.runs_used_today(member) == assistant.ASSISTANT_DAILY_RUNS_PER_USER
    assert (
        AICall.objects.filter(purpose=assistant.ASSISTANT_LEDGER_PURPOSE).count()
        == assistant.ASSISTANT_DAILY_RUNS_PER_USER
    )
    before = AICall.objects.count()
    with pytest.raises(assistant.AssistantQuotaExceeded) as einfo:
        assistant.ask(member, have_text=HAVE, need_text=NEED, provider=provider)
    assert "AI assistance is resting for today" in str(einfo.value.message)
    assert AICall.objects.count() == before           # refused run logs NOTHING
    assert AssistantRun.objects.count() == assistant.ASSISTANT_DAILY_RUNS_PER_USER


@pytest.mark.django_db
def test_quota_resets_the_next_day(member):
    """The day boundary rolls forward by itself — tomorrow the member is
    fresh (no reset job, same accounting as the ledger)."""
    seed_runs(member, assistant.ASSISTANT_DAILY_RUNS_PER_USER, at=TODAY)
    with pytest.raises(assistant.AssistantQuotaExceeded):
        assistant.ask(
            member, have_text=HAVE, need_text=NEED,
            provider=providers.MockLLM(), now=TODAY,
        )
    result = assistant.ask(
        member, have_text=HAVE, need_text=NEED,
        provider=providers.MockLLM(), now=TOMORROW,
    )
    assert result["run"].user == member
    assert AssistantRun.objects.count() == assistant.ASSISTANT_DAILY_RUNS_PER_USER + 1


# ------------------------------------------------ unavailable degradation
@pytest.mark.django_db
def test_ai_unavailable_degrades_with_zero_rows(member, ugali_guide):
    """Budget exhausted (or a stub provider): ask() raises AiUnavailable
    and writes NOTHING — no row, no 500, no fake answer."""
    seed_tokens(ledger.AI_DAILY_TOKEN_BUDGET, at=TODAY)
    before = AICall.objects.count()
    with pytest.raises(ledger.AiUnavailable):
        assistant.ask(
            member, have_text=HAVE, need_text=NEED,
            provider=providers.MockLLM(), now=TODAY,
        )
    assert AICall.objects.count() == before
    assert AssistantRun.objects.count() == 0
    # a stub selected explicitly degrades the same honest way
    with pytest.raises(ledger.AiUnavailable):
        assistant.ask(
            member, have_text=HAVE, need_text=NEED,
            provider=providers.get_provider("ollama"),
        )
    assert AICall.objects.count() == before
    assert AssistantRun.objects.count() == 0


# ------------------------------------------------ views: the labelled flow
@pytest.mark.django_db
def test_ask_get_shows_guided_intake_and_honest_meter(client_member):
    resp = client_member.get(reverse("ai:ask"))
    assert resp.status_code == 200
    html = resp.content.decode()
    # the guided intake questions, as form fields
    assert 'name="have_text"' in html and 'name="need_text"' in html
    assert "What do you have on hand?" in html
    # the honest per-user meter (0 used, 5 free left)
    assert "5 free adaptations left today" in html
    # a bare form shows NO AI content → NO AI badge
    assert "badge badge-ai" not in html
    assert_under_budget(html, kb=50)


@pytest.mark.django_db
def test_ask_post_renders_labeled_draft_with_citations(
    client_member, member, ugali_guide
):
    """POST → ONE ledgered run → the labelled draft render: badge pair,
    cited guide (titled + linked), verify CTA, under the data budget."""
    resp = client_member.post(
        reverse("ai:ask"), {"have_text": HAVE, "need_text": NEED}
    )
    assert resp.status_code == 200
    html = resp.content.decode()
    # THE BADGE CONTRACT: the draft always renders with ai_label()'s pair
    assert "badge badge-ai" in html and "AI-assisted" in html
    # the active provider is the unseeded mock → the honest fallback copy,
    # which is ALSO an AI draft render and so carries the same badge
    assert "MOCK (no fixture)" in html
    # the cited guide renders TITLED + LINKED (guides:detail)
    assert f'href="/guides/{ugali_guide.pk}/"' in html
    assert "Cook ugali on a jiko" in html
    assert "Did this work for you?" in html  # the community-verify CTA
    rows = AICall.objects.filter(purpose=assistant.ASSISTANT_LEDGER_PURPOSE)
    assert rows.count() == 1 and assistant.runs_used_today(member) == 1
    assert_under_budget(html, kb=50)


@pytest.mark.django_db
def test_ask_post_medical_gated_renders_warning_verbatim(client_member, member):
    """A MEDICAL-cited POST render: the guide's own safety warning appears
    VERBATIM in the page and the draft stays labeled (gate never waives
    the badge; the guide warning is never overridden)."""
    make_guide(
        member,
        title_en="First aid for a cut hand",
        category="health-education",
        steps=[
            "Wash the wound with clean running water.",
            "Cover with a clean bandage.",
        ],
        safety=SafetyCategory.MEDICAL.value,
        warning=MEDICAL_WARNING,
    )
    have, need = "I cut my hand and I have a clean bandage", "first aid"
    resp = client_member.post(reverse("ai:ask"), {"have_text": have, "need_text": need})
    assert resp.status_code == 200
    html = resp.content.decode()
    assert MEDICAL_WARNING in html          # VERBATIM, rendered as the block
    assert "badge badge-ai" in html         # still labeled on the gated render
    assert "First aid for a cut hand" in html  # cited, titled
    assert AICall.objects.filter(purpose=assistant.ASSISTANT_LEDGER_PURPOSE).count() == 1
    run = AssistantRun.objects.get()
    assert run.gated is True and MEDICAL_WARNING in run.safety_block


@pytest.mark.django_db
def test_ask_quota_state_honest_no_badge_no_rows(client_member, member):
    """Quota spent: 200 + the honest resting message; NO AI content (no
    badge, no fallback draft) and the refused run wrote NOTHING."""
    seed_runs(member, assistant.ASSISTANT_DAILY_RUNS_PER_USER)
    resp = client_member.post(
        reverse("ai:ask"), {"have_text": "sufuria", "need_text": "ugali"}
    )
    assert resp.status_code == 200  # never a 500
    html = resp.content.decode()
    assert "AI assistance is resting for today" in html
    assert "badge badge-ai" not in html  # no AI content renders → no badge
    assert "MOCK (no fixture)" not in html
    assert AICall.objects.count() == 0  # refused run: nothing logged
    assert AssistantRun.objects.count() == assistant.ASSISTANT_DAILY_RUNS_PER_USER


@pytest.mark.django_db
def test_ask_unavailable_state_honest_never_500(client_member):
    """A stub left in settings: the POST renders the honest message —
    no 500, no badge (no AI content), nothing logged."""
    with override_settings(AI_PROVIDER_NAME="ollama"):
        resp = client_member.post(
            reverse("ai:ask"), {"have_text": HAVE, "need_text": NEED}
        )
    assert resp.status_code == 200
    html = resp.content.decode()
    assert "AI temporarily unavailable" in html
    assert "badge badge-ai" not in html
    assert AICall.objects.count() == 0 and AssistantRun.objects.count() == 0


@pytest.mark.django_db
def test_ask_requires_login(client, member):
    """The quota is per user and the verify click writes the member's row
    — anonymous visitors bounce (302) and nothing runs."""
    assert client.get(reverse("ai:ask")).status_code == 302
    assert (
        client.post(reverse("ai:ask"), {"have_text": "x", "need_text": "y"}).status_code
        == 302
    )
    assert AICall.objects.count() == 0


# -------------------------------------------------------- the verify click
@pytest.mark.django_db
def test_helpful_click_flags_own_run_and_thanks(client_member, member, stranger):
    """POST "Did this work for you?" → the run row's honest flag + a plain
    thanks render; GET refused; someone else's run is a 404 (never a
    leak); the click aggregates nothing (no content writes)."""
    run = AssistantRun.objects.create(user=member, cited=[], draft_text="d")
    assert client_member.get(reverse("ai:helpful", args=[run.pk])).status_code == 405
    resp = client_member.post(reverse("ai:helpful", args=[run.pk]))
    assert resp.status_code == 200 and "marked helpful" in resp.content.decode()
    run.refresh_from_db()
    assert run.verified_helpful is True
    # idempotent: a second click stays ONE flag, ONE row
    client_member.post(reverse("ai:helpful", args=[run.pk]))
    assert AssistantRun.objects.count() == 1
    run.refresh_from_db()
    assert run.verified_helpful is True
    # a stranger's run is invisible (scoped to the member's own rows)
    stray = AssistantRun.objects.create(user=stranger, cited=[])
    assert (
        client_member.post(reverse("ai:helpful", args=[stray.pk])).status_code == 404
    )
    assert stray.verified_helpful is False


# ----------------------------------------------------------- plumbing green
@pytest.mark.django_db
def test_assistant_migrations_synchronized():
    """``makemigrations ai --check --dry-run`` exits clean: the shipped
    migration matches AICall + AssistantRun exactly (schema drift fails
    here, not on a member phone)."""
    call_command("makemigrations", "ai", "--check", "--dry-run", verbosity=0)


def test_django_check_green():
    call_command("check")  # raises SystemExit on a failing check

