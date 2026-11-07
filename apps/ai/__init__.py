"""AI abstraction layer (PROMPTS.md Prompt 33) — provider-agnostic LLM access
with a token-cost ledger, a daily budget guard, and NO auto-publishing.

IMPORTANT: this package ``__init__`` stays import-FREE on purpose — Django
loads the app package during registry setup, and importing ``.models`` at
that moment races app initialization (AppRegistryNotReady). Every consumer
imports from the submodules:

    from apps.ai.providers  import LLMProvider, LLMResult, MockLLM, get_provider
    from apps.ai.ledger     import AICall, log_call, run_call, available_provider
    from apps.ai.ledger     import daily_budget_guard, AiUnavailable, AI_DAILY_TOKEN_BUDGET
    from apps.ai.prompts    import prompt_text, list_prompts, PromptNotFound
    from apps.ai.labeling   import ai_label, AI_BADGE_CLASS, AI_LABEL_TEXT

THE CONTRACT every consumer (Prompt 34 and later) must follow:

1. NO SDK IMPORTS anywhere (PROMPTS.md 33 pitfall): openai/anthropic/ollama/
   requests/httpx never appear in app code — all access goes through the
   ``LLMProvider`` interface (``providers.py``). The test suite scans this
   package with ``ast`` so a quiet SDK import can't land.
2. Prompts live in versioned FILES (``apps/ai/prompts/assistant-v1.md``);
   read them with ``prompts.prompt_text`` — never inline strings in code.
3. AI output is a DRAFT and carries a VISIBLE label (brief §6 / KDPA: label,
   never fabricate authority). ``LLMProvider.complete`` performs NO writes —
   not content tables, not moderation, nothing. Data-driven badges belong to
   the owning app: qa answers are marked only via
   ``apps.qa.services.set_machine_answer`` (Prompt 12's contract).
4. Call providers ONLY through ``ledger.run_call``: it checks the daily
   token budget BEFORE the call (degrade first, never after), logs ONE
   ledger row, and translates a stub/misconfigured provider into
   ``ledger.AiUnavailable`` — which every view renders as the honest
   "AI temporarily unavailable" state. Never a 500, never a fake answer.
"""
