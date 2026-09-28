"""
Image collections — ad-hoc, manually curated sets of images.

A collection is a *snapshot* (Q5a): it stores concrete image ids captured at creation time, not
a rule that is re-evaluated later. That is what makes the planned management operations (merge,
clone, remove single images) and the future "pick images in a gallery" flow well-defined — they
all operate on an id set, which a live rule could not offer.

The first way to create one is from the confusion matrix: tick cells, hit "create collection".
A panel can then adopt a collection as its base image source ({"type": "collection", "id": N}),
exactly like it adopts another panel's filter result — the discriminator in the panel's
``image_source`` field was designed for this (see app.py _default_panel).

    {"id": int, "created_at", "updated_at", "user", "project", "dataset",
     "title": str, "image_ids": [<id>, ...],
     "origin": {"kind": "confmatrix", "model", "level",
                "selection": {"key","value"}|None, "cells": [[true, predicted], ...]}}

``origin`` is provenance only (how the set was captured) — the id list is authoritative.

The management operations (rename, clone, merge, remove single images, delete) are plain id-set
edits here. Whether a given collection may be edited *in place* at all is **not** decided in this
module: a collection referenced by a note is frozen, and the caller clones it first (see
``_collection_edit_target`` in app.py). Keeping that rule out of the store keeps the store free of
any knowledge about notes.

**Ownership** (2026-08-30) mirrors NotesStore exactly: every collection belongs to an ``owner``
(the server-assigned key from ``auth.py``), which is a required argument of every read and
write, and ``user`` is only the displayed author. See ``notes.py`` for the reasoning and for
how collections written before this change are mapped.

Persistence mirrors NotesStore: a single server-side JSON file, re-read on every access. Trivially
correct at demonstrator scale and robust against parallel dev-server restarts; later this all moves
into a database (Entscheidung 2026-07-17). Deliberately free of Flask imports.
"""

import json
import os
import tempfile
from datetime import datetime
from pathlib import Path

from notes import owner_of
from store_lock import locked

