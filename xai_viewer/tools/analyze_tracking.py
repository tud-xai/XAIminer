"""
Reads the recordings of the tracking mode (see ``tracking.py``) and makes them legible.

    python tools/analyze_tracking.py                      # list the recorded sessions
    python tools/analyze_tracking.py <session-id>         # the click track, with dwell times
    python tools/analyze_tracking.py <session-id> --summary   # only the aggregates
    python tools/analyze_tracking.py --dir /path/to/tracking ...

**On dwell times.** A recording holds points in time, not durations, so every duration here is
the gap to the *next* recorded event. That is a good measure for "how long did someone sit in
front of this step", and a bad one for anything the recording cannot see: a person who walks
away produces the same gap as a person who thinks hard, and the last action before the mode is
switched off has no successor and therefore no duration at all. Gaps beyond ``IDLE_SECONDS``
are therefore reported separately as *idle* and kept out of the totals — otherwise one coffee
break decides the statistics.

Two clocks are in every line: ``ts`` is the browser's (UTC, when it happened) and ``srv_ts``
the server's (with offset, when the batch arrived). Durations use ``ts``; it is the one that
measures the person rather than the network.
"""

import argparse
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tracking import TrackingStore  # noqa: E402  (after the path fix, like the other tools)

DEFAULT_DIR = Path(__file__).resolve().parent.parent / "tracking"

# Beyond this, a gap is not "the person was working on it" but "the person was gone". Kept out
# of the totals and shown separately, so a break cannot masquerade as deep engagement.
IDLE_SECONDS = 120

# Events that are consequences of an action rather than actions themselves. They are shown in
# the track but never get a dwell time of their own — otherwise the millisecond between a click
# and the request it triggers would count as a step the person took.
DERIVED_KINDS = ("request",)


def parse_ts(event: dict):
    """The browser timestamp of an event, or the server's for the ones the browser never saw
    (switching the mode on and off is recorded server-side)."""
    raw = event.get("ts") or event.get("srv_ts")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def describe(event: dict) -> str:
    """One event as a person would read it."""
    kind = event["kind"]
    label = event.get("label") or ""
    where = event.get("el_id") or event.get("name") or event.get("tag") or ""
    parts = []
    if kind == "click":
        parts.append(f"Klick auf {label or where!r}")
    elif kind == "change":
        parts.append(f"{label or where} → {event.get('value', '')!r}")
    elif kind == "submit":
        parts.append(f"Formular abgeschickt ({event.get('hx') or where})")
    elif kind == "key":
        parts.append(f"Taste {event.get('key')!r}")
    elif kind == "request":
        parts.append(f"→ {event.get('hx', '')}")
    elif kind == "page":
        parts.append(f"Seite {event.get('path', '')} ({label})")
    elif kind == "tracking":
        parts.append(f"[Aufzeichnung {event.get('value')}]")
    else:
        parts.append(f"{kind} {where}")
    context = []
    if event.get("panel"):
        context.append(f"P{event['panel']}")
    if event.get("dialog"):
        context.append(event["dialog"])
    if context:
        parts.append(f"({', '.join(context)})")
    return " ".join(parts)


def with_durations(events: list) -> list:
    """``[(event, seconds_to_next_or_None, is_idle), ...]`` in recorded order.

    The duration of an event is the gap to the next event that is an *action*; consequences
    (the HTMX request a click causes) are skipped, so a click keeps the time the person spent
    before their next move instead of the millisecond until the request went out.
    """
    stamps = [parse_ts(e) for e in events]
    rows = []
    for i, event in enumerate(events):
        seconds = None
        if event["kind"] not in DERIVED_KINDS and stamps[i] is not None:
            for j in range(i + 1, len(events)):
                if events[j]["kind"] in DERIVED_KINDS or stamps[j] is None:
                    continue
                seconds = (stamps[j] - stamps[i]).total_seconds()
                break
        rows.append((event, seconds, seconds is not None and seconds > IDLE_SECONDS))
    return rows


def session_summary(events: list) -> dict:
    """The aggregates one actually wants: length, counts, where the time went."""
    rows = with_durations(events)
    stamps = [s for s in (parse_ts(e) for e in events) if s is not None]
    active = sum(d for _, d, idle in rows if d is not None and not idle)
    idle = sum(d for _, d, idle in rows if d is not None and idle)
    per_kind = defaultdict(float)
    for event, seconds, is_idle in rows:
        if seconds is not None and not is_idle:
            per_kind[event["kind"]] += seconds
    clicks = Counter(e.get("label") or e.get("el_id") or e.get("tag")
                     for e in events if e["kind"] == "click")
    return {
        "events": len(events),
        "wall": (max(stamps) - min(stamps)).total_seconds() if len(stamps) > 1 else 0.0,
        "active": active,
        "idle": idle,
        "kinds": Counter(e["kind"] for e in events),
        "time_per_kind": dict(per_kind),
        "top_clicks": clicks.most_common(8),
        "dialogs": dialog_durations(rows),
    }


