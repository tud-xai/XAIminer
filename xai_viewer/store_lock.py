"""Cross-process advisory lock for the JSON stores (security review 2026-09-09, S10).

The stores mutate by *read-modify-write* on a whole JSON file. The write itself is atomic
(tmp file + ``os.replace``), but the sequence is not: under several gunicorn workers two
concurrent mutations can both read the old file and the later writer wins, silently dropping the
earlier change (a lost note, a duplicate ``next_id``). An exclusive ``flock`` on a sidecar
``<path>.lock`` serialises the whole read-modify-write across processes.

Where ``fcntl`` is unavailable (a Windows dev machine) this degrades to a no-op: the Flask dev
server there is single-process, so there is nothing to serialise. The production target is
POSIX (gunicorn on Linux), where the lock is real.

Deliberately free of Flask imports, like the stores that use it.
"""

from contextlib import contextmanager
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX (Windows dev only)
    fcntl = None


@contextmanager
def locked(path):
    """Hold an exclusive lock tied to ``path`` for the duration of the ``with`` block.

    The lock is keyed by a sidecar ``<path>.lock`` file (never the data file itself, whose inode
    is swapped out by the atomic replace). Do not nest ``locked()`` on the same path within one
    process — ``flock`` is per open-file-description, so a second acquisition would deadlock.
    """
    if fcntl is None:
        yield
        return
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + ".lock")
    with open(lock_path, "w") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)
