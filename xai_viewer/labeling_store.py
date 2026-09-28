"""Hand labels for the filter attributes: label store + category parsing.

Hand labels make the *filter* attributes (environment, objects, time of day, weather) of a
dataset real instead of mocked; ``tools/build_metadata.py`` reads them via ``parse_categories()``.

Like ``notes.py`` and ``filter_options.py`` this module is free of Flask imports.

**Categories** are user data, not code: they are stored as a *text* in a Markdown-ish syntax
(``# Heading`` starts a group, every other non-empty line is a selectable property) and parsed
into groups. Headings and properties carry a *token* derived from the text (``slugify``); the
tokens are what the export writes, so the default text is chosen such that its tokens reproduce
exactly the keys/tokens of ``filter_options.FILTER_CATEGORIES`` (asserted by a test).

**Semantics**: every property is an independent checkbox — multi-valued in every group (unlike
the filters, where e.g. ``umgebung`` is single-valued; sun *and* snow can coexist in reality).
Editing the category text *migrates* the existing labels (2026-08-15; before that it discarded
them wholesale, which was simple but hostile): a tick survives as long as its property still
exists — under a different heading too, since headings are only layout. See ``diff_properties``
for how renames are recognised. The "unclear" marks are independent of the category set and are
always kept.
"""

import json
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path

# Default category text: mirrors filter_options.FILTER_CATEGORIES. The wording is chosen so that
# slugify() reproduces the existing keys/tokens 1:1 ("Gebäude" -> gebaeude, "Überland" ->
# ueberland). Exported files then drop straight into the metadata pipeline
# (tools/build_metadata.py reads them as originals/manual_labels.json).
DEFAULT_CATEGORIES_TEXT = """# Umgebung
Überland
Semi-Urban
Stadt
Bahnhof
Tunnel
Bahnübergang

# Objekte
Mensch
Gebäude
Signal
Andere Züge
Überwiegend Vegetation
Gewässer
Schutzwand
Brücke

# Tageszeit
Tag
Dämmerung
Nacht
Indoor

# Wetter
Sonnig
Bewölkt
Regen
Schnee
Nebel
Gegenlicht
"""

# File name of a dataset's store, below the app directory. Shared with tools/build_metadata.py, which
# can read the store directly so that labelling and rebuilding metadata need no export in between.
STORE_NAME = "labeling_{dataset}.json"


def store_path(base_dir, dataset: str) -> Path:
    """Where the labels of one dataset live (gitignored user data, never deployed)."""
    return Path(base_dir) / STORE_NAME.format(dataset=dataset)


# Navigation modes: all images, or only those frozen into the "unclear" review sequence.
MODE_ALL = "all"
MODE_REVIEW = "review"

_UMLAUTS = {"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"}


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def slugify(text: str) -> str:
    """Display text -> token ("Gebäude" -> "gebaeude"). Empty string if nothing remains."""
    slug = text.strip().lower()
    for umlaut, replacement in _UMLAUTS.items():
        slug = slug.replace(umlaut, replacement)
    slug = re.sub(r"[^a-z0-9]+", "_", slug)
    return slug.strip("_")


def parse_categories(text: str) -> tuple:
    """Parses the category text into ``(groups, errors)``.

    groups: ``[{"key", "label", "options": [{"token", "label"}, ...]}, ...]``
    errors: ``[{"code", ...}]`` — UI-facing wording lives in the i18n catalogue (``labeling.err.*``),
    this module stays translation-free. Non-empty errors mean "do not save".
    """
    groups, errors = [], []
    # Property tokens must be unique across the WHOLE catalog, not just within a heading: a
    # property is identified by its own name (headings are a layout dimension), so a name used
    # twice would make the migration of existing labels ambiguous.
    seen_tokens = set()
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            label = line.lstrip("#").strip()
            key = slugify(label)
            if not key:
                errors.append({"code": "empty_slug", "line": lineno, "text": line})
                continue
            if any(g["key"] == key for g in groups):
                errors.append({"code": "duplicate_group", "line": lineno, "text": label})
                continue
            groups.append({"key": key, "label": label, "options": []})
            continue
        if not groups:
            errors.append({"code": "before_heading", "line": lineno, "text": line})
            continue
        token = slugify(line)
        if not token:
            errors.append({"code": "empty_slug", "line": lineno, "text": line})
            continue
        if token in seen_tokens:
            errors.append({"code": "duplicate_option", "line": lineno, "text": line})
            continue
        seen_tokens.add(token)
        groups[-1]["options"].append({"token": token, "label": line})

    if not groups:
        errors.append({"code": "empty"})
    for group in groups:
        if not group["options"]:
            errors.append({"code": "empty_group", "text": group["label"]})
    return groups, errors


def properties(groups: list) -> list:
    """All selectable properties in text order: ``[{"token", "label", "group"}, ...]``."""
    return [{**option, "group": group["key"]} for group in groups for option in group["options"]]