def dialog_durations(rows: list) -> list:
    """How long each visit to a dialog lasted — the question a usability session really asks.

    Two things this has to get right, both learned from the first real recordings:

    * The HTMX request a click causes carries no dialog of its own. Taken at face value it
      would end the visit and split one trip through the configuration dialog into three —
      so derived events inherit the context of the event before them.
    * The time a person spends on a dialog starts when they click the button that opens it,
      not when they first touch something inside it. That opening click is where the reading
      and orienting happens, so its duration belongs to the visit.
    """
    visits, current, previous = [], None, None
    for event, seconds, is_idle in rows:
        dialog = event.get("dialog")
        if event["kind"] in DERIVED_KINDS:
            dialog = current["dialog"] if current else None   # inherit, never terminate
        if dialog and current is None:
            current = {"dialog": dialog, "seconds": 0.0, "events": 0}
            if previous and previous[1] is not None and not previous[2]:
                current["seconds"] += previous[1]             # the click that opened it
        if current is not None:
            if dialog == current["dialog"]:
                if event.get("dialog"):
                    current["events"] += 1
                if seconds is not None and not is_idle:
                    current["seconds"] += seconds
            else:
                visits.append(current)
                current = None
        if event["kind"] not in DERIVED_KINDS:
            previous = (event, seconds, is_idle)
    if current is not None:
        visits.append(current)
    return visits


def fmt(seconds) -> str:
    if seconds is None:
        return "     –"
    if seconds >= 60:
        return f"{int(seconds // 60)}m{seconds % 60:04.1f}s"
    return f"{seconds:5.1f}s"


def print_track(events: list):
    rows = with_durations(events)
    print(f"{'Zeit':12} {'Dauer':>7}  Ereignis")
    print("-" * 100)
    for event, seconds, is_idle in rows:
        stamp = parse_ts(event)
        clock = stamp.astimezone().strftime("%H:%M:%S.%f")[:-3] if stamp else "??"
        mark = " (Pause)" if is_idle else ""
        print(f"{clock:12} {fmt(seconds):>7}  {describe(event)}{mark}")


def print_summary(summary: dict):
    print(f"Ereignisse:       {summary['events']}")
    print(f"Gesamtspanne:     {fmt(summary['wall'])}")
    print(f"davon aktiv:      {fmt(summary['active'])}   "
          f"(Pausen > {IDLE_SECONDS}s: {fmt(summary['idle'])})")
    print(f"Arten:            {dict(summary['kinds'])}")
    if summary["time_per_kind"]:
        print("Zeit je Art:      " + ", ".join(
            f"{kind} {fmt(seconds)}" for kind, seconds in
            sorted(summary["time_per_kind"].items(), key=lambda kv: -kv[1])))
    if summary["dialogs"]:
        print("Dialog-Besuche:")
        for visit in summary["dialogs"]:
            print(f"  {visit['dialog']:16} {fmt(visit['seconds'])}  "
                  f"({visit['events']} Ereignisse)")
    if summary["top_clicks"]:
        print("Häufigste Klicks:")
        for label, count in summary["top_clicks"]:
            print(f"  {count:3}x  {str(label)[:80]}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("session", nargs="?", help="session id (without .jsonl); omit to list")
    parser.add_argument("--dir", default=str(DEFAULT_DIR), help=f"default: {DEFAULT_DIR}")
    parser.add_argument("--summary", action="store_true", help="aggregates only, no track")
    args = parser.parse_args()

    store = TrackingStore(Path(args.dir))
    sessions = store.sessions()
    if not sessions:
        print(f"Keine Aufzeichnungen unter {args.dir}.")
        return 1

    if not args.session:
        print(f"{len(sessions)} Aufzeichnung(en) unter {args.dir}:\n")
        for sid in sessions:
            events = store.read(sid)
            summary = session_summary(events)
            who = next((e.get("user") for e in events if e.get("user")), "?")
            # A session that was armed and disarmed straight away is noise, not a recording.
            note = "  (leer)" if summary["events"] <= 2 else ""
            print(f"  {sid}  {summary['events']:4} Ereignisse  {fmt(summary['wall']):>8}  "
                  f"{who}{note}")
        return 0

    events = store.read(args.session)
    if not events:
        print(f"Keine Aufzeichnung {args.session!r} unter {args.dir}.")
        return 1
    if not args.summary:
        print_track(events)
        print()
    print_summary(session_summary(events))
    return 0


if __name__ == "__main__":
    sys.exit(main())
