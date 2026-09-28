"""
Example content every demo visitor starts with (public demo, 2026-08-31).

An empty workspace is a poor first impression: a visitor sees panels and filters but has no
idea what the notes and the audit report are *for*. So a fresh sandbox is handed one or two
prepared notes — a real one, written in the app, that demonstrates the point of the feature.

**Copies, not shared originals.** Each owner receives their own copy, which they may edit and
delete like anything else. That keeps the rendering free of special cases ("this note is
read-only"), lets a visitor try deleting, and hands the next visitor a fresh set regardless.

Bound to one dataset: a note's reference points name concrete image ids, so a seed is only
copied into a project that opened the dataset it was written against. A seed for a dataset the
server does not have is simply never used.

Seeded **once per owner and dataset**, and only when that owner has no notes for it yet — so a
visitor who deletes the example is not handed it again on the next project.

The seed file (``demo_seed/notes.json``) is checked in: it is example content, not user data.
Write the note in the running app, then extract it with ``tools/make_demo_seed.py``.

The requirements catalogue needs no seed of its own — the read-only master
(``requirements.toml``) already applies to everyone until they edit it.

Deliberately free of Flask imports (like ``notes.py``).
"""

import json
from pathlib import Path

# Fields a seed note may carry. Anything else in the file is ignored rather than trusted into
# the store — the seed is data, and the store's schema is not its business.
SEED_FIELDS = ("title", "text", "refpoints", "user", "dataset", "include_in_report",
               "requirement_status")


def load(path) -> list:
    """The seed notes, or [] when there is no (or a broken) seed file.

    Never raises: example content is a nicety, and a typo in it must not stop anyone from
    entering the app.
    """
    path = Path(path)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    notes = data.get("notes") if isinstance(data, dict) else None
    if not isinstance(notes, list):
        return []
    return [n for n in notes if isinstance(n, dict) and n.get("title")]


def for_dataset(seeds: list, dataset: str) -> list:
    return [n for n in seeds if n.get("dataset") == dataset]


def apply_notes(store, seeds: list, owner: str, dataset: str, project: str) -> int:
    """Copies the seeds for ``dataset`` into ``store`` as this owner's own notes.

    Does nothing (returns 0) when the owner already has notes for that dataset — that covers
    both "already seeded" and "has their own work", and makes the call idempotent.
    """
    wanted = for_dataset(seeds, dataset)
    if not wanted or store.list_notes(owner, dataset=dataset):
        return 0
    for seed in wanted:
        fields = {k: v for k, v in seed.items() if k in SEED_FIELDS}
        store.create(
            title=fields.get("title", ""),
            text=fields.get("text", ""),
            refpoints=fields.get("refpoints") or [],
            owner=owner,
            # The name on the note stays the seed's own: the visitor did not write it.
            user=fields.get("user") or "Beispiel",
            # The project, on the other hand, is the one they just opened.
            project=project,
            dataset=dataset,
            include_in_report=bool(fields.get("include_in_report")),
            requirement_status=fields.get("requirement_status") or {},
        )
    return len(wanted)
