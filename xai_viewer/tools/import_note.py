#!/usr/bin/env python3
"""
Puts a note exported from one store into another account's notes.

For handing a colleague's note to a specific user on the deployed server: export the note from
the source ``notes.json``, copy the file over and import it here. The deployed store is excluded
from every upload (``deployment/deploy.py``), so short of writing it in the app by hand this is
the way a note gets there.

The note is *copied*, not moved: it receives a fresh id and fresh timestamps from the target
store and belongs to the target owner like anything that owner wrote themselves. The write goes
through ``NotesStore.create()`` — the same path the app takes — so the cross-process lock and the
atomic write apply and a running server needs no restart (the store re-reads the file on every
access). Editing the JSON by hand would have neither.

Accepted input: a whole store file (``{"next_id": …, "notes": […]}``), a ``{"notes": […]}``
wrapper, a bare note object, or a list of notes.

Run from `xai_viewer/`:

    python tools/import_note.py note.json                 # owner and author as in the file
    python tools/import_note.py note.json --account anna  # into anna's account instead
    python tools/import_note.py note.json --id 22         # pick one of several notes
    python tools/import_note.py note.json --dry-run

Reference points travel verbatim, which is what the self-contained types want (``workspace``,
``image``, ``model``, ``xai_method`` carry their own data). A ``collection`` refpoint is NOT
self-contained: it names a collection *id* of the source store, which in the target store means
nothing or, worse, something else. Such a note is refused unless ``--force``.
"""

import argparse
import json
import sys
import tomllib
from pathlib import Path

HERE = Path(__file__).resolve().parent
XAI_DIR = HERE.parent
# sys.path bootstrap for the flat app modules — see tools/build_metadata.py.
if str(XAI_DIR) not in sys.path:
    sys.path.insert(0, str(XAI_DIR))

from auth import account_owner          # noqa: E402
from notes import NotesStore, owner_of  # noqa: E402

# Fields that describe a note. Everything else in the input (id, timestamps, owner) identifies
# the *original* and is not carried over — same split as demo_seed.SEED_FIELDS.
COPIED_FIELDS = ("title", "text", "refpoints", "user", "project", "dataset",
                 "include_in_report", "requirement_status")


def _config() -> dict:
    """The app's own config.toml, for ``paths.notes_file`` (same source as app.py reads)."""
    path = XAI_DIR / "config.toml"
    if not path.exists():
        return {}
    with open(path, "rb") as f:
        return tomllib.load(f)


def notes_path(config: dict = None) -> Path:
    """Where the notes store lives — resolved exactly as app.py resolves it, so the tool cannot
    end up writing a file the app does not read."""
    configured = ((config if config is not None else _config()).get("paths", {})
                  .get("notes_file"))
    return Path(configured) if configured else XAI_DIR / "notes.json"


def read_notes(path) -> list:
    """The notes in an export file, whatever of the accepted shapes it has."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data = data.get("notes", data)  # a store file / wrapper, else a bare note
    notes = data if isinstance(data, list) else [data]
    return [n for n in notes if isinstance(n, dict) and n.get("title")]


def select(notes: list, nid: int = None) -> dict:
    """The one note to import. Ambiguity is an error, never a guess."""
    if not notes:
        sys.exit("No usable note in the input (a note needs at least a title).")
    if nid is None:
        if len(notes) > 1:
            listing = ", ".join(f"{n.get('id', '?')} ({n['title'][:40]!r})" for n in notes)
            sys.exit(f"{len(notes)} notes in the input — pick one with --id: {listing}")
        return notes[0]
    found = [n for n in notes if n.get("id") == nid]
    if not found:
        sys.exit(f"No note with id {nid} in the input.")
    return found[0]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0].strip())
    parser.add_argument("file", help="the export to import (see the module docstring)")
    parser.add_argument("--id", type=int, help="which note, when the input holds several")
    parser.add_argument("--account", help="target account (default: the owner in the input)")
    parser.add_argument("--author", help="displayed author (default: the one in the input)")
    parser.add_argument("--project", help="project label (default: the one in the input)")
    parser.add_argument("--dataset", help="dataset (default: the one in the input)")
    parser.add_argument("--notes-file", help="target store (default: as the app resolves it)")
    parser.add_argument("--force", action="store_true",
                        help="import despite a collection refpoint or an identical title")
    parser.add_argument("--dry-run", action="store_true", help="only report what would happen")
    args = parser.parse_args(argv)

    note = select(read_notes(args.file), args.id)
    fields = {k: v for k, v in note.items() if k in COPIED_FIELDS}

    if args.account:
        owner = account_owner(args.account)
    elif note.get("owner") or note.get("user"):
        owner = owner_of(note)
    else:
        sys.exit("The input names neither an owner nor an author — pass --account.")
    dataset = args.dataset or fields.get("dataset")
    if not dataset:
        # Notes are listed per owner AND dataset: one without a dataset would be invisible.
        sys.exit("The input names no dataset — pass --dataset.")

    dangling = [r for r in (fields.get("refpoints") or [])
                if isinstance(r, dict) and r.get("type") == "collection"]
    if dangling and not args.force:
        sys.exit(f"{len(dangling)} collection reference point(s): they name collections of the "
                 "source store, which do not exist here. Remove them, or accept the dangling "
                 "references with --force.")
    if fields.get("requirement_status"):
        print("NOTE: the verdicts are keyed by requirement label — they only read correctly if "
              "the target account's catalogue uses the same labels.")

    store = NotesStore(Path(args.notes_file) if args.notes_file else notes_path())
    title = fields.get("title", "")
    clash = [n for n in store.list_notes(owner, dataset=dataset) if n.get("title") == title]
    if clash and not args.force:
        # The tool is run by hand on a server; running it twice must not silently duplicate.
        sys.exit(f"{owner} already has a note titled {title!r} for dataset {dataset!r} "
                 f"(id {clash[0]['id']}). Use --force to import it a second time.")

    author = args.author or fields.get("user") or ""
    project = args.project or fields.get("project") or ""
    if args.dry_run:
        print(f"would import {title!r} for {owner} (dataset {dataset!r}, project {project!r}, "
              f"author {author!r}, {len(fields.get('refpoints') or [])} reference points) "
              f"into {store.path}")
        return None

    created = store.create(
        title=title,
        text=fields.get("text", ""),
        refpoints=fields.get("refpoints") or [],
        owner=owner,
        user=author,
        project=project,
        dataset=dataset,
        include_in_report=bool(fields.get("include_in_report")),
        requirement_status=fields.get("requirement_status") or {},
    )
    print(f"imported {title!r} as id {created['id']} for {owner} (dataset {dataset!r}, "
          f"project {project!r}) into {store.path}")
    return created


if __name__ == "__main__":
    main()
