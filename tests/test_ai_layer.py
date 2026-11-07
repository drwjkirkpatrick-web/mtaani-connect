"""Prompt 33 acceptance tests: the AI abstraction layer.

Covers the spec bullet list verbatim — MockLLM determinism, registry
selection, cost ledger accrual, budget tripwire, prompt-version pinning —
plus the parent's additions: the no-SDK-import pitfall scan (via ast),
day-boundary accrual, the honest "AI temporarily unavailable" degrade
(page + JSON + probe, never a 500), the Prompt 12 labeling seam, the
staff-only status page + probe endpoint, the no-auto-publish contract,
and a green ``manage.py check`` / synchronized migrations at the end.

Rules the file encodes (see module docstrings under apps/ai):
- determinism = NO wall clock, NO RNG in MockLLM (byte-identical repeats);
- the ledger is TOKENS/day (no currency by design);
- the guard degrades BEFORE the call, never after;
- complete() writes NOTHING (rows come from log_call/run_call only).
"""
from __future__ import annotations

import ast
import json
from datetime import timedelta
from pathlib import Path

import pytest
from django.core.management import call_command
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.ai import ledger, prompts, providers
from apps.ai.labeling import AI_BADGE_CLASS, AI_LABEL_TEXT, ai_label
from apps.ai.models import AICall
from apps.core.testing import assert_under_budget, mock_now

# Deterministic anchor (Africa/Nairobi is UTC+3 — both stamps below map to
# unambiguous calendar days in the display TZ, so the day-boundary test
# can't flake on a tz edge).
TODAY = mock_now()  # 2026-01-15 09:30 UTC
YESTERDAY = TODAY - timedelta(days=1)

SYSTEM = "You are a test assistant."
MESSAGES = [{"role": "user", "content": "Habari?"}]
FIXTURE_TEXT = "Asante — habari njema."

FORBIDDEN_SDK_ROOTS = {
    # AI SDKs (the Prompt 33 pitfall: even ONE direct import fakes the seam)
    "openai", "anthropic", "ollama", "litellm", "groq", "cohere",
    "together", "google",
    # HTTP client libraries (a real adapter may only use stdlib urllib,
    # wholly inside its own class — see OllamaProvider's docstring)
    "requests", "httpx", "aiohttp",
}


# ----------------------------------------------------------------- helpers
def seed_spend(tokens: int, *, at=None, purpose: str = "seed") -> AICall:
    """Put ``tokens`` into TODAY'S ledger (or at ``at``).

    AICall.created_at is auto_now_add, so the create cannot pin the day —
    a plain UPDATE lands the row anywhere the test wants. Determinism:
    tests always pin the day they mean (injected-clock hard rule).
    """
    result = providers.LLMResult(
        text="seed",
        tokens_in=tokens,
        tokens_out=0,
        model="seed-model",
        provider_id="seed",
        elapsed_ms=0,
    )
    row = ledger.log_call(result, purpose)
    when = at if at is not None else timezone.now()
    AICall.objects.filter(pk=row.pk).update(created_at=when)
    row.refresh_from_db(fields=["created_at"])
    return row


@pytest.fixture
def staff_client(django_user_model, client):
    user = django_user_model.objects.create_user(
        username="mwalimu", email="mwalimu@example.ke", password="***",
        is_staff=True,
    )
    client.force_login(user)
    return client


# ------------------------------------------------------ MockLLM determinism
@pytest.mark.django_db
def test_mockllm_byte_identical_on_repeat():
    """Same input twice (fresh instances — the registry's actual shape)
    → byte-identical results, deterministically zero elapsed time."""
    fixtures = {providers.MockLLM.fixture_key(SYSTEM, MESSAGES): FIXTURE_TEXT}
    first = providers.MockLLM(fixtures).complete(SYSTEM, MESSAGES, max_tokens=64)
    second = providers.MockLLM(fixtures).complete(SYSTEM, MESSAGES, max_tokens=64)
    assert first == second  # frozen dataclass field-wise equality
    assert first.text == second.text == FIXTURE_TEXT
    assert first.text.encode() == second.text.encode()  # byte-identical
    assert first.elapsed_ms == second.elapsed_ms == 0  # clock never read
    assert first.finish_reason == providers.MockLLM.FINISH_FIXTURE == "stop"


