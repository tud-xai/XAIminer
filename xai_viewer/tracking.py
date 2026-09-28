"""
Usage tracking for usability analysis — opt-in, off by default.

The server log records *requests*, which is too coarse to reconstruct what a person did: a
click on "Cancel" produces no request at all, and a filter request does not say which checkbox
was ticked to produce it. This module stores the *click track*: one line per interaction,
recorded in the browser and posted in batches.

Not to be confused with an action log for the validator (so the audit process is
traceable). That is a product feature for the user; this is an instrument for *us*, invisible
in the normal application and switched on by hand for a usability session.

Two properties the rest of the app relies on:

* **Events come from the browser and are therefore untrusted.** ``sanitize()`` is the only way
  in: it keeps known keys, caps every string, and drops anything else. Nothing that lands in a
  file was taken over unchecked.
* **Password fields are never recorded.** The island does not read them, and ``sanitize()``
  drops the value a second time based on the field type — a guard that survives an edit of the
  island.

Persistence: one JSONL file per tracking session under a configured directory. Append-only, so
a session that ends in a browser crash keeps everything up to the crash, and an analysis can
stream the file. Deliberately free of Flask imports (like ``notes``/``filter_options``).
"""

import json
import re
from datetime import datetime
from pathlib import Path
from secrets import token_hex

from store_lock import locked

# Event kinds the client may report. An unknown kind is dropped rather than stored: the
# vocabulary is small on purpose, so an analysis can rely on it.
EVENT_KINDS = (
    "page",       # a page was loaded (full navigation)
    "click",      # a click reached an interactive element
    "change",     # a checkbox/select/text field was left with a new value
    "submit",     # a form was submitted
    "key",        # one of the application's keyboard shortcuts was used
    "request",    # an HTMX request started (verb + path)
    "tracking",   # the tracking mode itself was switched on/off
)

# Recorded fields and their maximum length. Everything is stored as a string: the analysis reads
# JSONL, and a uniform type is worth more there than a faithful bool.
EVENT_FIELDS = {
    "ts": 32,          # client timestamp (ISO, ms) — the order within a batch
    "path": 300,       # location.pathname + search at the time of the event
    "tag": 20,         # BUTTON, A, INPUT, SELECT, …
    "el_id": 120,      # the element's id, the most stable handle for an analysis
    "name": 120,       # form field name
    "input_type": 20,  # checkbox, radio, text, … (never "password", see below)
    "label": 300,      # what the person saw: aria-label, title, or the visible text
    "value": 500,      # the new value of a field, or checked/unchecked
    "hx": 240,         # the HTMX request the element triggers ("post /panel/1/config")
    "panel": 20,       # id of the surrounding panel, if any
    "dialog": 120,     # id of the surrounding dialog/dock, if any
    "key": 40,         # the pressed shortcut ("Escape", "ArrowLeft", "n")
}

# A batch is small in normal use (the island flushes every few seconds). The cap is an upper
# bound against a client that posts a huge array, not a limit anyone reaches by working.
MAX_EVENTS_PER_BATCH = 500

# Per tracking session. A usability session produces a few hundred KB; this is far above that
# and exists so a stuck client cannot fill the disk.
DEFAULT_MAX_BYTES = 20 * 1024 * 1024

# Tracking session ids are ours (generated below) and become a file name — checked anyway, so
# that no future caller can turn one into a path.
SESSION_ID_RE = re.compile(r"\A[0-9]{8}-[0-9]{6}-[0-9a-f]{8}\Z")


def new_session_id(now: datetime = None) -> str:
    """``20260921-143005-1a2b3c4d`` — sorts chronologically and is unique across workers."""
    return f"{(now or datetime.now()).strftime('%Y%m%d-%H%M%S')}-{token_hex(4)}"


def sanitize(raw) -> dict:
    """One browser-reported event, reduced to what may be stored — or ``{}`` if unusable.

    Unknown keys are dropped rather than passed through: the file is read by our own analysis
    later, and letting a client define its own fields would make that a moving target.
    """
    if not isinstance(raw, dict):
        return {}
    kind = raw.get("kind")
    if not isinstance(kind, str) or kind not in EVENT_KINDS:
        return {}
    event = {"kind": kind}
    for field, limit in EVENT_FIELDS.items():
        value = raw.get(field)
        if value is None or isinstance(value, (dict, list)):
            continue
        if isinstance(value, bool):
            value = "true" if value else "false"
        text = str(value).strip()
        # An empty string is dropped — except as a field's *value*, where "" is the fact that
        # someone cleared the field, which an analysis would otherwise never see.
        if text or field == "value":
            event[field] = text[:limit]
    # Second line of defence: the island never reads a password field, and if an edit ever makes
    # it do so, the value still does not reach the disk.
    if event.get("input_type") == "password":
        event.pop("value", None)
    return event


def sanitize_batch(raw) -> list:
    """The storable events of a posted batch, in the order the browser reported them."""
    if not isinstance(raw, list):
        return []
    events = (sanitize(item) for item in raw[:MAX_EVENTS_PER_BATCH])
    return [e for e in events if e]


class TrackingStore:
    """Append-only JSONL, one file per tracking session, under ``directory``."""

    def __init__(self, directory, max_bytes: int = DEFAULT_MAX_BYTES):
        self.directory = Path(directory)
        self.max_bytes = max_bytes

    def path_for(self, session_id: str):
        """The file of this tracking session, or None if the id is not one of ours."""
        if not isinstance(session_id, str) or not SESSION_ID_RE.match(session_id):
            return None
        return self.directory / f"{session_id}.jsonl"

    def append(self, session_id: str, events: list, context: dict = None) -> int:
        """Writes the events, each stamped with the server-side context. Returns how many landed.

        Zero means the batch was empty, the id was not ours, or the session hit its size cap —
        none of which a caller can do anything about, so they are not distinguished. Tracking
        must never be the reason a request fails.
        """
        path = self.path_for(session_id)
        if path is None or not events:
            return 0
        # ``astimezone()`` rather than the naive ``now()`` the other stores use: a line carries
        # the browser's timestamp too, and that one is UTC ("…Z"). Two stamps in one line that
        # look comparable but are hours apart is exactly the trap an analysis falls into, so the
        # server stamp states its offset.
        stamp = {"srv_ts": datetime.now().astimezone().isoformat(timespec="milliseconds")}
        stamp.update(context or {})
        lines = "".join(json.dumps({**stamp, **e}, ensure_ascii=False) + "\n" for e in events)
        # The load-modify-write is a size check plus an append; two workers must not interleave
        # around the cap (same reasoning as the JSON stores, see store_lock.py).
        with locked(path):
            size = path.stat().st_size if path.exists() else 0
            if size >= self.max_bytes:
                return 0
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write(lines)
                if size + len(lines.encode("utf-8")) >= self.max_bytes:
                    # Say so in the file itself: a silently truncated track would be read as a
                    # person who simply stopped working.
                    f.write(json.dumps({**stamp, "kind": "tracking", "value": "cap_reached"}) + "\n")
        return len(events)

    def sessions(self) -> list:
        """Ids of the recorded tracking sessions, oldest first (the id sorts chronologically)."""
        if not self.directory.is_dir():
            return []
        return sorted(p.stem for p in self.directory.glob("*.jsonl")
                      if SESSION_ID_RE.match(p.stem))

    def read(self, session_id: str) -> list:
        """Every event of one tracking session — for the analysis, not used by the app."""
        path = self.path_for(session_id)
        if path is None or not path.exists():
            return []
        events = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # a torn last line (killed process) must not spoil the whole read
        return events
