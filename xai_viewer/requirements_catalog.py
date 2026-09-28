"""
Requirements catalog for the audit protocol.

A protocol entry (a special note type, see ``notes.py``) records, per catalog
requirement, whether that requirement is *met* or *unmet*. The catalog is a list of
bilingual criteria in a TOML file: a read-only master (``requirements.toml`` next to
this module, overridable via config) plus optional per user+dataset copies that users
edit in the app (see ``requirements_store.py``).

Each requirement carries an explicit, stable label (``label = "A5"``). That label — not
the position in the file — is what protocol entries store their verdicts under, so
reordering is harmless and deleting a requirement is unambiguous (2026-08-14; before
that labels were index-based, and deleting an entry silently shifted every following
requirement under an existing label). Entries without a ``label`` still fall back to
their position (``A1``..``An``), which keeps older files readable.

Deliberately free of Flask imports (like ``filter_options``/``notes``): the loader and the
parser are plain functions so they can be unit-tested and reused without an app context.
Both languages are optional individually — a missing one falls back to the other, since
users edit these files by hand; the omission is reported as a *warning* so the editing
dialog can show it. Warnings are structured (``code`` + data), never prose: the wording
lives in the i18n catalogue.
"""

import sys
import tomllib
from pathlib import Path

# Label prefix for requirements (A1, A2, ...). Kept short: it is shown in a dense
# checkbox table and referenced in the info dialog and the audit report.
LABEL_PREFIX = "A"


def label_for(index0: int) -> str:
    """Fallback label for a requirement without an explicit one (A1, A2, ... by position)."""
    return f"{LABEL_PREFIX}{index0 + 1}"


def _warn(msg: str) -> None:
    print(f"[requirements_catalog] WARNING: {msg}", file=sys.stderr)


def parse_requirements(source: str) -> tuple:
    """Parses catalog TOML source into ``(catalog, warnings)``.

    catalog: ``[{"label": "A1", "de": <str>, "en": <str>}, ...]`` in file order.
    warnings: ``[{"code": ..., ...}]`` — ``duplicate_label`` (only the FIRST entry with a label
    counts), ``missing_text`` (entry without any text, skipped), ``missing_lang`` (one language
    filled in for the other), ``empty`` (no requirements at all).

    Raises ``tomllib.TOMLDecodeError`` on a syntax error — the caller decides whether that is
    a hard error (editing dialog) or a degraded load (startup).
    """
    data = tomllib.loads(source)
    raw = data.get("requirement", [])
    catalog, warnings, seen = [], [], set()

    for i, entry in enumerate(raw):
        label = str(entry.get("label") or "").strip() or label_for(i)
        de = (entry.get("de") or "").strip()
        en = (entry.get("en") or "").strip()

        if not de and not en:
            warnings.append({"code": "missing_text", "label": label, "index": i + 1})
            continue
        if label in seen:
            # First occurrence wins: dropping both would lose data, merging would be guesswork.
            warnings.append({"code": "duplicate_label", "label": label, "index": i + 1})
            continue
        if not de or not en:
            warnings.append({"code": "missing_lang", "label": label,
                             "lang": "de" if not de else "en"})
        seen.add(label)
        catalog.append({"label": label, "de": de or en, "en": en or de})

    if not catalog:
        warnings.append({"code": "empty"})
    return catalog, warnings


def load_requirements(path) -> list:
    """Loads a catalog file. A missing file or a syntax error yields an empty catalog with a CLI
    warning — a broken catalog degrades to "no requirements" rather than taking the app down."""
    path = Path(path)
    if not path.exists():
        _warn(f"catalog file not found: {path} — protocol entries will have no requirements")
        return []
    try:
        catalog, warnings = parse_requirements(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        _warn(f"catalog file {path} is not valid TOML ({exc}) — protocol entries will have "
              f"no requirements")
        return []
    for w in warnings:
        _warn(f"{path.name}: {w}")
    return catalog


def text_for(catalog: list, lang: str) -> dict:
    """Maps each requirement label to its text in ``lang`` (fallback: the other language)."""
    other = "en" if lang == "de" else "de"
    return {r["label"]: (r.get(lang) or r.get(other) or r["label"]) for r in catalog}
