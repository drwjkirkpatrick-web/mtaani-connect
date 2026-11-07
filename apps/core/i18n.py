"""i18n helpers (PROMPTS.md Prompt 35) + the missing-translation detector.

Two layers live in this project's i18n story and they answer to different
masters:

1. UI CHROME (app labels, buttons, nav) — Django's own machinery: gettext
   markers in apps + ``{% trans %}``/``{% blocktranslate %}`` in templates,
   catalogs under ``locale/``. The chrome is sw or en, nothing between.
   The DRIFT DETECTOR below keeps ``locale/sw/`` from rotting when English
   strings change: an msgid present in code but absent from the sw catalog
   makes the suite fail, so drift can never land silently.

2. CONTENT LANGUAGE (posts, guides, listings — what people WROTE) —
   free-form by design. Sheng, Kiswahili, English, code-switching: content
   fields accept whatever the member typed (alias/nickname fields already
   accept Sheng since Prompt 25). We do NOT force a content language column
   into content models; feeds show items from all members, so mixed feeds
   are the norm. ``apps.lang`` (content-language PREFERENCE + chips) gives
   the reading side structure; see apps/lang and locale/README.md for the
   parent wiring seam (a 12-seam: one optional column/attr on content items
   + one template include).

Community-reviewable translation workflow
-----------------------------------------
``locale/sw/LC_MESSAGES/django.po`` carries BOTH:
- real Kiswahili for high-traffic chrome (nav, buttons, core labels);
- entries flagged ``#, fuzzy`` == "needs review" (community workflow:
  gettext has no 'needs review' flag, fuzzy is its standard stand-in, and
  gettext ignores fuzzy entries at runtime so a shaky translation never
  reaches users until a human clears the flag).
The header comment inside the .po documents the same convention.

NOTES: no gettext CLI tools ship on this Jetson box, so .mo files are
compiled with polib (pure Python) — see refresh_sw_po()/.mo notes; if polib
is missing the drift test still works because it reads .po SOURCES, never
the binary. Everything here is pure computation (no network, no clock).
"""
from __future__ import annotations

import tokenize
import warnings
from pathlib import Path

from django.utils.translation import gettext_lazy as _lazy

# --- content-language preference vocabulary (the READING side) -------------
SPEC_CONTENT_LANGS = ("sw", "en", "both")
# WHY 'both' is the default: the app is a community feed — honest default is
# "show me everything"; members who want only-one-language opt in explicitly.
DEFAULT_CONTENT_LANG = "both"

CONTENT_LANG_CHOICES = (
    ("both", _lazy("Both (Kiswahili + English)")),
    ("sw", _lazy("Kiswahili only")),
    ("en", _lazy("English only")),
)


def normalize_content_lang(value) -> str | None:
    """Strictly validate a content-language choice ('en' | 'sw' | 'both')."""
    if value is None:
        return None
    value = str(value).strip().lower()
    return value if value in SPEC_CONTENT_LANGS else None


# --- source msgid enumeration (the CHROME scanner) -------------------------
# Scope bounds (Why: the drift test must stay well under 10s over the real
# tree and must not re-run the lexer over data/docs/media).
REPO_ROOT = Path(__file__).resolve().parents[2]
LOCALE_DIR = REPO_ROOT / "locale"
SW_PO_PATH = LOCALE_DIR / "sw" / "LC_MESSAGES" / "django.po"

# Prompt 34 settled (2026-11): apps/ai is a normal app now — its msgids are
# translated in the .po AND pinned by the detector like every other app's.
SCANNER_EXCLUDED_APPS = ()

# Python gettext call targets.
_GETTEXT_SIMPLE = {"_", "gettext", "gettext_lazy", "gettext_noop"}
_GETTEXT_N = {"ngettext", "ngettext_lazy", "npgettext", "npgettext_lazy"}
_GETTEXT_P = {"pgettext", "pgettext_lazy"}
_GETTEXT_ALL = _GETTEXT_SIMPLE | _GETTEXT_N | _GETTEXT_P

# HTML block tags carrying plural/context bodies.
_BLOCK_TAGS = {"blocktrans", "blocktranslate"}
_SINGLE_TAGS = {"trans", "translate"}

_PY_SKIP_DIRS = {"migrations", "__pycache__"}
_SKIP_TOP = {"mediafiles", "static", "data", "docs", "scripts", ".venv", ".pgdata"}


