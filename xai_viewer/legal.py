"""Operator-specific legal texts: imprint, the operator part of the privacy policy, contact link
(plus an optional announcement on the login page — also the operator's, not the application's).

Whoever runs an instance answers for its imprint and privacy policy, so these texts are NOT part
of the i18n catalogues — those describe the *application* and are the same for every instance.
They live in a separate TOML file per deployment instead:

    XAI_LEGAL_FILE (environment) → [paths] legal_file (config.toml) → legal.toml beside app.py

``legal.example.toml`` is the annotated template. What the application itself does (session
cookie, stored content, guest retention, no third-party content) stays in the catalogues: it has
to follow the code, not the operator.

Without a file the pages still answer (an imprint route must never be what breaks), but say that
nothing is configured, and the contact links disappear. Deliberately free of Flask imports.
"""

import re
import tomllib
from datetime import date
from pathlib import Path

# "[to be filled in]" — a value the operator has not replaced yet. The pages carry a draft notice
# exactly while one of these is left in their section.
PLACEHOLDER_RE = re.compile(r"\[[^\]]+\]")


def load_legal(path: Path) -> dict:
    """``{lang: {...}}`` from the legal file. Missing file -> ``{}`` (pages say "not configured").

    A file that exists but does not parse raises: a silently vanished imprint on a public instance
    is worse than a server that refuses to start and says why."""
    if not path.exists():
        return {}
    with open(path, "rb") as f:
        data = tomllib.load(f)
    return {lang: section for lang, section in data.items() if isinstance(section, dict)}


def for_lang(legal: dict, lang: str, fallback: str = "de") -> dict:
    """The section for ``lang``; else the fallback language; else any; else ``{}``."""
    if lang in legal:
        return legal[lang]
    if fallback in legal:
        return legal[fallback]
    return next(iter(legal.values()), {})


def announcement(section: dict, today: date) -> dict | None:
    """The login page's ``announcement`` (e.g. an upcoming event) while it is current, else None.

    ``show_until`` (a TOML date, inclusive) retires it on its own, so a past event does not linger
    on the start page until somebody remembers to delete it."""
    item = section.get("announcement")
    if not item or not item.get("title"):
        return None
    until = item.get("show_until")
    if isinstance(until, date) and today > until:
        return None
    return item


def placeholders(value) -> list:
    """Every string in ``value`` (walked recursively) that still carries a ``[placeholder]``.

    Lists are walked element by element on purpose: ``str(a_list)`` is itself wrapped in square
    brackets, so matching against it would report every list as unfilled."""
    if isinstance(value, str):
        return [value] if PLACEHOLDER_RE.search(value) else []
    if isinstance(value, dict):
        return [text for v in value.values() for text in placeholders(v)]
    if isinstance(value, list):
        return [text for v in value for text in placeholders(v)]
    return []