@pytest.mark.django_db
def test_mockllm_fixture_key_is_stable_and_input_derived():
    """The key derives from system+messages canonically — same inputs,
    same key across instances/runs (never the salted builtin hash())."""
    key_a = providers.MockLLM.fixture_key(SYSTEM, MESSAGES)
    key_b = providers.MockLLM.fixture_key(SYSTEM, MESSAGES)
    assert key_a == key_b and len(key_a) == 32
    other = providers.MockLLM.fixture_key(SYSTEM + "!", MESSAGES)
    assert other != key_a  # different inputs → different key


@pytest.mark.django_db
def test_mockllm_fallback_marks_mock_honestly():
    """An unkeyed call gets the honest MOCK marker — never a plausible
    fabrication that could ride unlabeled into a surface."""
    result = providers.MockLLM().complete(SYSTEM, MESSAGES, max_tokens=64)
    assert result.text.startswith("MOCK (no fixture)")
    assert result.finish_reason == providers.MockLLM.FINISH_FALLBACK


@pytest.mark.django_db
def test_mockllm_respects_max_tokens():
    """The interface promises callers can't silently blow a token budget
    — mock mode truncates deterministically and says ``length``."""
    fixtures = {
        providers.MockLLM.fixture_key(SYSTEM, MESSAGES): "one two three four five six"
    }
    result = providers.MockLLM(fixtures).complete(SYSTEM, MESSAGES, max_tokens=3)
    assert result.text == "one two three"
    assert result.finish_reason == providers.MockLLM.FINISH_TRUNCATED


@pytest.mark.django_db
def test_mockllm_validates_message_shape_loudly():
    """Deterministic shape errors — loud ValueError, no silent behavior."""
    with pytest.raises(ValueError):
        providers.MockLLM().complete(SYSTEM, [], max_tokens=10)
    with pytest.raises(ValueError):
        providers.MockLLM().complete(SYSTEM, [{"content": "no role"}], max_tokens=10)
    with pytest.raises(ValueError):
        providers.MockLLM().complete(12345, MESSAGES, max_tokens=10)


# ------------------------------------------------------------ registry
@pytest.mark.django_db
def test_registry_default_is_mock_and_fresh_per_call():
    first = providers.get_provider()
    second = providers.get_provider()
    assert first.provider_id == "mock" and first.is_stub is False
    assert first is not second  # fresh instance (no shared state)


@pytest.mark.django_db
def test_registry_unknown_name_raises_honestly():
    with pytest.raises(ValueError) as einfo:
        providers.get_provider("nope")
    assert "nope" in str(einfo.value)
    for known in providers.PROVIDERS:  # the honest catalog in the error
        assert known in str(einfo.value)


@pytest.mark.django_db
def test_registry_stub_selection_without_network_attempts():
    """Both documented stubs select by NAME (config, not code) and fail
    LOUDLY exactly where a network call would be. No SDK/transport exists
    (the AST scan below proves no import could dial out, ever)."""
    for name in ("ollama", "openai-compat"):
        provider = providers.get_provider(name)
        assert provider.is_stub is True
        with pytest.raises(NotImplementedError) as einfo:
            provider.complete(SYSTEM, MESSAGES, max_tokens=64)
        assert "documented" in str(einfo.value)  # points at the extension
        assert provider.provider_id == name


@pytest.mark.django_db
def test_active_provider_reads_settings_seam():
    """``settings.AI_PROVIDER_NAME`` (absent in repo settings = mock) is
    the config switch — swapping providers changes config, not code."""
    assert providers.get_active_provider().provider_id == "mock"
    with override_settings(AI_PROVIDER_NAME="openai-compat"):
        assert providers.get_active_provider().provider_id == "openai-compat"
    with override_settings(AI_PROVIDER_NAME="nope"):
        with pytest.raises(ValueError):  # misconfigured names stay LOUD here
            providers.get_active_provider()