def diff_properties(old_groups: list, new_groups: list) -> dict:
    """What an edit does to the properties: ``{"added", "removed", "renamed"}``.

    Identity is the property token itself, *independent of its heading* — headings are a layout
    dimension (see the module docstring), so moving a property under a different heading is not
    a change at all and keeps its labels.

    Renames cannot be told apart from "delete one, add another" by looking at text. Heuristic:
    when exactly as many properties disappear as appear, they are paired in text order and
    treated as renames, so the usual case (fixing a wording) keeps its ticks. Deliberately
    computed on *sets*, not on line positions: reordering lines would otherwise look like a
    batch of renames and would swap the ticks between properties. The pairing is never silent —
    the editing dialog shows it before saving (see labeling.py).
    """
    old_props, new_props = properties(old_groups), properties(new_groups)
    old_tokens = {p["token"] for p in old_props}
    new_tokens = {p["token"] for p in new_props}
    removed = [p for p in old_props if p["token"] not in new_tokens]
    added = [p for p in new_props if p["token"] not in old_tokens]
    if removed and len(removed) == len(added):
        return {"added": [], "removed": [], "renamed": list(zip(removed, added))}
    return {"added": added, "removed": removed, "renamed": []}


class LabelingStore:
    """Per-dataset labelling state in a single JSON file.

    Layout: ``{categories_text, labels: {image_id: {group_key: [token, ...]}}, unclear: [image_id],
    notes: {image_id: text}, cursor: {all, review}, mode, review_seq: [image_id]}``. Like
    ``NotesStore`` the file is re-read on every access — trivially correct at demonstrator scale
    and robust against dev-server restarts.

    ``unclear`` and ``notes`` are per-image working material *beside* the categories: they neither
    depend on the category set (an edit never touches them) nor on each other (a note does not
    imply the unclear mark).
    """

    def __init__(self, path, dataset: str = ""):
        self.path = Path(path)
        self.dataset = dataset

    # ── file handling ──────────────────────────────────────────────────────────

    def _default_state(self) -> dict:
        return {
            "dataset": self.dataset,
            "categories_text": DEFAULT_CATEGORIES_TEXT,
            "labels": {},
            "unclear": [],
            "notes": {},
            "cursor": {MODE_ALL: 0, MODE_REVIEW: 0},
            "mode": MODE_ALL,
            "review_seq": [],
            "updated_at": _now(),
        }

    def load(self) -> dict:
        """Full state with all keys present (missing ones filled from the defaults)."""
        state = self._default_state()
        if self.path.exists():
            with open(self.path, encoding="utf-8") as f:
                stored = json.load(f)
            state.update({k: v for k, v in stored.items() if k in state})
            cursor = dict(self._default_state()["cursor"])
            cursor.update(stored.get("cursor") or {})
            state["cursor"] = cursor
        return state

    def _save(self, state: dict):
        """Atomic write (tmp file + rename) so a crash never leaves a torn file."""
        state["updated_at"] = _now()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    # ── categories ─────────────────────────────────────────────────────────────

    def categories(self) -> list:
        """Parsed groups of the stored text (errors are ignored here — the text was validated
        before it was stored)."""
        return parse_categories(self.load()["categories_text"])[0]

    def categories_text(self) -> str:
        return self.load()["categories_text"]

    def plan_categories(self, text: str) -> dict:
        """What saving ``text`` would do, without doing it — the dialog's preview.

        ``{"errors", "added", "removed", "renamed", "affected": {token: image_count}}``.
        ``affected`` counts, per removed *and* per renamed property, how many images carry a tick
        for it: those are the ticks that would be lost (removed) or carried over (renamed).
        """
        new_groups, errors = parse_categories(text)
        if errors:
            return {"errors": errors, "added": [], "removed": [], "renamed": [], "affected": {}}
        state = self.load()
        diff = diff_properties(parse_categories(state["categories_text"])[0], new_groups)

        counts = {}
        for entry in state["labels"].values():
            for tokens in entry.values():
                for token in tokens:
                    counts[token] = counts.get(token, 0) + 1
        touched = [p["token"] for p in diff["removed"]] + [old["token"] for old, _ in diff["renamed"]]
        return {"errors": [], **diff, "affected": {token: counts.get(token, 0) for token in touched}}

    def set_categories(self, text: str) -> list:
        """Validates and stores the category text, migrating the existing labels (see
        ``diff_properties``): ticks survive as long as their property still exists — renamed ones
        travel to the new name, deleted ones are dropped. The "unclear" marks are independent of
        the categories and always kept. Returns the (possibly empty) error list; a non-empty list
        means nothing was written."""
        new_groups, errors = parse_categories(text)
        if errors:
            return errors
        state = self.load()
        diff = diff_properties(parse_categories(state["categories_text"])[0], new_groups)
        renames = {old["token"]: new["token"] for old, new in diff["renamed"]}
        group_of = {p["token"]: p["group"] for p in properties(new_groups)}
        order = {p["token"]: i for i, p in enumerate(properties(new_groups))}

        migrated = {}
        for image_id, entry in state["labels"].items():
            # Re-keyed by the NEW group of each property: a property that moved under a different
            # heading keeps its ticks, they just live in another bucket now.
            tokens = {renames.get(token, token)
                      for group_tokens in entry.values() for token in group_tokens}
            kept = sorted((token for token in tokens if token in group_of), key=order.get)
            if not kept:
                continue
            record = {}
            for token in kept:
                record.setdefault(group_of[token], []).append(token)
            migrated[image_id] = record

        state["categories_text"] = text
        state["labels"] = migrated
        self._save(state)
        return []

    # ── labels ─────────────────────────────────────────────────────────────────

    def labels_for(self, image_id: str) -> dict:
        return self.load()["labels"].get(image_id, {})

    def set_label(self, image_id: str, key: str, token: str, on: bool):
        state = self.load()
        entry = state["labels"].setdefault(image_id, {})
        selected = entry.setdefault(key, [])
        if on and token not in selected:
            selected.append(token)
        elif not on and token in selected:
            selected.remove(token)
        self._save(state)

    def copy_labels(self, src_id: str, dst_id: str) -> dict:
        """Copies the labels of ``src_id`` onto ``dst_id`` (replacing them). The unclear mark and
        the note are NOT copied — they are per-image notes-to-self, not properties of the scene."""
        state = self.load()
        source = state["labels"].get(src_id, {})
        state["labels"][dst_id] = {k: list(v) for k, v in source.items()}
        self._save(state)
        return state["labels"][dst_id]

    def labeled_count(self) -> int:
        """Images carrying at least one selected property (the unclear mark alone does not count)."""
        return sum(1 for entry in self.load()["labels"].values() if any(entry.values()))

    # ── unclear marks ──────────────────────────────────────────────────────────

    def is_unclear(self, image_id: str) -> bool:
        return image_id in self.load()["unclear"]

    def set_unclear(self, image_id: str, on: bool):
        state = self.load()
        marked = set(state["unclear"])
        marked.add(image_id) if on else marked.discard(image_id)
        state["unclear"] = sorted(marked)
        self._save(state)

    # ── per-image notes ────────────────────────────────────────────────────────

    def note_for(self, image_id: str) -> str:
        return self.load()["notes"].get(image_id, "")

    def set_note(self, image_id: str, text: str):
        """Stores (or, for empty text, removes) the free-text note of one image. Independent of
        the unclear mark: an image can carry a note without being flagged, and vice versa."""
        state = self.load()
        text = text.strip()
        if text:
            state["notes"][image_id] = text
        else:
            state["notes"].pop(image_id, None)
        self._save(state)

    # ── navigation state ───────────────────────────────────────────────────────

    def mode(self) -> str:
        return self.load()["mode"]

    def set_mode(self, mode: str, all_ids: list):
        """Switches the navigation mode. Entering the review mode *freezes* the current set of
        unclear images: unticking the mark keeps the image in the sequence until the mode is left
        and re-entered (so the checkbox does not yank the image away under the cursor)."""
        state = self.load()
        state["mode"] = MODE_REVIEW if mode == MODE_REVIEW else MODE_ALL
        if state["mode"] == MODE_REVIEW:
            marked = set(state["unclear"])
            state["review_seq"] = [i for i in all_ids if i in marked]
            state["cursor"][MODE_REVIEW] = 0
        self._save(state)

    def sequence(self, all_ids: list) -> list:
        """Image ids to step through in the current mode."""
        state = self.load()
        if state["mode"] != MODE_REVIEW:
            return list(all_ids)
        known = set(all_ids)
        return [i for i in state["review_seq"] if i in known]

    def cursor(self, mode: str = None) -> int:
        state = self.load()
        return state["cursor"].get(mode or state["mode"], 0)

    def set_cursor(self, index: int, mode: str = None):
        state = self.load()
        state["cursor"][mode or state["mode"]] = max(0, index)
        self._save(state)

    # ── export ─────────────────────────────────────────────────────────────────

    def export(self, all_ids: list) -> dict:
        """Export payload for the download button.

        Shaped to drop into the metadata pipeline: per image one list per category key — the keys
        and tokens are those of ``metadata.json``'s ``attributes`` as long as the categories were
        not renamed (hence ``categories`` travels with the file, making it self-describing).
        All images are present; ``unlabeled`` names those without a single selected property, i.e.
        the ones that must stay mocked when the file is ingested.
        """
        state = self.load()
        groups = parse_categories(state["categories_text"])[0]
        marked = set(state["unclear"])
        images, unlabeled = {}, []
        for image_id in all_ids:
            entry = state["labels"].get(image_id, {})
            record = {g["key"]: list(entry.get(g["key"], [])) for g in groups}
            record["unclear"] = image_id in marked
            record["note"] = state["notes"].get(image_id, "")
            images[image_id] = record
            if not any(record[g["key"]] for g in groups):
                unlabeled.append(image_id)
        return {
            "schema": "xai-viewer-manual-labels/1",
            "dataset": state.get("dataset") or self.dataset,
            "exported_at": _now(),
            "categories": {
                g["key"]: {"label": g["label"], "options": g["options"]} for g in groups
            },
            "images": images,
            "unlabeled": unlabeled,
        }
