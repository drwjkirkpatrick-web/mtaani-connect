"""assistant.py — the LOCAL-ADAPTATION ASSISTANT "Ask Mtaani"
(PROMPTS.md Prompt 34: AI that adapts to locally available resources).

The flow a member takes (view renders this — see views.ask):
  guided intake ("What do you have? What do you need?") →
  retrieval over the guides corpus (retrieval.py — deterministic tokens,
  NO embeddings) → ledgered run_call with the VERSIONED strict prompt
  (prompts.py reads ``assistant-v2``; never an inline string) → the SAFETY
  GATE (gated categories ALWAYS append the guides' own safety warnings
  VERBATIM — the assistant never overrides a guide's safety_warning) →
  ONE AssistantRun sidecar row (who, what was cited, the labelled draft,
  the safety block) → draft-only render with the Prompt 12 AI badge and
  the community-verification prompt ("Did this work for you?").

WHY A SIDECAR (AssistantRun) instead of a user column on the ledger:
Prompt 33's AICall is provider-agnostic spend history — user-free by
contract. AssistantRun carries the member's half of the run AND is the
per-user daily quota counter (its created_at day filter, same pattern as
AICall's day accounting: nothing to reset, no counters to drift).

HONEST DEGRADE ORDERS (degrade BEFORE any call, mirroring the ledger):
  1. per-user quota  → AssistantQuotaExceeded ("AI assistance is resting
     for today…") — free-tier sustainability (brief §8); NO retrieval, NO
     call, NO rows written for the rejected run;
  2. ledger guard    → AiUnavailable ("AI temporarily unavailable…").

AI content is DRAFT-ONLY (Prompt 12): nothing here writes guide/post/qa
content — the only surface is the labelled draft (ai_label() badge) that
the member reads and may verify with one boolean click; community
aggregation stays 08's realm (GuideConfirmation), documented, not built.
"""
from __future__ import annotations

from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.core.types import ContentStatus, SafetyCategory
from apps.guides import services as guide_services  # the guides PUBLIC door

from . import ledger, prompts, retrieval
from .labeling import ai_label
from .models import AssistantRun

#: Version-pinned prompt (versioning contract: exact name, never "latest").
ASSISTANT_PROMPT_VERSION = "assistant-v2"

#: The ledger purpose every adaptation logs under (Prompt 34 brief).
ASSISTANT_LEDGER_PURPOSE = "assistant-adaptation"

#: Free-tier sustainability (brief §8): adaptations per member per day.
#: Module constant like ledger.AI_DAILY_TOKEN_BUDGET — ships in tests
#: without settings wiring; the parent may hoist it into settings later.
ASSISTANT_DAILY_RUNS_PER_USER = 5

#: Retrieval top-k = the guides one answer may cite (bounded lists rule).
MAX_CITED_GUIDES = 3

#: Bounded drafts/inputs (mobile data is the user's money).
MAX_DRAFT_TOKENS = 220
MAX_HAVE_CHARS = 500
MAX_NEED_CHARS = 500

#: Corpus caps: a small estate corpus is retrievable in-memory; runaway
#: guides are capped so the index never grows unbounded per request.
MAX_CORPUS_ROWS = 200
MAX_KEYWORDS_CHARS = 1200


class AssistantQuotaExceeded(Exception):
    """The honest per-user quota state (Prompt 34; brief §8).

    Views catch it and render ``str(exc.message)`` — never a fake answer,
    never a 500. NO AI content renders in this state, so NO AI badge shows
    (the Prompt 12 label covers AI content; an empty quota state shows an
    honest system message instead).
    """

    default_message = _(
        "AI assistance is resting for today — you have used your free "
        "adaptations. Try again tomorrow."
    )

    def __init__(self, message: str | None = None) -> None:
        self.message = message if message else self.default_message
        super().__init__(self.message)


# ------------------------------------------------------------- the gate
#: Gated categories (PROMPTS.md Prompt 34 verbatim) — the guides' enum is
#: IMPORTED (mirrored, never copied: values stay frozen vocabulary).
GATED_CATEGORIES = (
    SafetyCategory.MEDICAL.value,
    SafetyCategory.ELECTRICAL.value,
    SafetyCategory.CONSTRUCTION.value,
)

SAFETY_BLOCK_HEADER = _(
    "⚠ Safety warning — copied from the adapted guide(s), exactly as written:"
)
#: Shown only if a gated run somehow has no warning text on its cited
#: guides (guides' model clean() makes a real gated guide unable to exist
#: without one) — the honest standardized line, not a fabricated warning.
SAFETY_BLOCK_FALLBACK = _(
    "This topic involves medical, electrical or construction risk; the "
    "guide's own safety warning is authoritative — stop and consult a "
    "qualified professional before proceeding."
)


