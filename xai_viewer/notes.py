"""
Notes with n:m reference points, optionally flagged as audit-report entries.

A note holds free text plus any number of *reference points* (Bezugspunkte). Any note can be
marked as an *audit-report entry* (``include_in_report``): such notes appear in the audit
report and may additionally record — keyed by requirements-catalog label (``A1``…, see
``requirements_catalog``) — whether each assessed requirement is met or unmet. The two are
one entity distinguished by a single flag, not separate types (the UI toggles it with a
checkbox). Reference
points are generic dicts with a ``type`` discriminator so further types can be added
without schema changes:

    {"type": "image", "image_id": "<id>", "xai_method": "original"|<method>,
     "model": ..., "level": ...}   # model/level only when known (panel suggestion)
    {"type": "workspace", "panels": [{panel_id, model, level, xai_method, filters,
                                      image_source, visible, view, current_image}, ...]}
    {"type": "model", "model": "<name>"}
    {"type": "xai_method", "method": "<name>"}
    {"type": "collection", "id": <collection id>}

``image`` references a CONCRETE image variant: the base image id plus whether the original
or an XAI rendering is meant (and for XAI which model/level/method). Free-text image ids
have no display context and are stored as originals. Legacy image refpoints without
``xai_method`` are treated as originals on load.

``collection`` references a stored collection *by id* (not by copying its ids): the note is
about "this curated set", and the set is expected to stay what it was. That expectation is
enforced outside this module — a referenced collection is frozen against destructive management
operations, which clone it instead (see ``_collection_edit_target`` in app.py). Note that a
``workspace`` snapshot may also mention a collection (in a panel's ``image_source``); such an
incidental mention deliberately does NOT freeze anything, otherwise nearly every collection
would end up frozen.

``workspace`` is a *snapshot* (copy) of the entire panel configuration at note-creation
time — which panels exist, how each is configured, and the concrete image each panel
displays (per-panel ``current_image`` = {image_id, rel}; ``rel`` is the displayed variant,
e.g. the Grad-CAM rendering, not just the base image). Panels are ephemeral session state,
so referencing them by id would dangle. (The legacy per-panel type ``panel_config`` may
still occur in stored data and is only rendered, never created.)

**Ownership** (2026-08-30): every note belongs to an ``owner`` — the server-assigned key from
``auth.py``, not the typed display name, which stays in ``user`` and is only ever displayed.
The owner is a *required* argument of every read and write, so a caller cannot forget the
filter: a note that belongs to someone else is indistinguishable from one that does not exist.
Notes written before this change carry only ``user`` and are read as belonging to the account
of that name — which is what they meant when guests did not yet exist.

Persistence: a single server-side JSON file (session cookies are limited to ~4 KB and
already hold the panel state). The store re-reads the file on every access — trivially
correct for demonstrator-scale data and robust against parallel dev-server restarts.
Deliberately free of Flask imports (like ``filter_options``).
"""

import json
import os
import tempfile
from datetime import datetime
from pathlib import Path

# Only for the legacy mapping below: the ``user:`` namespace is defined in exactly one place.
from auth import account_owner
from store_lock import locked

# Reference point types of the first expansion stage. "modelcard" can be added later.
REFPOINT_TYPES = ("image", "workspace", "model", "xai_method", "collection")

# Titles are shown as the only text in the overview → keep them short.
TITLE_MAX = 100

# The body has no display constraint, so this is purely a ceiling against a store file that a
# single note could otherwise blow up (it is rewritten whole on every write). Far above any
# note a person writes; truncation is silent, like the title's.
TEXT_MAX = 20_000


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def owner_of(entry: dict) -> str:
    """The owner key of a stored note — derived from ``user`` for notes written before owners
    existed. Also used by ``collections_store`` for the same migration."""
    return entry.get("owner") or account_owner(entry.get("user") or "")


