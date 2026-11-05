"""
Download of a person's own working data: all notes, all collections and the private
requirements catalogs, as ONE JSON document (2026-10-15).

One file rather than a ZIP: everything is JSON already, and the only non-JSON part — a private
requirements catalog — is a short TOML text that fits in a string field. One file is also what a
later import will want to take as a single upload.

The format is meant to be carried over to ANOTHER account (the import is not built yet), so:

- ``owner`` is dropped from every record: it is the server-assigned key of the exporting account
  (see ``auth.py``) and means nothing on the receiving side. ``user`` stays — it is the displayed
  author and is worth keeping as provenance.
- Ids are kept as they are. They are only unique per instance, so an importer must assign new
  ones and rewrite the references that point at collections: refpoints ``{"type": "collection",
  "id": N}`` and, inside ``workspace`` refpoints, panel ``image_source`` values
  ``{"type": "collection", "id": N}``. Every referenced collection of the owner is in the same
  file, so the mapping is always complete.
- Everything spans ALL datasets of the owner; each record carries its ``dataset``.
- A collection's optional ``quicksort_role`` (target of the key A/D, see collections_store.py)
  travels along, so the quick-sort setup can be restored together with the collections.
- Audit verdicts on notes are keyed by requirement labels (``A1``…). Which text a label stood for
  comes from the catalog in effect for that dataset; a private one is included under
  ``requirements`` (datasets without an entry used the master catalog).

    {"format": "xaiminer-export", "format_version": 1,
     "exported_at": "<iso>", "app_version": "<x.y.z>", "exported_by": "<display name>",
     "notes": [<note without owner>, ...],
     "collections": [<collection without owner>, ...],
     "requirements": [{"dataset": "<name>", "toml": "<catalog source>"}, ...]}

Deliberately free of Flask imports (like the stores it reads).
"""

from datetime import datetime

FORMAT = "xaiminer-export"
FORMAT_VERSION = 1


def _portable(record: dict) -> dict:
    """A copy of a store record without the account-specific owner key."""
    return {k: v for k, v in record.items() if k != "owner"}


def build_export(notes_store, collections_store, requirements_store, owner: str, user: str,
                 app_version: str) -> dict:
    """The export document for <owner> (see the module docstring for the format)."""
    notes = sorted(notes_store.list_notes(owner), key=lambda n: n["id"])
    collections = sorted(collections_store.list_collections(owner), key=lambda c: c["id"])
    datasets = sorted({r.get("dataset") for r in notes + collections if r.get("dataset")})
    requirements = [{"dataset": d, "toml": requirements_store.source(owner, d)}
                    for d in datasets if requirements_store.is_custom(owner, d)]
    return {
        "format": FORMAT,
        "format_version": FORMAT_VERSION,
        "exported_at": datetime.now().isoformat(timespec="seconds"),
        "app_version": app_version,
        "exported_by": user,
        "notes": [_portable(n) for n in notes],
        "collections": [_portable(c) for c in collections],
        "requirements": requirements,
    }
