"""Lightweight internationalisation (DE+EN).

Deliberately *not* Flask-Babel/gettext: fits the build-free, HTMX-/Python-centred style.
Language catalogues live as TOML files under ``i18n/<lang>.toml`` and are flattened into
dot-keyed dicts on load (``filter.cat.umgebung.label``). ``t()`` and ``nt()`` read the language
from the Flask session and interpolate via ``str.format``; both are usable as Jinja globals *and*
as Python functions. Since HTMX partials render server-side, the session language applies
automatically for full and OOB renders – no client state needed.
"""

import tomllib
from pathlib import Path

from flask import session

I18N_DIR = Path(__file__).resolve().parent / "i18n"

# Display names of the languages (order = order in the language switcher).
LANGUAGES = {"de": "Deutsch", "en": "English"}
DEFAULT_LANG = "de"  # German for now (English default will follow once the EN catalogue is reviewed)


def _flatten(d: dict, prefix: str = "") -> dict:
    """Nested tables → flat dot-keys ({'a': {'b': 1}} → {'a.b': 1})."""
    flat = {}
    for key, value in d.items():
        full = f"{prefix}{key}"
        if isinstance(value, dict):
            flat.update(_flatten(value, full + "."))
        else:
            flat[full] = value
    return flat


def _load_catalog(lang: str) -> dict:
    with open(I18N_DIR / f"{lang}.toml", "rb") as f:
        return _flatten(tomllib.load(f))


CATALOGS = {lang: _load_catalog(lang) for lang in LANGUAGES}


def get_lang() -> str:
    """Current session language (with fallback to the default)."""
    lang = session.get("lang", DEFAULT_LANG)
    return lang if lang in LANGUAGES else DEFAULT_LANG


def t(key: str, default: str = None, **kwargs) -> str:
    """Translates a key into the session language. Fallback: default language → ``default`` → key.
    If kwargs are given, the result is interpolated via ``str.format``; invalid placeholders → raw."""
    text = CATALOGS.get(get_lang(), {}).get(key)
    if text is None:
        text = CATALOGS[DEFAULT_LANG].get(key, key if default is None else default)
    if kwargs:
        try:
            return text.format(**kwargs)
        except (KeyError, IndexError, ValueError):
            return text
    return text


def t_variants(key: str) -> list:
    """All languages' texts for one key as ``(lang, text)`` pairs, in ``LANGUAGES`` order.

    For labels that must not move the layout when the language changes: the template renders
    every variant stacked in one box, so its width is that of the longest variant regardless of
    which language happens to be the longest one (see ``partials/_stable_label.html``)."""
    texts = []
    for lang in LANGUAGES:
        text = CATALOGS[lang].get(key)
        if text is None:
            text = CATALOGS[DEFAULT_LANG].get(key, key)
        texts.append((lang, text))
    return texts


def nt(n: int, key: str, **kwargs) -> str:
    """Plural helper with ngettext-like signature: selects ``<key>.one`` (n==1) or ``<key>.other``
    and provides ``n`` as a placeholder. Intentionally simple (DE/EN: 1 = singular, else plural)."""
    return t(f"{key}.{'one' if n == 1 else 'other'}", n=n, **kwargs)


def init_app(app):
    """Registers t/nt/t_variants/get_lang/LANGUAGES as Jinja globals."""
    app.jinja_env.globals.update(
        t=t, nt=nt, t_variants=t_variants, get_lang=get_lang, LANGUAGES=LANGUAGES
    )
