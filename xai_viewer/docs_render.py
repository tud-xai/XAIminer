"""Render the Markdown guides in ``xai_viewer/docs/`` as pages of the application.

The guides are written for the repository first: readable on the git host, with relative links
and image paths. This module adapts them for the browser:

- image paths ``../static/…`` become URLs of the static folder,
- the overview figure follows the UI language (one SVG per language),
- links between the guides point to their routes; links to other repository files (README,
  DATA.md …) are not served by the app and are reduced to their text.

Heading anchors are generated the way GitHub/Gitea do it, so one set of in-document links works
in both places.
"""

import re
from functools import lru_cache
from pathlib import Path

import markdown

DOCS_DIR = Path(__file__).resolve().parent / "docs"

# Route name (/docs/<page>) → guide file. "overview" is the target of the help button.
PAGES = {"overview": "usage.md", "setup": "setup.md"}
_PAGE_OF_FILE = {filename: page for page, filename in PAGES.items()}

_MD_LINK = re.compile(r'<a href="([^"#]*\.md)(#[^"]*)?">(.*?)</a>', re.S)


def github_slug(value: str, separator: str = "-") -> str:
    """Heading anchor as GitHub/Gitea generate it: lower case, punctuation dropped (Unicode
    letters such as umlauts kept), every space becomes a hyphen."""
    value = re.sub(r"[^\w\- ]", "", value.strip().lower())
    return value.replace(" ", separator)


def render(page: str, lang: str, static_prefix: str, page_url_pattern: str) -> tuple[str, str]:
    """(title, html) of a guide. ``page_url_pattern`` is a URL containing ``{page}``."""
    path = DOCS_DIR / PAGES[page]
    return _render(path, path.stat().st_mtime_ns, lang, static_prefix, page_url_pattern)


@lru_cache(maxsize=16)
def _render(path: Path, _mtime_ns: int, lang: str, static_prefix: str,
            page_url_pattern: str) -> tuple[str, str]:
    source = path.read_text(encoding="utf-8")
    source = source.replace("](../static/", f"]({static_prefix}")
    source = source.replace("/docs/overview-de.svg", f"/docs/overview-{lang}.svg")

    md = markdown.Markdown(extensions=["tables", "fenced_code", "toc"],
                           extension_configs={"toc": {"slugify": github_slug}})
    html = md.convert(source)

    def relink(match):
        href, anchor, text = match.group(1), match.group(2) or "", match.group(3)
        page = _PAGE_OF_FILE.get(href)
        if page is None:
            return text                       # a repository file the app does not serve
        return f'<a href="{page_url_pattern.format(page=page)}{anchor}">{text}</a>'

    html = _MD_LINK.sub(relink, html)
    html = html.replace("<table>", '<table class="table table-sm table-bordered w-auto">')
    title = md.toc_tokens[0]["name"] if md.toc_tokens else path.stem
    return title, html