class MsgidIndex:
    """(context, msgid) -> sorted [file:line locations].

    Key shape mirrors gettext: ``("\u0004" separated)`` is how gettext itself
    disambiguates pgettext; we keep the tuple explicit instead.
    Also collects PLURAL PAIRS: (singular_key, plural_key) from ngettext
    calls and count-form blocktranslate tags — the plural catalog entries
    (msgid + msgid_plural + msgstr[0..1]) are built from these; without
    them ngettext lookups fall back to English even when both forms are
    in the catalog as separate entries.
    """

    def __init__(self):
        self.locations: dict[tuple[str, str], list[str]] = {}
        self.pairs: set[tuple[tuple[str, str], tuple[str, str]]] = set()

    def add(self, context: str, msgid: str, loc: str) -> None:
        key = (context or "", msgid)
        bucket = self.locations.setdefault(key, [])
        if loc not in bucket:
            bucket.append(loc)

    def add_pair(self, s_key: tuple[str, str], p_key: tuple[str, str]) -> None:
        if s_key != p_key:
            self.pairs.add((s_key, p_key))

    def keys(self):
        return self.locations

    def __len__(self) -> int:
        return len(self.locations)


def _source_files(roots=None, apps=None):
    """Yield (.py + .html) files inside the bounded translation scope.

    ``roots`` overrides the default walk set (tests seed synthetic trees).
    ``apps`` restricts/overrides the app list for the walk.
    """
    if roots is not None:
        seen = set()
        for root in roots:
            root = Path(root)
            if not root.exists():
                continue
            for pattern in ("*.py", "*.html"):
                for f in root.glob(f"**/{pattern}"):
                    if f.is_file() and f.resolve() not in seen:
                        seen.add(f.resolve())
                        yield f
        return
    # Default: the real project tree.
    candidates: list[Path] = []
    apps_root = REPO_ROOT / "apps"
    app_list = apps if apps is not None else [
        p.name for p in apps_root.iterdir() if p.is_dir() and p.name not in SCANNER_EXCLUDED_APPS
    ]
    for name in app_list:
        app_dir = apps_root / name
        candidates += [p for p in app_dir.rglob("*.py") if not (set(p.parts) & _PY_SKIP_DIRS)]
        candidates += list(app_dir.rglob("*.html"))
    for top in ("templates", "mtaani"):
        candidates += list((REPO_ROOT / top).rglob("*.html" if top == "templates" else "*.py"))
    seen = set()
    for f in candidates:
        resolved = f.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        yield f


def _py_extract(text: str, index: MsgidIndex, loc_base: str) -> None:
    """Walk Python string tokens following gettext-family call targets.

    Shape accepted: ``name( "literal" [, "literal"] )`` — anything dynamic
    (a NAME/NUMBER arg, f-string, nested call) disqualifies the capture.
    """
    import ast

    try:
        tokens = list(tokenize.generate_tokens(
            iter(text.splitlines(keepends=True)).__next__
        ))
    except (tokenize.TokenError, IndentationError, SyntaxError) as exc:
        warnings.warn(f"i18n scanner: {loc_base} skipped ({exc})", RuntimeWarning)
        return
    lineno = 1
    pending = None
    depth = 0  # nesting depth of the pending call's parens
    collected: list[str] = []
    for tok in tokens:
        ttype, tstring, (lineno, _), _end, _line = tok
        if ttype in (tokenize.NL, tokenize.COMMENT):
            continue
        if depth == 0:
            if ttype == tokenize.NAME and tstring in _GETTEXT_ALL:
                pending = tstring
                collected = []
            elif ttype == tokenize.OP and tstring == "(" and pending:
                depth = 1
            elif pending and ttype in (tokenize.NEWLINE, tokenize.OP, tokenize.NAME):
                pending = None  # call target was a bare alias, not a call
            continue
        # Inside a pending call.
        if ttype == tokenize.OP:
            if tstring == "(":
                depth += 1
            elif tstring == ")":
                depth -= 1
                if depth == 0:
                    _register_call(pending, collected, index, loc_base, lineno)
                    pending = None
                    collected = []
        elif ttype == tokenize.STRING:
            prefix = tstring[:min(3, len(tstring))].lower()
            if "f" in prefix or "b" in prefix:
                # f-strings/bytes yield msgids no catalog can match; gettext
                # markers on interpolated strings are an error class here.
                pending = None
                collected = []
                continue
            try:
                collected.append(ast.literal_eval(tstring))
            except (ValueError, SyntaxError):
                pending = None
        elif ttype in (tokenize.NAME, tokenize.NUMBER):
            pending = None  # dynamic argument — not extractable


