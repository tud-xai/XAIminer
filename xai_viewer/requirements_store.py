"""Per owner+dataset requirements catalogs.

The audit criteria used to be one hard-coded file for everybody. They are now editable *per
owner and dataset*: the master (``requirements.toml``) is read-only and acts as the default;
the first edit creates a private copy ``requirements_<owner>_<dataset>.toml`` beside the other
server-side stores, and "reset to default" deletes that copy again.

Why per owner *and* dataset: the criteria one auditor applies to a dataset are their own working
material — two people auditing the same dataset may legitimately word them differently, and the
same person auditing a different dataset needs different criteria.

The key is the *owner* (``auth.owner_slug``), not the typed name — see ``notes.py``. Copies
written before owners existed are named after the bare user name; ``path_for`` falls back to
that name so an existing local catalog is not orphaned by the change.

Deliberately free of Flask imports (like ``notes.py``): a path in, text out. Parsing/validation
lives in ``requirements_catalog``; this module only decides *which* source applies and writes it.
"""

import re
import tomllib
from pathlib import Path

from auth import account_of, is_account, owner_slug
from requirements_catalog import parse_requirements

# File name pattern of a private copy. The slug keeps the name filesystem-safe and unambiguous
# (see auth.owner_slug); dataset names come from a directory listing.
FILENAME = "requirements_{owner}_{dataset}.toml"


def _slug(text: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", (text or "").strip()).strip("-").lower()
    return slug or "unknown"


class RequirementsStore:
    """Resolves the active catalog source for an (owner, dataset) pair and stores private copies."""

    def __init__(self, master_path, directory):
        self.master_path = Path(master_path)
        self.directory = Path(directory)

    # ── paths ──────────────────────────────────────────────────────────────────

    def path_for(self, owner: str, dataset: str) -> Path:
        """Where this owner's copy lives. For an account whose pre-owner file still exists and
        whose new one does not, that legacy path is returned — the catalog stays where it is
        until the next save, which then writes it under the new name."""
        path = self.directory / FILENAME.format(owner=owner_slug(owner), dataset=_slug(dataset))
        if not path.exists() and is_account(owner):
            legacy = self.directory / FILENAME.format(owner=_slug(account_of(owner)),
                                                      dataset=_slug(dataset))
            if legacy.exists():
                return legacy
        return path

    def is_custom(self, owner: str, dataset: str) -> bool:
        """Whether this owner+dataset has its own copy (i.e. the master is not in effect)."""
        return self.path_for(owner, dataset).exists()

    # ── reading ────────────────────────────────────────────────────────────────

    def master_source(self) -> str:
        return self.master_path.read_text(encoding="utf-8") if self.master_path.exists() else ""

    def source(self, owner: str, dataset: str) -> str:
        """Editable source text: the private copy if there is one, else the master."""
        path = self.path_for(owner, dataset)
        return path.read_text(encoding="utf-8") if path.exists() else self.master_source()

    def catalog(self, owner: str, dataset: str) -> tuple:
        """``(catalog, warnings)`` of the active source. A private copy that became invalid TOML
        (edited outside the app) degrades to an empty catalog with a ``syntax`` warning rather
        than breaking every note dialog."""
        try:
            return parse_requirements(self.source(owner, dataset))
        except tomllib.TOMLDecodeError as exc:
            return [], [{"code": "syntax", "message": str(exc)}]

    # ── writing ────────────────────────────────────────────────────────────────

    def save(self, owner: str, dataset: str, source: str) -> tuple:
        """Validates and stores a private copy.

        Returns ``(catalog, warnings, error)``. ``error`` is the TOML message when the source does
        not parse — nothing is written in that case. Warnings (duplicate label, missing language)
        do not prevent saving; they are shown in the dialog.
        """
        try:
            catalog, warnings = parse_requirements(source)
        except tomllib.TOMLDecodeError as exc:
            return [], [], str(exc)
        path = self.path_for(owner, dataset)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")
        return catalog, warnings, None

    def delete_owner(self, owner: str) -> int:
        """Removes every catalog of one owner (all datasets) — the guest-sandbox sweep. Returns
        how many files were deleted."""
        pattern = FILENAME.format(owner=owner_slug(owner), dataset="*")
        removed = 0
        for path in self.directory.glob(pattern):
            path.unlink()
            removed += 1
        return removed

    def reset(self, owner: str, dataset: str) -> bool:
        """Deletes the private copy (the master applies again). True if one existed."""
        path = self.path_for(owner, dataset)
        if not path.exists():
            return False
        path.unlink()
        return True
