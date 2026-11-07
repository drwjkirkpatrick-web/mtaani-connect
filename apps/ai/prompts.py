"""prompts.py — VERSIONED PROMPT FILES (PROMPTS.md Prompt 33).

System prompts NEVER live as inline strings: one ``.md`` file per prompt
version in THIS package's ``prompts/`` directory, name = versioned stem
(e.g. ``assistant-v1``). Header comment carries version + purpose.
Behavior changes ship NEW files (``assistant-v2``) and the call site
repoints to the exact name — that is the version pinning: code always
names a precise version, there is no "latest" resolution anywhere
(tested: ``prompt_text("assistant")`` fails; so does a typo).

``prompt_text`` is the only reader; a MISSING file raises
``PromptNotFound`` (honest error — never a silent fallback, never an
inline string smuggled into the call site). Names allow
``[a-z0-9-]`` only — no path traversal, no spaces.
"""
from __future__ import annotations

import re
from pathlib import Path

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


class PromptNotFound(LookupError):
    """The honest error for a missing/malformed prompt name.

    LookupError so generic ``except LookupError`` consumers treat it like
    a missing key, not a programming error.
    """


def prompt_text(name: str) -> str:
    """Read the SHIPPED prompt ``name`` (versioned stem) from the package.

    WHY read every time (no cache): prompt files are tiny; a live read
    keeps tests deterministic without cache invalidation and lets an
    operator hot-swap a prompt without a deploy. Version pinning comes
    from the FILENAME (exact name in the call site), not from caching.
    """
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise PromptNotFound(
            f"Bad prompt name {name!r} — expect lowercase-dash versioned "
            f"stems like 'assistant-v1'."
        )
    path = PROMPTS_DIR / f"{name}.md"
    if not path.is_file():
        raise PromptNotFound(
            f"No shipped prompt {name!r} — shipped prompts: "
            f"{', '.join(list_prompts()) or 'none'}."
        )
    return path.read_text(encoding="utf-8")


def list_prompts() -> list[str]:
    """Every shipped prompt stem, sorted — the honest catalog."""
    return sorted(path.stem for path in PROMPTS_DIR.glob("*.md"))