def _register_call(call_name: str | None, args: list, index: MsgidIndex, loc_base: str, lineno: int) -> None:
    if not call_name:
        return
    loc = f"{loc_base}:{lineno}"
    if call_name in _GETTEXT_SIMPLE:
        if len(args) == 1:
            index.add("", args[0], loc)
    elif call_name in _GETTEXT_N:
        if len(args) >= 2:
            index.add("", args[0], loc)
            index.add("", args[1], loc)
            index.add_pair(("", args[0]), ("", args[1]))
    elif call_name in _GETTEXT_P:
        if len(args) >= 2 and isinstance(args[0], str):
            index.add(args[0], args[1], loc)


def _html_extract(text: str, index: MsgidIndex, loc_base: str) -> None:
    """Extract {% trans %}/{% blocktranslate %} msgids with the REAL lexer.

    Ride Django's own Lexer so TEXT/VAR token split and {{ expr }} handling
    match the runtime msgid byte-for-byte (render_token_list rules:
    verbatim text, ``{{ x }}`` -> ``%(x)s``). ``trimmed`` uses
    django.utils.translation.trim_whitespace — the same function the runtime
    applies.
    """
    from django.template.base import Lexer, TokenType
    from django.utils.translation import trim_whitespace

    try:
        tokens = Lexer(text).tokenize()
    except Exception as exc:  # pragma: no cover — lexer never fails on file text
        warnings.warn(f"i18n scanner: {loc_base} skipped ({exc})", RuntimeWarning)
        return

    def _build_msg(parts: list) -> str:
        out = []
        for kind, contents in parts:
            if kind == TokenType.TEXT:
                out.append(contents.replace("%", "%%"))
            else:
                out.append(f"%({contents.strip()})s")
        return "".join(out)

    lineno = 1
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        lineno += 1 if tok.token_type == TokenType.TEXT else tok.contents.count("\n")
        if tok.token_type != TokenType.BLOCK:
            i += 1
            continue
        bits = tok.contents.strip().split()
        tag = bits[0] if bits else ""
        if tag in _SINGLE_TAGS:
            import re as _re

            m = _re.match(
                r"trans\w*\s+(?P<q>['\"])(?P<msg>.*?)(?P=q)"
                r"(?:\s+context\s+(?P<qc>['\"])(?P<ctx>.*?)(?P=qc))?\s*$",
                tok.contents.strip(),
            )
            if m:
                index.add(m.group("ctx") or "", m.group("msg"), f"{loc_base}:{lineno}")
        elif tag in _BLOCK_TAGS:
            import re as _re

            head = tok.contents.strip()
            trimmed = "trimmed" in bits
            ctx = None
            mc = _re.search(r"context\s+(?P<q>['\"])(?P<ctx>.*?)(?P=q)", head)
            if mc:
                ctx = mc.group("ctx")
            singular_parts: list = []
            plural_parts: list = []
            target = [singular_parts]
            i += 1
            valid = False
            while i < len(tokens):
                t2 = tokens[i]
                if t2.token_type in (TokenType.TEXT, TokenType.VAR):
                    lineno += t2.contents.count("\n")
                    target[-1].append((t2.token_type, t2.contents))
                    i += 1
                    continue
                if t2.token_type == TokenType.BLOCK:
                    inner = t2.contents.strip()
                    if inner == "plural" and len(target) == 1:
                        lineno += 1
                        target.append(plural_parts)
                        i += 1
                        continue
                    if inner in ("end" + tag, "endblocktrans", "endblocktranslate"):
                        valid = True
                        break
                    # Some other block tag nested inside — malformed for
                    # i18n purposes (runtime would not accept it either).
                    break
                i += 1
            if valid and singular_parts:
                lineno += 1
                msg = _build_msg(singular_parts)
                if trimmed:
                    msg = trim_whitespace(msg)
                index.add(ctx or "", msg, f"{loc_base}:{lineno}")
                if plural_parts:
                    pmsg = _build_msg(plural_parts)
                    if trimmed:
                        pmsg = trim_whitespace(pmsg)
                    index.add(ctx or "", pmsg, f"{loc_base}:{lineno}")
                    index.add_pair((ctx or "", msg), (ctx or "", pmsg))
        i += 1


def enumerate_source_msgids(roots=None, apps=None) -> MsgidIndex:
    """Enumerate every gettext-marker msgid in the (bounded) source tree.

    Returns a MsgidIndex keyed (context, msgid) with file:line locations.
    Pure stdlib + Django's own template lexer; no settings needed, no I/O
    beyond reading the source files it owns scope for.
    """
    index = MsgidIndex()
    for path in _source_files(roots=roots, apps=apps):
        rel = path.relative_to(REPO_ROOT) if REPO_ROOT in path.parents else path
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if path.suffix == ".py":
            _py_extract(text, index, str(rel))
        else:
            _html_extract(text, index, str(rel))
    return index