class NotesStore:
    """CRUD for notes in a JSON file: {"next_id": int, "notes": [note, ...]}."""

    def __init__(self, path):
        self.path = Path(path)

    # ── file handling ──────────────────────────────────────────────────────────

    def _load(self) -> dict:
        if not self.path.exists():
            return {"next_id": 1, "notes": []}
        with open(self.path, encoding="utf-8") as f:
            return json.load(f)

    def _save(self, data: dict):
        """Atomic write (tmp file + rename) so a crash never leaves a torn file."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    # ── CRUD ───────────────────────────────────────────────────────────────────

    def list_notes(self, owner: str, dataset: str = None) -> list:
        """This owner's notes (optionally restricted to one dataset), newest first."""
        notes = [n for n in self._load()["notes"] if owner_of(n) == owner]
        if dataset is not None:
            notes = [n for n in notes if n.get("dataset") == dataset]
        return sorted(notes, key=lambda n: n["id"], reverse=True)

    def get(self, nid: int, owner: str):
        """The note if it exists **and** belongs to this owner — otherwise None. Someone else's
        note is deliberately indistinguishable from a missing one (the caller answers 404)."""
        return next((n for n in self._load()["notes"]
                     if n["id"] == nid and owner_of(n) == owner), None)

    def create(self, title: str, text: str, refpoints: list,
               owner: str, user: str, project: str, dataset: str,
               include_in_report: bool = False, requirement_status: dict = None) -> dict:
        # locked(): the load-modify-save runs under a cross-process lock so a concurrent write on
        # another worker cannot reuse next_id or drop this note (S10). Same for every mutator below.
        with locked(self.path):
            data = self._load()
            note = {
                "id": data["next_id"],
                "created_at": _now(),
                "updated_at": _now(),
                # Who may see and edit it (server-assigned) vs. the name displayed as its author.
                "owner": owner,
                "user": user,
                "project": project,
                "dataset": dataset,
                # True → this note is an audit-report entry: shown in the report.
                "include_in_report": include_in_report,
                "title": title[:TITLE_MAX],
                "text": (text or "")[:TEXT_MAX],
                # Per requirement label ("A1") -> "met"|"unmet". Only assessed requirements are
                # present; a missing label means "no assessment". Kept even for non-report notes
                # so toggling the report flag off/on in the dialog never loses verdicts.
                "requirement_status": requirement_status or {},
                "refpoints": refpoints,
            }
            data["next_id"] += 1
            data["notes"].append(note)
            self._save(data)
            return note

    def update(self, nid: int, owner: str, title: str = None, text: str = None,
               refpoints: list = None, include_in_report: bool = None,
               requirement_status: dict = None):
        """Updates any provided field (title, text, reference points, report flag, verdicts);
        returns the note, or None if it is missing or belongs to someone else."""
        with locked(self.path):
            data = self._load()
            note = next((n for n in data["notes"]
                         if n["id"] == nid and owner_of(n) == owner), None)
            if note is None:
                return None
            if title is not None:
                note["title"] = title[:TITLE_MAX]
            if text is not None:
                note["text"] = text[:TEXT_MAX]
            if refpoints is not None:
                note["refpoints"] = refpoints
            if include_in_report is not None:
                note["include_in_report"] = include_in_report
            if requirement_status is not None:
                note["requirement_status"] = requirement_status
            note["updated_at"] = _now()
            self._save(data)
            return note

    def purge_requirement_labels(self, keep_labels, owner: str, dataset: str) -> int:
        """Drops verdicts for requirements that no longer exist, returns the number of notes changed.

        Called after a user edited their requirements catalog: a verdict keyed by a deleted label
        would dangle (nothing to display it against). Scoped to this owner's notes for this
        dataset — other users have their own catalog, where the label may well still exist.
        """
        keep = set(keep_labels)
        with locked(self.path):
            data = self._load()
            changed = 0
            for note in data["notes"]:
                if owner_of(note) != owner or note.get("dataset") != dataset:
                    continue
                status = note.get("requirement_status") or {}
                kept = {label: verdict for label, verdict in status.items() if label in keep}
                if len(kept) != len(status):
                    note["requirement_status"] = kept
                    note["updated_at"] = _now()
                    changed += 1
            if changed:
                self._save(data)
            return changed

    def delete(self, nid: int, owner: str) -> bool:
        """Deletes this owner's note; False if it does not exist or is someone else's."""
        with locked(self.path):
            data = self._load()
            before = len(data["notes"])
            data["notes"] = [n for n in data["notes"]
                             if not (n["id"] == nid and owner_of(n) == owner)]
            if len(data["notes"]) == before:
                return False
            self._save(data)
            return True

    def delete_owner(self, owner: str) -> int:
        """Removes everything one owner has: the sweep for expired guest sandboxes (see
        ``app.DEMO_MODE``). Returns the number of notes deleted."""
        with locked(self.path):
            data = self._load()
            before = len(data["notes"])
            data["notes"] = [n for n in data["notes"] if owner_of(n) != owner]
            removed = before - len(data["notes"])
            if removed:
                self._save(data)
            return removed

    def owners(self) -> set:
        """All owner keys present in the file — the sweep needs to know who exists."""
        return {owner_of(n) for n in self._load()["notes"]}

    def last_activity(self, owner: str) -> str:
        """The newest ``updated_at`` of this owner's notes ("" if none) — the sweep's clock."""
        stamps = [n.get("updated_at") or n.get("created_at") or ""
                  for n in self._load()["notes"] if owner_of(n) == owner]
        return max(stamps) if stamps else ""
