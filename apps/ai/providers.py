"""providers.py — THE LLM ADAPTER SEAM (PROMPTS.md Prompt 33).

FROZEN INTERFACE (what every provider implements — ONE method):

    provider.complete(system, messages, *, max_tokens) -> LLMResult

``system`` is the system-prompt TEXT read from a versioned prompt file by
the caller (``apps.ai.prompts.prompt_text`` — prompts never live as
inline strings). ``messages`` is a list of ``{"role": "user"|
"assistant"|"system", "content": <str>}`` dicts. ``max_tokens`` is a
REQUIRED keyword — every call states its token budget explicitly.

``LLMProvider.complete`` performs NO writes: no content tables, no
moderation, no publishing — AI output is draft-only data that the CALLER
chooses to keep, and everything it shows must carry the AI label
(PROMPTS.md 12's contract / brief §6: label, never fabricate authority).
The no_auto_publish test in tests/test_ai_layer.py pins that calling
``complete`` creates zero rows anywhere.

NO SDK ANYWHERE (the Prompt 33 pitfall): no module in this app may
import an AI SDK (openai/anthropic/ollama/…) or an HTTP client lib
(requests/httpx) directly — everything enters through this interface;
the test suite scans the package import roots with ``ast`` so a quiet
SDK import cannot land. A future REAL adapter implements its transport
entirely inside its own class here (standard-library urllib is
acceptable INSIDE this one module; an SDK would enter as this package's
own adapter class — never by patching SDK calls into consumer code).

The registry + honesty pattern mirrors apps/alerts/notify.py
(NotifyAdapter) and apps/barter/mpesa.py (stub honesty: ``is_stub=True``,
loud failure on accidental use).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from hashlib import sha256

from django.conf import settings


@dataclass(frozen=True)
class LLMResult:
    """The frozen outcome of ONE provider call.

    ``text`` is DRAFT content — never published directly. ``tokens_in``/
    ``tokens_out`` feed the cost ledger (ledger.log_call); ``finish_reason``
    is the provider's honest story about stopping ("stop", "length", or
    MockLLM's "mock_fallback"). Frozen so a result can be compared,
    re-rendered and logged without mutation.
    """

    text: str
    tokens_in: int
    tokens_out: int
    model: str
    provider_id: str
    # WHY 0 default: MockLLM never reads the wall clock (determinism —
    # two identical calls must produce byte-identical results); real
    # providers pass their measured elapsed time.
    elapsed_ms: int = 0
    finish_reason: str | None = None

    @property
    def total_tokens(self) -> int:
        return self.tokens_in + self.tokens_out


class LLMProvider:
    """The interface every AI backend implements (contract in docstring).

    Subclasses only need ``complete(...)``; they must NOT mutate any
    argument and must NOT write to the database (ledgering is the
    ledger's job — one door, see apps/ai/ledger.py).
    """

    #: Registry key + the value stored on ledger rows.
    provider_id = "abstract"

    #: Honesty marker mirroring barter's MPesa stub & alert's WebPushAdapter:
    #: True ONLY for adapters that are documented extension points, not
    #: wired backends. No flow may treat a stub as a working backend.
    is_stub = False

    def complete(
        self,
        system: str,
        messages: list[dict],
        *,
        max_tokens: int,
    ) -> LLMResult:
        """Run one completion. NEVER networked from this ABC: only real
        (non-stub) subclasses define a transport, wholly inside them."""
        raise NotImplementedError


class MockLLM(LLMProvider):
    """Deterministic mock provider (hard rule: mock mode first).

    FIXTURE-KEYED DETERMINISM: replies come from a ``{key: text}``
    registry where the key is ``MockLLM.fixture_key(system, messages)``
    — a sha256 over a canonical dump of system+messages (stable across
    interpreter runs and processes; Python's builtin ``hash()`` is salted
    per-process and never used). Two fresh MockLLM instances given the
    same inputs return BYTE-IDENTICAL results — no wall clock, no RNG,
    no hidden state (same contract as alerts' MockPush).

    UNKEYED INPUTS GET AN HONEST FALLBACK — "MOCK (no fixture) …" — a
    reply that can never be mistaken for real content, because the only
    dangerous outcome here would be a fabricated-sounding answer getting
    shown as if it were real (every consumer labels its drafts anyway;
    MockLLM does its part by not producing plausibility).

    Tokens are a DETERMINISTIC word-count proxy: the ledger needs stable
    numbers to accrue budgets against, not provider-accurate usage
    (real providers report their own counts — see the stub docstrings).
    """

    provider_id = "mock"

    #: The honest no-fixture reply. Contains the literal marker the tests
    #: pin; tests also pin that it is NOT fabricated-real-sounding.
    FALLBACK_TEXT = (
        "MOCK (no fixture) — deterministic placeholder reply; this layer "
        "never fakes real content."
    )
    FINISH_FIXTURE = "stop"
    FINISH_FALLBACK = "mock_fallback"
    FINISH_TRUNCATED = "length"
    MODEL = "mock-keyed-v1"  # versioned like everything else here

    def __init__(self, fixtures: dict[str, str] | None = None) -> None:
        self.fixtures: dict[str, str] = dict(fixtures or {})
        # Ordered record of sends — mirrors MockPush.sent so tests can
        # assert exactly what a pass produced (fresh instance per
        # get_provider call keeps this honest; see get_provider's note).
        self.calls: list[dict] = []

    # ------------------------------------------------------------- keys
    @staticmethod
    def fixture_key(system: str, messages: list[dict]) -> str:
        """Canonical fixture key: sha256 of system + messages.

        WHY: the caller seeds ``{MockLLM.fixture_key(system, msgs): reply}```
        — the key is derived from the SAME canonicalization the mock's
        lookup uses, so tests can pin inputs without counting tokens or
        poking the clock.
        """
        payload = json.dumps(
            {"system": system or "", "messages": messages},
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return sha256(payload.encode("utf-8")).hexdigest()[:32]

    # ---------------------------------------------------------- helpers
    @staticmethod
    def _validate(system: str, messages: list[dict]) -> None:
        """Cheap, deterministic shape errors (honest, loud ValueError)."""
        if not isinstance(system, str):
            raise ValueError("system must be a str (read it via prompt_text)")
        if not messages or not isinstance(messages, (list, tuple)):
            raise ValueError(
                "messages must be a non-empty list of "
                "{'role': …, 'content': …} dicts"
            )
        for message in messages:
            if (
                not isinstance(message, dict)
                or "role" not in message
                or "content" not in message
            ):
                raise ValueError(
                    "each message must be a dict with 'role' and 'content'"
                )

    @staticmethod
    def _approx_tokens(text: str) -> int:
        """Word-count proxy — documented above; NOT provider usage."""
        return len((text or "").split())

    # ------------------------------------------------------------ the API
    def complete(
        self,
        system: str,
        messages: list[dict],
        *,
        max_tokens: int,
    ) -> LLMResult:
        self._validate(system, messages)
        key = self.fixture_key(system, messages)
        text = self.fixtures.get(key)
        if text is None:
            text = self.FALLBACK_TEXT
            finish = self.FINISH_FALLBACK
        else:
            finish = self.FINISH_FIXTURE

        words = text.split()
        if max_tokens is not None and len(words) > max_tokens:
            # WHY truncate: the interface promises callers cannot silently
            # blow a token budget — not even in mock mode.
            text = " ".join(words[:max_tokens])
            finish = self.FINISH_TRUNCATED

        tokens_in = self._approx_tokens(
            " ".join([system or ""] + [str(m.get("content", "")) for m in messages])
        )
        result = LLMResult(
            text=text,
            tokens_in=tokens_in,
            tokens_out=self._approx_tokens(text),
            model=self.MODEL,
            provider_id=self.provider_id,
            # 0 on purpose — determinism contract (class docstring).
            elapsed_ms=0,
            finish_reason=finish,
        )
        self.calls.append(
            {
                "fixture_key": key,
                "fixture_hit": finish == self.FINISH_FIXTURE,
                "max_tokens": max_tokens,
                "result": result,
            }
        )
        return result


class OllamaProvider(LLMProvider):
    """STUB — documented LOCAL extension point: Ollama on the Jetson IS a
    valid local backend (brief §6: local models qualify).

    WHAT WOULD GO HERE LATER (documented so the implementer starts right;
    NONE of this is built or networked in Prompt 33):
    - constructor: ``base_url`` (default http://127.0.0.1:11434) + ``model``
      (e.g. a small instruct GGUF pulled per apps' model-acquisition docs).
    - ``complete`` POSTs JSON to ``{base_url}/api/chat``:
        {"model": <model>, "stream": false,
         "messages": [{"role": "system", "content": system}] + messages}
      and reads back ``{"message": {"content": …}, "done": true,
      "prompt_eval_count": n_in, "eval_count": n_out}``.
    - tokens_in/tokens_out map to prompt_eval_count/eval_count; elapsed_ms
      is measured around the HTTP call; finish_reason from ``done``.

    WHY __init__ does NOT raise (unlike barter's MPesa stub): the registry
    must SELECT this provider by config (``AI_PROVIDER_NAME = "ollama"``)
    — swapping providers is config, not code (PROMPTS.md 33 acceptance).
    The loud failure moved to exactly where a network call WOULD happen:
    ``complete()`` raises NotImplementedError, so an unimplemented backend
    can never silently return nothing (or worse, something fabricated).
    """

    provider_id = "ollama"
    is_stub = True

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:11434",
        model: str = "llama3",
    ) -> None:
        self.base_url = base_url
        self.model = model

    def complete(
        self,
        system: str,
        messages: list[dict],
        *,
        max_tokens: int,
    ) -> LLMResult:
        raise NotImplementedError(
            "OllamaProvider is a stub: the local-model transport is a "
            "documented later extension point (PROMPTS.md 33). Implement "
            "the /api/chat POST documented on this class first — the "
            "ledger logs whatever it returns, the budget guard already "
            "covers it."
        )


class OpenAICompatProvider(LLMProvider):
    """STUB — documented CLOUD extension point (any OpenAI-compatible API.

    WHAT WOULD GO HERE LATER (documented, NOT built — no SDK import
    ANYWHERE, not even for the real thing: PROMPTS.md 33 pitfall. A real
    implementation either posts HTTPS itself here or is its own adapter
    class behind this same interface):
    - constructor: ``api_key`` (from .env via settings — never committed,
      never logged), ``base_url`` (default https://api.openai.example/v1),
      ``model`` (e.g. some small instruct model).
    - ``complete`` POSTs JSON to ``{base_url}/chat/completions`` with
      header ``Authorization: Bearer <api_key>`` and body
        {"model": <model>,
         "messages": [{"role": "system", "content": system}] + messages,
         "max_tokens": <max_tokens>}
      reading back ``choices[0].message.content``,
      ``usage.prompt_tokens`` / ``usage.completion_tokens``, and the
      finish_reason of the choice — the exact LLMResult shape.

    Same honesty as OllamaProvider: selectable by config, ``complete()``
    fails loudly on first use. Kenya-context default is the LOCAL Jetson
    (no per-token data spend for members); cloud stays opt-in per the
    data-budget hard rule.
    """

    provider_id = "openai-compat"
    is_stub = True

    def __init__(
        self,
        api_key: str = "",
        base_url: str = "https://api.openai.example/v1",
        model: str = "small-instruct",
    ) -> None:
        # NOTE: a stub never even stores credentials — the real class
        # takes it from settings/.env (PROMPTS.md secrets rule).
        self.api_key = api_key
        self.base_url = base_url
        self.model = model

    def complete(
        self,
        system: str,
        messages: list[dict],
        *,
        max_tokens: int,
    ) -> LLMResult:
        raise NotImplementedError(
            "OpenAICompatProvider is a stub: cloud transport is a "
            "documented later extension point (PROMPTS.md 33). No SDK "
            "import may enter this app — implement the HTTP POST "
            "documented on this class, inside this one class."
        )


# ----------------------------------------------------------------- registry
# Name → class. Default is MockLLM (mock mode first). A future adapter
# registers itself HERE — no flow changes (PROMPTS.md 33 acceptance:
# swapping providers is config, not code). Unknown names raise — never guess.
PROVIDERS: dict[str, type[LLMProvider]] = {
    MockLLM.provider_id: MockLLM,
    OllamaProvider.provider_id: OllamaProvider,
    OpenAICompatProvider.provider_id: OpenAICompatProvider,
}
DEFAULT_PROVIDER = MockLLM.provider_id


def get_provider(name: str | None = None) -> LLMProvider:
    """Resolve ``name`` (or the default) into a FRESH provider instance.

    WHY fresh per call: identical to alerts' get_adapter — a feature
    owns its adapter for one pass; no hidden shared state means two
    identical runs with fresh providers are provably identical (the
    determinism tests use this).
    """
    name = name or DEFAULT_PROVIDER
    try:
        klass = PROVIDERS[name]
    except KeyError:
        raise ValueError(
            f"Unknown AI provider {name!r} — one of {sorted(PROVIDERS)}"
        ) from None
    return klass()


def get_active_provider() -> LLMProvider:
    """The CONFIGURED provider: ``settings.AI_PROVIDER_NAME``, default 'mock'.

    The settings var is deliberately NOT pre-defined in mtaani/settings
    (parent-owned file) — absent = mock mode. When the parent wires a
    real backend they add ``AI_PROVIDER_NAME = env("AI_PROVIDER_NAME",
    default="mock")`` to .env.example/base.py and set an in-repo name:
    swapping providers is CONFIG, not code (PROMPTS.md 33 acceptance).

    WHY not django-import-string like SMS_ADAPTER: provider CLASSES are
    registry members above — name keys keep the registry the single
    catalog (mirrors NotifyAdapter ADAPTERS, not apps.core.comms).
    """
    name = getattr(settings, "AI_PROVIDER_NAME", DEFAULT_PROVIDER)
    return get_provider(name)