def assistant_payload(*, category: str, guide_warnings, draft_text: str) -> dict:
    """THE SAFETY GATE (PROMPTS.md Prompt 34).

    ``category`` is the safety category driving the gate (assistant.ask
    computes it from the CITED guides), ``guide_warnings`` is the list of
    ``(guide_title, guide_safety_warning)`` pairs for those cited guides.

    Gated categories (GATED_CATEGORIES) ALWAYS append the guides' own
    safety_warning VERBATIM as the standard safety-warning block — never
    shortened, paraphrased or replaced — and NEVER modify the draft (the
    draft cannot "corrupt" the warnings; the block rides beside it).
    Ungated categories append NO block: ``safety_block`` is "".
    """
    is_gated = (category or "") in GATED_CATEGORIES
    block = ""
    if is_gated:
        blocks = [
            f"{title}:\n{warning}"
            for title, warning in guide_warnings
            if (warning or "").strip()
        ]
        block = str(SAFETY_BLOCK_HEADER) + "\n" + (
            "\n\n".join(blocks) if blocks else str(SAFETY_BLOCK_FALLBACK)
        )
    return {"draft": draft_text, "safety_block": block, "gated": is_gated}


# ----------------------------------------------------------- corpus + query
def corpus_rows(for_user) -> list[dict]:
    """Guide-corpus rows via the guides PUBLIC read door (Prompt 08:
    ``services.visible_guides`` + ``services.latest_published_revision``).

    Grounding uses PUBLIC knowledge only: PUBLISHED guides (a member's own
    drafts are excluded — the assistant never adapts unpublished work),
    their newest PUBLISHED revision's step text as the keyword source, and
    the guide's own safety fields carried through untouched (the gate
    needs them). Bounded at MAX_CORPUS_ROWS / MAX_KEYWORDS_CHARS.
    """
    guides = (
        guide_services.visible_guides(for_user)
        .filter(status=ContentStatus.PUBLISHED.value)
        .order_by("pk")[:MAX_CORPUS_ROWS]
    )
    rows: list[dict] = []
    for guide in guides:
        revision = guide_services.latest_published_revision(guide)
        if revision is None:
            continue  # no published content — honest: nothing to ground on
        parts = [guide.title_en, guide.title_sw, guide.category]
        parts += [step.text for step in revision.steps.all()]
        keywords = " ".join(part for part in parts if part.strip())[:MAX_KEYWORDS_CHARS]
        rows.append(
            {
                "pk": guide.pk,
                "title": guide.display_title,
                "category": guide.category,
                "safety_category": guide.safety_category,
                "safety_warning": guide.safety_warning,
                "keywords": keywords,
                # tags: the safety-category vocabulary — a member who types
                # "wiring" / "medicine" gets category-matched grounding.
                "tags": guide.safety_category,
            }
        )
    return rows


def query_text(have_text: str, need_text: str) -> str:
    """The ONE deterministic retrieval query: the two intake answers."""
    return f"{(have_text or '').strip()}\n{(need_text or '').strip()}"


def compose_intake_message(have_text: str, need_text: str, cited_rows) -> list[dict]:
    """The bounded, deterministic user message (PURE — fixture-keyed tests
    reproduce it exactly). It carries the intake verbatim plus ONLY the
    retrieved guide content, so the model cannot cite anything else."""
    guide_blocks = [
        (
            f'[{row["pk"]}] "{row["title"]}" (category: {row["category"]}; '
            f'safety handling: {row.get("safety_category") or "NONE"}) — '
            f'{row.get("keywords", "")}'
        )
        for row in cited_rows
    ]
    content = "\n\n".join(
        [
            "Member writes — what I have on hand:",
            (have_text or "").strip(),
            "What I need help with:",
            (need_text or "").strip(),
            "Published guide content you may adapt (name the guide each "
            "adapted step draws on; never cite anything you were not given):",
            "\n\n".join(guide_blocks)
            if guide_blocks
            else "(no guide content matched — say plainly that nothing fits "
            "and give no substitutes)",
        ]
    )
    return [{"role": "user", "content": content}]