# --- sw catalog readers -----------------------------------------------------
def load_sw_po():
    """Parse locale/sw/LC_MESSAGES/django.po. polib-only, no msgfmt needed."""
    try:
        import polib
    except ImportError as exc:  # pragma: no cover
        raise ImportError(
            "polib is required to read the sw catalog (pip install polib)."
        ) from exc
    return polib.pofile(str(SW_PO_PATH))


def po_msgids(pofile) -> set[tuple[str, str]]:
    return {(entry.msgctxt or "", entry.msgid) for entry in pofile}


def po_fuzzy_keys(pofile) -> set[tuple[str, str]]:
    return {
        (entry.msgctxt or "", entry.msgid) for entry in pofile if "fuzzy" in entry.flags
    }


def missing_sw_translations(index: MsgidIndex | None = None) -> list[dict]:
    """msgids present in code but with NO catalog record -> the drift list.

    An EMPTY msgstr is intentionally NOT "missing": untranslated-but-tracked
    strings are the honest partial state and render English fallback. Only a
    completely absent catalog record (the code changed, the catalog did not)
    is drift and must fail the suite.
    """
    index = index if index is not None else enumerate_source_msgids()
    try:
        pofile = load_sw_po()
    except (ImportError, FileNotFoundError, OSError) as exc:
        raise RuntimeError(f"cannot read sw catalog: {exc}") from exc
    have = po_msgids(pofile)
    missing = []
    for key in sorted(index.locations):
        if key not in have:
            missing.append(
                {
                    "msgid": key[1],
                    "context": key[0],
                    "files": sorted(index.locations[key]),
                }
            )
    return missing


def translation_status(pofile=None) -> dict:
    """Catalog-level honesty metrics for the UI (`lang_prefs_status` view).

    Only reads the .po (fast; no source walk). Convention:
    translated = has a msgstr (fuzzy ones count too but are flagged),
    needs_review = fuzzy flag (machine/agent-authored, pending human),
    untranslated = empty msgstr (renders English fallback at runtime).
    """
    pofile = pofile if pofile is not None else load_sw_po()
    total = len(pofile)

    def _has_sw(entry) -> bool:
        if entry.msgstr:
            return True
        plural = getattr(entry, "msgstr_plural", None)
        return bool(plural) and any(plural.values())

    translated = sum(1 for e in pofile if _has_sw(e))
    untranslated = total - translated
    # needs review = fuzzy flag AND a filled translation worth checking;
    # fuzzy-but-empty is just untranslated, nothing to review yet.
    needs_review = sum(
        1 for e in pofile if "fuzzy" in e.flags and _has_sw(e)
    )
    reviewed = translated - needs_review
    coverage = round((reviewed / total) * 100, 1) if total else 0.0
    return {
        "total_entries": total,
        "translated": translated,
        "reviewed": max(reviewed, 0),
        "needs_review": needs_review,
        "untranslated": untranslated,
        # % of all entries a human reviewer has cleared (needs_review is
        # NOT counted as usable): the honest "usable in Kiswahili" figure.
        "coverage_percent": coverage,
    }


def refresh_sw_po(dry_run: bool = True) -> dict:
    """Merge new source msgids into the sw catalog (the drift REMEDIATION).

    Adds an empty record for every code msgid the catalog lacks (never
    invents a translation), so the suite can go green and the community
    can fill translations in review. Run with dry_run=False to write.
    Returns {'added': n, 'missing': [...]}-style report.
    """
    index = enumerate_source_msgids()
    gaps = missing_sw_translations(index)
    report = {"scanned": len(index), "missing": len(gaps), "added": 0, "dry_run": dry_run}
    if not gaps or dry_run:
        report["msgids"] = [g["msgid"] for g in gaps]
        return report
    pofile = load_sw_po()
    for gap in gaps:
        pofile.append(
            _polib().POEntry(msgid=gap["msgid"], msgctxt=gap["context"] or None, msgstr="")
        )
        report["added"] += 1
    pofile.save(str(SW_PO_PATH))
    report["msgids"] = [g["msgid"] for g in gaps]
    return report


def _polib():
    try:
        import polib
    except ImportError as exc:  # pragma: no cover
        raise ImportError("polib is required to write catalogs (pip install polib).") from exc
    return polib
