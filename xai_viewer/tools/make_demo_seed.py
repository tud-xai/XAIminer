#!/usr/bin/env python3
"""
Turns a note you wrote in the app into a demo seed (see demo_seed.py).

Write the note in the running app until it is exactly what every visitor should find, then:

Run from `xai_viewer/`:

    python tools/make_demo_seed.py 1               # note id 1 from notes.json
    python tools/make_demo_seed.py 1 3 --author "Beispiel"
    python tools/make_demo_seed.py 7 --title "Clever Hans Effect" --text "..."

The note's id, owner, timestamps and project are dropped — those belong to the copy each
visitor gets, not to the template. Everything else (title, text, reference points, report flag
and verdicts) is taken verbatim, unless ``--title``/``--text`` override it: a note written for
one's own use rarely carries the wording a first-time visitor needs.

**Seeds are added, not replaced.** A dataset gets its examples over time and from different
sessions, and an extraction run for one dataset must not silently drop the seeds of another —
those cannot be recovered once their source note is gone from notes.json. A seed with the same
title and dataset is updated in place, so re-extracting an improved note is idempotent.
``--overwrite`` throws the file away and starts from the notes given, for when that is really
what is meant.

The result is written to ``demo_seed/notes.json``, which IS checked in: it is example content,
not user data. Editing that file by hand is fine — unknown keys are ignored by the loader, so a
``_comment`` explaining where a seed came from is allowed to stay in it.
"""

import argparse
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
XAI_DIR = HERE.parent
SEED_PATH = XAI_DIR / "demo_seed" / "notes.json"

# Fields that identify the ORIGINAL note rather than describing it.
DROPPED = ("id", "owner", "created_at", "updated_at", "project")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[1].strip())
    parser.add_argument("ids", nargs="+", type=int, help="note ids from notes.json")
    parser.add_argument("--author", default="Beispiel",
                        help="name shown as the note's author (default: %(default)s)")
    parser.add_argument("--title", default=None,
                        help="replace the note's title (only sensible with a single id)")
    parser.add_argument("--text", default=None, help="replace the note's body")
    parser.add_argument("--overwrite", action="store_true",
                        help="discard the existing seed file instead of adding to it")
    parser.add_argument("--notes-file", default=str(XAI_DIR / "notes.json"))
    parser.add_argument("--out", default=str(SEED_PATH))
    args = parser.parse_args(argv)
    if (args.title or args.text) and len(args.ids) > 1:
        raise SystemExit("--title/--text apply to one note; pass a single id")

    stored = json.loads(Path(args.notes_file).read_text(encoding="utf-8"))["notes"]
    by_id = {n["id"]: n for n in stored}
    missing = [i for i in args.ids if i not in by_id]
    if missing:
        raise SystemExit(f"No note with id {missing} in {args.notes_file}")

    seeds = []
    for nid in args.ids:
        note = {k: v for k, v in by_id[nid].items() if k not in DROPPED}
        note["user"] = args.author
        if args.title is not None:
            note["title"] = args.title
        if args.text is not None:
            note["text"] = args.text
        seeds.append(note)

    out = Path(args.out)
    existing = [] if args.overwrite else _read_seeds(out)
    kept, replaced = list(existing), 0
    for note in seeds:
        # Same title in the same dataset = the same example, extracted again.
        key = (note.get("dataset"), note.get("title"))
        for i, old in enumerate(kept):
            if (old.get("dataset"), old.get("title")) == key:
                kept[i], replaced = note, replaced + 1
                break
        else:
            kept.append(note)

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"notes": kept}, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    added = len(seeds) - replaced
    datasets = sorted({n.get("dataset", "?") for n in seeds})
    action = "Replaced file with" if args.overwrite else f"Added {added}, updated {replaced} of"
    print(f"{action} {len(seeds)} seed note(s) for dataset(s) {', '.join(datasets)} "
          f"— {out} now holds {len(kept)}.")


def _read_seeds(path: Path) -> list:
    """The seeds already in the file. A missing file is simply an empty list; a broken one is
    *not* — silently dropping seeds because of a typo is what this whole function prevents."""
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    return list(data.get("notes") or [])


if __name__ == "__main__":
    main()