def test_no_sdk_imports_anywhere_in_the_package():
    """THE PITFALL TEST (PROMPTS.md 33): scan every .py file under
    apps/ai with ast — no AI SDK and no HTTP client lib may be imported
    even once. Docstrings may MENTION cloud APIs; imports may not exist.
    """
    package_dir = Path(providers.__file__).resolve().parent
    offenders = []
    for path in sorted(package_dir.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots = {alias.name.split(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                roots = {node.module.split(".")[0]}
            else:
                continue
            bad = roots & FORBIDDEN_SDK_ROOTS
            if bad:
                offenders.append((path.name, sorted(bad)))
    assert offenders == [], f"SDK imports found (abstraction is fake): {offenders}"


# ------------------------------------------------------------- cost ledger
@pytest.mark.django_db
def test_cost_ledger_accrues_one_row_per_call():
    """Two run_call's → exactly two rows, with honest token counts."""
    fixtures = {providers.MockLLM.fixture_key(SYSTEM, MESSAGES): FIXTURE_TEXT}
    provider = providers.MockLLM(fixtures)
    first_result, first_row = ledger.run_call(
        SYSTEM, MESSAGES, max_tokens=64, purpose="one", provider=provider
    )
    second_result, second_row = ledger.run_call(
        SYSTEM, MESSAGES, max_tokens=64, purpose="two", provider=provider
    )
    assert AICall.objects.count() == 2
    assert first_row.provider == "mock" and first_row.model == "mock-keyed-v1"
    assert first_row.total_tokens == first_result.total_tokens > 0
    assert first_row.purpose == "one" and second_row.purpose == "two"
    assert first_row.pk != second_row.pk


@pytest.mark.django_db
def test_ledger_day_boundary_excludes_yesterday():
    """The guard counts TODAY'S rows only — yesterday's spend does NOT
    ride into today (and no reset job exists to make it vanish today)."""
    seed_spend(100, at=YESTERDAY)
    seed_spend(40, at=TODAY)
    used, remaining = ledger.daily_budget_guard(now=TODAY)
    assert used == 40
    assert remaining == ledger.AI_DAILY_TOKEN_BUDGET - 40
    # And the same ledger read against YESTERDAY keeps that day's spend.
    assert ledger.tokens_used_today(now=YESTERDAY) == 100


@pytest.mark.django_db
def test_log_raw_and_result_paths_agree():
    """The dict-form door records exactly what a real adapter would say."""
    row = ledger.log_raw(
        provider="future", model="m1", tokens_in=11, tokens_out=7,
        elapsed_ms=120, purpose="raw",
    )
    assert row.total_tokens == 18 and row.provider == "future"
    assert AICall.objects.filter(purpose="raw").count() == 1


@pytest.mark.django_db
def test_run_call_full_cycle_is_the_only_row_writer():
    """run_call = guard → default registry resolution (mock) → one call
    → one row. Nothing else writes."""
    result, row = ledger.run_call(
        SYSTEM, MESSAGES, max_tokens=64, purpose="cycle"
    )  # default provider resolution path (mock)
    assert AICall.objects.count() == 1  # ONE row, from run_call itself
    assert result.text == providers.MockLLM.FALLBACK_TEXT  # default mock path,
    # unkeyed → the honest marker (fixture-keyed runs use an explicit provider,
    # covered by the accrual test above).
    assert row.provider == "mock" and row.purpose == "cycle"


# ---------------------------------------------------------- budget tripwire
@pytest.mark.django_db
def test_budget_tripwire_degrades_only_at_exhaustion():
    """99.5% of budget → the layer still serves; ≥100% → the provider is
    refused BEFORE any call (remaining never goes negative)."""
    seed_spend(199_000, at=TODAY)  # 99.5% — still under
    provider = ledger.available_provider(now=TODAY)
    assert provider.provider_id == "mock"

    seed_spend(1_001, at=TODAY)  # now ≥ 100%
    with pytest.raises(ledger.AiUnavailable) as einfo:
        ledger.available_provider(now=TODAY)
    assert "AI temporarily unavailable" in str(einfo.value)
    used, remaining = ledger.daily_budget_guard(now=TODAY)
    assert used == 200_001 and remaining == 0  # honest floor, never negative

    seed_spend(500, at=TODAY)  # runaway seeding STILL shows zero remaining
    assert ledger.daily_budget_guard(now=TODAY) == (200_501, 0)


@pytest.mark.django_db
def test_run_call_degrades_before_call_on_exhaustion():
    """An exhausted day: run_call raises with NO new ledger row — the
    degrade happens BEFORE the call, never after. Injectable clock pins
    the day for the seed AND the guard (deterministic, no "today")."""
    seed_spend(ledger.AI_DAILY_TOKEN_BUDGET, at=TODAY)
    before = AICall.objects.count()
    with pytest.raises(ledger.AiUnavailable):
        ledger.run_call(
            SYSTEM, MESSAGES, max_tokens=64, purpose="blocked", now=TODAY
        )
    assert AICall.objects.count() == before


@pytest.mark.django_db
def test_run_call_translates_stub_and_misconfig_to_unavailable():
    """A stub selected by config (or an unknown name) degrade to the same
    honest state — never a 500, never a fake answer. Direct provider
    calls still raise loudly for tests (see the registry tests)."""
    before = AICall.objects.count()
    with pytest.raises(ledger.AiUnavailable):
        ledger.run_call(
            SYSTEM, MESSAGES, max_tokens=64, purpose="x",
            provider=providers.get_provider("ollama"),
        )
    with override_settings(AI_PROVIDER_NAME="ollama"):
        with pytest.raises(ledger.AiUnavailable):
            ledger.run_call(SYSTEM, MESSAGES, max_tokens=64, purpose="x")
    with override_settings(AI_PROVIDER_NAME="nope"):
        with pytest.raises(ledger.AiUnavailable):
            ledger.run_call(SYSTEM, MESSAGES, max_tokens=64, purpose="x")
    assert AICall.objects.count() == before  # degraded calls log NOTHING


# ------------------------------------------------ version-pinned prompt files
def test_prompt_loader_reads_shipped_versioned_file():
    """prompt_text('assistant-v1') reads the SHIPPED file — header pins
    the exact version, no inline strings, no "latest" resolution."""
    text = prompts.prompt_text("assistant-v1")
    assert text.startswith(
        "<!-- mtg-prompt | name: assistant-v1 | version: 1 | purpose:"
    )
    # Sentinel contract lines for the Prompt 34 consumer:
    assert "guide" in text and "safety warning" in text
    assert "never published as fact" in text


def test_prompt_version_pinning_refuses_unversioned_names():
    """Exact-version addressing only: bare/unknown/malformed names fail
    with the honest catalog — they must never fall back to "closest"."""
    for bad in ("assistant", "assistant-v99", "Assistant-V1", "", "../secret"):
        with pytest.raises(prompts.PromptNotFound):
            prompts.prompt_text(bad)


def test_prompt_catalog_lists_shipped_files():
    assert prompts.list_prompts() == ["assistant-v1", "probe-echo-v1"]


def test_probe_prompt_file_ships_with_the_status_page():
    text = prompts.prompt_text("probe-echo-v1")
    assert "status-page echo probe" in text
    assert 'badge-labelled "AI-assisted"' in text  # the contract in-file


# --------------------------------------------------- the Prompt 12 seam
def test_labeling_helper_mirrors_prompt_12():
    """ai_label() returns the (text, class) pair — same css class as qa's
    rendered AI badge (`badge badge-ai` in apps/qa detail template)."""
    label_text, css_class = ai_label()
    assert css_class == AI_BADGE_CLASS == "badge-ai"
    assert "AI-assisted" in label_text

    qa_template = Path(providers.__file__).resolve().parent.parent
    qa_detail = qa_template / "qa" / "templates" / "qa" / "detail.html"
    assert "badge-ai" in qa_detail.read_text(encoding="utf-8")  # shared class

    from apps.qa.services import set_machine_answer  # THE data-driven door
    assert callable(set_machine_answer)


# ------------------------------------------------------- views: status page
@pytest.mark.django_db
def test_status_page_is_staff_only(client, django_user_model):
    """Anonymous + ordinary member get bounced; staff sees the page."""
    assert client.get(reverse("ai:status_page")).status_code in (302, 403)
    member = django_user_model.objects.create_user(
        username="wanjiku2", email="w2@example.ke", password="***"
    )
    client.force_login(member)
    assert client.get(reverse("ai:status_page")).status_code in (302, 403)
    staff_member = django_user_model.objects.create_user(
        username="mwalimu", email="mwalimu@example.ke", password="***",
        is_staff=True,
    )
    separate = Client()
    separate.force_login(staff_member)
    assert separate.get(reverse("ai:status_page")).status_code == 200


@pytest.mark.django_db
def test_status_page_shows_budget_and_stays_under_50kb(staff_client):
    seed_spend(1500, at=timezone.now())
    response = staff_client.get(reverse("ai:status_page"))
    html = response.content.decode()
    assert "1500" in html  # today's spend, straight from the ledger
    assert str(ledger.AI_DAILY_TOKEN_BUDGET) in html
    assert_under_budget(html, kb=50)


@pytest.mark.django_db
def test_status_page_honest_when_budget_spent_never_500(staff_client):
    seed_spend(ledger.AI_DAILY_TOKEN_BUDGET, at=timezone.now())
    html = staff_client.get(reverse("ai:status_page")).content.decode()
    assert "AI temporarily unavailable" in html  # the honest state


# ------------------------------------------------- the probe (one call)
@pytest.mark.django_db
def test_probe_makes_exactly_one_labelled_mock_call(staff_client):
    assert AICall.objects.count() == 0
    response = staff_client.post(reverse("ai:probe"))
    assert response.status_code == 200  # re-render, no redirect
    assert AICall.objects.count() == 1
    row = AICall.objects.get()
    assert row.purpose == "status-probe" and row.provider == "mock"
    html = response.content.decode()
    assert "AI-assisted" in html and "badge badge-ai" in html  # labeled
    assert "MOCK (no fixture)" in html  # honest mock content, labeled


@pytest.mark.django_db
def test_probe_is_post_only_and_never_writes_content(staff_client):
    assert staff_client.get(reverse("ai:probe")).status_code == 405
    staff_client.post(reverse("ai:probe"))
    from apps.qa.models import Answer, Question
    assert Question.objects.count() == 0 and Answer.objects.count() == 0


@pytest.mark.django_db
def test_probe_degrades_when_budget_spent(staff_client):
    """Budget spent → the POST renders the honest message, logs NOTHING."""
    seed_spend(ledger.AI_DAILY_TOKEN_BUDGET + 1, at=timezone.now())
    response = staff_client.post(reverse("ai:probe"))
    assert response.status_code == 200  # never a 500
    html = response.content.decode()
    assert "AI temporarily unavailable" in html
    assert AICall.objects.count() == 1  # the seed row only
    assert "MOCK (no fixture)" not in html


@pytest.mark.django_db
def test_probe_degrades_with_stub_provider_never_500(staff_client):
    """A stub left in settings: the probe degrades honestly, logs nothing."""
    with override_settings(AI_PROVIDER_NAME="ollama"):
        response = staff_client.post(reverse("ai:probe"))
    assert response.status_code == 200
    assert "AI temporarily unavailable" in response.content.decode()
    assert AICall.objects.count() == 0


@pytest.mark.django_db
def test_status_json_mirrors_the_page(staff_client):
    response = staff_client.get(reverse("ai:status"))
    assert response.status_code == 200
    data = json.loads(response.content)  # the response IS the JSON
    used, remaining = ledger.daily_budget_guard()
    assert data["used_tokens"] == used
    assert data["remaining_tokens"] == remaining
    assert data["budget"] == ledger.AI_DAILY_TOKEN_BUDGET
    assert data["provider_active"] == "mock"
    assert data["label"] == {"text": str(AI_LABEL_TEXT), "class": AI_BADGE_CLASS}
    assert "recent_calls" in data  # a bounded list for dashboards
    assert len(data["recent_calls"]) <= 20  # ≤20-item hard rule


@pytest.mark.django_db
def test_status_json_is_staff_only(client, client_auth):
    assert client.get(reverse("ai:status")).status_code in (302, 403)
    assert client_auth.get(reverse("ai:status")).status_code in (302, 403)


# ------------------------------------------------ no-auto-publish contract
@pytest.mark.django_db
def test_complete_writes_no_rows_outside_the_ledger():
    """complete() is WRITE-FREE: two bare calls → zero rows anywhere.
    The contract, spelled: AI output is a draft a human keeps or drops;
    publishing is a CONSUMER decision, always labeled."""
    provider = providers.MockLLM()
    provider.complete(SYSTEM, MESSAGES, max_tokens=64)
    provider.complete(SYSTEM, [{"role": "user", "content": "again"}], max_tokens=64)
    assert AICall.objects.count() == 0
    from apps.qa.models import Answer, Question
    assert Question.objects.count() == 0 and Answer.objects.count() == 0

    import apps.ai as package
    import apps.ai.providers as providers_module
    assert "auto-publish" in package.__doc__.lower()
    assert "no writes" in providers_module.__doc__.lower() or "performs NO writes" in providers_module.__doc__


# -------------------------------------------------------- plumbing green
def test_django_check_green():
    call_command("check")  # raises SystemExit on a failing check


@pytest.mark.django_db
def test_ai_migrations_synchronized():
    """``makemigrations ai --check --dry-run`` exits clean — the shipped
    migration exactly matches AICall (a schema drift fails HERE)."""
    call_command("makemigrations", "ai", "--check", "--dry-run", verbosity=0)