# ----------------------------------------------------------------- quota
def runs_used_today(user, *, now=None) -> int:
    """Adaptation runs the member has used TODAY (the AssistantRun day
    filter — nothing to reset, the boundary rolls forward). Injectable
    clock (hard rule); ``None`` means now."""
    day = timezone.localdate(now) if now is not None else timezone.localdate()
    return AssistantRun.objects.filter(user=user, created_at__date=day).count()


def quota_remaining(user, *, now=None) -> int:
    """Runs left today — never negative; the page meter's honest number."""
    return max(0, ASSISTANT_DAILY_RUNS_PER_USER - runs_used_today(user, now=now))


def enforce_quota(user, *, now=None) -> None:
    """The FIRST gate of ask(): refuse before any retrieval/call (the
    ledger's degrade-before-the-call order, mirrored per user)."""
    if quota_remaining(user, now=now) < 1:
        raise AssistantQuotaExceeded()


# ------------------------------------------------------------- the door
def ask(user, *, have_text: str, need_text: str, now=None, provider=None) -> dict:
    """THE Prompt 34 door: one member adaptation, fully logged and gated.

    Order matters (each can degrade honestly BEFORE side work):
    1. per-user quota (AssistantRun day counter) → AssistantQuotaExceeded;
    2. corpus build (guides PUBLIC door) + deterministic retrieval →
       the cited guides (top-k EXACTLY as retrieval returned it);
    3. the strict versioned prompt + ledger.run_call (ONE ledger row:
       guard → provider → complete → log — budget exhaustion and stub/mis-
       configured providers raise ledger.AiUnavailable and NOTHING here
       writes);
    4. assistant_payload — gated ⇒ the cited guides' warnings appended
       VERBATIM, draft never touched;
    5. ONE AssistantRun sidecar row (the "sidecar beside AICall": the
       ledger has no user column by contract).

    Returns ``{"run", "draft", "safety_block", "gated", "cited", "label"}``
    — the draft-only payload the view renders with ai_label()'s badge.
    """
    enforce_quota(user, now=now)
    index = retrieval.build_index(corpus_rows(user))
    hits = retrieval.retrieve(
        index, query_text(have_text, need_text), limit=MAX_CITED_GUIDES
    )
    # Cited guides == retrieval top-k, exactly (tested: no post-filtering
    # can sneak a guide in or drop one out).
    cited_rows = [index["rows"][pk] for pk, _score, _category in hits]
    gated_rows = [
        row
        for row in cited_rows
        if row.get("safety_category") in GATED_CATEGORIES
    ]
    gate_category = (
        gated_rows[0]["safety_category"] if gated_rows else SafetyCategory.NONE.value
    )
    result, row = ledger.run_call(
        prompts.prompt_text(ASSISTANT_PROMPT_VERSION),
        compose_intake_message(have_text, need_text, cited_rows),
        max_tokens=MAX_DRAFT_TOKENS,
        purpose=ASSISTANT_LEDGER_PURPOSE,
        now=now,
        provider=provider,
    )
    payload = assistant_payload(
        category=gate_category,
        guide_warnings=[(r["title"], r["safety_warning"]) for r in gated_rows],
        draft_text=result.text,
    )
    run = AssistantRun(
        user=user,
        have_text=(have_text or "").strip()[:MAX_HAVE_CHARS],
        need_text=(need_text or "").strip()[:MAX_NEED_CHARS],
        draft_text=payload["draft"],
        safety_block=payload["safety_block"],
        gated=payload["gated"],
        cited=[
            {"pk": r["pk"], "title": r["title"], "safety_category": r.get("safety_category") or ""}
            for r in cited_rows
        ],
    )
    run.full_clean()  # model gates, not just the form (repo pitfall pattern)
    run.save()
    return {
        "run": run,
        "draft": payload["draft"],
        "safety_block": payload["safety_block"],
        "gated": payload["gated"],
        "cited": run.cited,
        "label": ai_label(),
    }


def mark_helpful(run: AssistantRun) -> AssistantRun:
    """The community-verification click (Prompt 34): ONE honest boolean on
    the run row, idempotent. NOTHING content-bearing is written here —
    aggregated community verification is Prompt 08's realm
    (GuideConfirmation, "This worked for me in [ward]"); documented, not
    duplicated."""
    if not run.verified_helpful:
        run.verified_helpful = True
        run.save(update_fields=["verified_helpful", "updated_at"])
    return run


# NOTE (kept import-light on purpose): this module touches guides ONLY
# through apps.guides.services read doors and stores a guide URL never —
# renders derive links with {% url 'guides:detail' pk %} (reverse stays in
# the template layer so a stored link can never rot).