# Titles double as the panel/source label → keep them short.
TITLE_MAX = 100


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class CollectionsStore:
    """CRUD for collections in a JSON file: {"next_id": int, "collections": [collection, ...]}."""

    def __init__(self, path):
        self.path = Path(path)

    # ── file handling ──────────────────────────────────────────────────────────

    def _load(self) -> dict:
        if not self.path.exists():
            return {"next_id": 1, "collections": []}
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

    def list_collections(self, owner: str, dataset: str = None) -> list:
        """This owner's collections (optionally restricted to one dataset), newest first."""
        cols = [c for c in self._load()["collections"] if owner_of(c) == owner]
        if dataset is not None:
            cols = [c for c in cols if c.get("dataset") == dataset]
        return sorted(cols, key=lambda c: c["id"], reverse=True)

    def get(self, cid: int, owner: str):
        """The collection if it exists **and** belongs to this owner, else None."""
        return next((c for c in self._load()["collections"]
                     if c["id"] == cid and owner_of(c) == owner), None)

    def titles(self, owner: str, dataset: str = None) -> set:
        """Existing titles (for auto-title collision avoidance)."""
        return {c["title"] for c in self.list_collections(owner, dataset)}

    def create(self, title: str, image_ids: list, owner: str, user: str, project: str,
               dataset: str, origin: dict = None) -> dict:
        # locked(): serialise the load-modify-save across workers so next_id is not reused and no
        # concurrent write drops this collection (S10). Same for every mutator below; clone()
        # delegates to create() and must NOT take the lock itself (flock would self-deadlock).
        with locked(self.path):
            data = self._load()
            collection = {
                "id": data["next_id"],
                "created_at": _now(),
                "updated_at": _now(),
                # Who may see and edit it (server-assigned) vs. the name displayed as its author.
                "owner": owner,
                "user": user,
                "project": project,
                "dataset": dataset,
                "title": title[:TITLE_MAX],
                "image_ids": list(image_ids),
                "origin": origin or {},
            }
            data["next_id"] += 1
            data["collections"].append(collection)
            self._save(data)
            return collection

    @staticmethod
    def _find(data: dict, cid: int, owner: str):
        """The owner's collection inside an already-loaded file (for read-modify-write)."""
        return next((c for c in data["collections"]
                     if c["id"] == cid and owner_of(c) == owner), None)

    def _write(self, data: dict, collection: dict) -> dict:
        """Stamps ``updated_at`` on a mutated collection and persists the whole file."""
        collection["updated_at"] = _now()
        self._save(data)
        return collection

    def rename(self, cid: int, owner: str, title: str):
        """Renames a collection. Non-destructive: the image set is untouched, so this is allowed
        even for note-referenced collections. Empty titles are ignored (None → not found)."""
        with locked(self.path):
            data = self._load()
            collection = self._find(data, cid, owner)
            if collection is None or not title.strip():
                return None
            collection["title"] = title.strip()[:TITLE_MAX]
            return self._write(data, collection)

    def clone(self, cid: int, owner: str, title: str):
        """Copies a collection (new id, same image set). ``origin`` records the source so the
        provenance chain stays readable after a freeze-induced clone."""
        source = self.get(cid, owner)
        if source is None:
            return None
        return self.create(
            title=title,
            image_ids=source["image_ids"],
            owner=owner,
            user=source.get("user", "?"),
            project=source.get("project", "?"),
            dataset=source.get("dataset"),
            origin={"kind": "clone", "source_id": cid, "source_title": source["title"],
                    "source_origin": source.get("origin") or {}},
        )

    def add_images(self, cid: int, owner: str, image_ids: list):
        """Appends image ids to a collection, skipping ones already in it (order preserved).
        This is the merge primitive: "add the images of X to Y"."""
        with locked(self.path):
            data = self._load()
            collection = self._find(data, cid, owner)
            if collection is None:
                return None
            present = set(collection["image_ids"])
            for image_id in image_ids:
                if image_id not in present:
                    collection["image_ids"].append(image_id)
                    present.add(image_id)
            return self._write(data, collection)

    def remove_images(self, cid: int, owner: str, image_ids):
        """Removes several images at once; None if the collection is unknown or nothing matched.

        Removing as a *batch* is what the caller needs: one selection → one operation → a frozen
        collection is cloned exactly once with all removals in that single copy. Per-image removal
        would clone the still-referenced original again for every single image, leaving a trail of
        copies of which none is the intended result. A collection may legitimately end up empty —
        the panel then simply shows "no match"."""
        with locked(self.path):
            data = self._load()
            collection = self._find(data, cid, owner)
            if collection is None:
                return None
            drop = set(image_ids) & set(collection["image_ids"])
            if not drop:
                return None
            collection["image_ids"] = [i for i in collection["image_ids"] if i not in drop]
            return self._write(data, collection)

    def delete(self, cid: int, owner: str) -> bool:
        """Deletes this owner's collection; False if it does not exist or is someone else's."""
        with locked(self.path):
            data = self._load()
            before = len(data["collections"])
            data["collections"] = [c for c in data["collections"]
                                   if not (c["id"] == cid and owner_of(c) == owner)]
            if len(data["collections"]) == before:
                return False
            self._save(data)
            return True

    def delete_owner(self, owner: str) -> int:
        """Removes everything one owner has — the sweep for expired guest sandboxes."""
        with locked(self.path):
            data = self._load()
            before = len(data["collections"])
            data["collections"] = [c for c in data["collections"] if owner_of(c) != owner]
            removed = before - len(data["collections"])
            if removed:
                self._save(data)
            return removed

    def owners(self) -> set:
        return {owner_of(c) for c in self._load()["collections"]}

    def last_activity(self, owner: str) -> str:
        stamps = [c.get("updated_at") or c.get("created_at") or ""
                  for c in self._load()["collections"] if owner_of(c) == owner]
        return max(stamps) if stamps else ""
