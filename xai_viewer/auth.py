"""
Identity: who owns the data a request creates (public-demo prerequisite, 2026-08-30).

Until now ``session["user"]`` was *both* the name shown on screen and the key the two
user-scoped features (audit report, requirements catalog) filtered by — so typing someone
else's name into the login field was enough to take over their data. A login field is not an
identity.

The two roles are therefore separated:

``owner``
    The key everything is filtered and stored by. Assigned **server-side**, never typed:
    ``guest:<token>`` for an anonymous visitor (a random token minted on entry, carried only
    in the signed session cookie) or ``user:<account>`` for one of the pre-created accounts,
    set only after the password checked out. The two prefixes keep the namespaces apart, so
    no guest can land on an account's data by choosing a clever name.

``display name``
    Stays ``session["user"]``: whatever the visitor typed, shown in the navbar and recorded as
    the author of notes and report entries. Freely chosen, without any effect on access.

**Guest identity only exists in demo mode.** On a local or internal instance a guest keeps
``user:<typed name>`` — the historical behaviour — because otherwise a developer would lose
their own notes whenever the session cookie goes away. Demo mode is what turns anonymous
visitors into throwaway sandboxes (see ``app.DEMO_MODE``).

Accounts live in a TOML file outside the deployed tree's upload set (``accounts.toml``,
maintained with ``tools/manage_accounts.py``); there is no self-registration and no password reset.
The file is re-read on every access, like the other stores — a new account is live without a
restart, and at ~20 accounts the cost is irrelevant.

Deliberately free of Flask imports (like ``notes.py``): names and hashes in, verdicts out.
"""

import hashlib
import os
import re
import secrets
import tomllib
from collections import deque
from pathlib import Path
from time import monotonic

from werkzeug.security import check_password_hash, generate_password_hash

GUEST_PREFIX = "guest:"
ACCOUNT_PREFIX = "user:"

# Account names double as TOML keys and (slugged) as file name fragments of the per-user
# requirements catalogs. Keeping them to this alphabet avoids both quoting and collisions.
ACCOUNT_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,39}$")

# Guest tokens: 32 bytes of URL-safe randomness. They are the *only* thing standing between
# two anonymous visitors' data, so they are generated with `secrets`, never derived.
GUEST_TOKEN_BYTES = 32


class AccountError(Exception):
    """Refused account operation (invalid name, duplicate, unknown) — shown, never raised at a user."""


def accounts_path(base_dir, config: dict = None) -> Path:
    """Where the accounts file lives — resolved identically by the app and by
    ``tools/manage_accounts.py`` so the two never disagree about which file they are looking at.

    Precedence mirrors the other stores: environment (set by the deployment) → ``config.toml``
    → beside the JSON stores. It is excluded from every upload (``USER_DATA_GLOBS`` in
    ``deployment/deploy.py``), so server accounts are created on the server.
    """
    configured = (os.environ.get("XAI_ACCOUNTS_FILE")
                  or ((config or {}).get("paths", {}) or {}).get("accounts_file"))
    return Path(configured) if configured else Path(base_dir) / "accounts.toml"


# ── owner keys ────────────────────────────────────────────────────────────────

def normalize_account_name(name: str) -> str:
    """Lower-cased, trimmed account name. Not a validity check — see ``valid_account_name``."""
    return (name or "").strip().lower()


def valid_account_name(name: str) -> bool:
    return bool(ACCOUNT_NAME_RE.match(name or ""))


def account_owner(name: str) -> str:
    return f"{ACCOUNT_PREFIX}{normalize_account_name(name)}"


def new_guest_owner() -> str:
    """A fresh sandbox key. Called once per anonymous visitor, at login."""
    return f"{GUEST_PREFIX}{secrets.token_urlsafe(GUEST_TOKEN_BYTES)}"


def is_guest(owner: str) -> bool:
    return (owner or "").startswith(GUEST_PREFIX)


def is_account(owner: str) -> bool:
    return (owner or "").startswith(ACCOUNT_PREFIX)


def account_of(owner: str) -> str:
    """The account name inside an ``user:`` owner key ("" for guests)."""
    return owner[len(ACCOUNT_PREFIX):] if is_account(owner) else ""


def owner_slug(owner: str) -> str:
    """A file-name-safe, collision-free rendering of an owner key.

    Accounts keep their name (``user-anna``) because a directory listing should stay readable.
    Guest tokens are hashed instead of transliterated: they are case-sensitive random strings,
    and lower-casing them for a file name would throw away entropy — a hash keeps them distinct
    *and* keeps a session token out of the file system.
    """
    if is_account(owner):
        return f"user-{account_of(owner)}"
    if is_guest(owner):
        digest = hashlib.sha256(owner.encode("utf-8")).hexdigest()[:16]
        return f"guest-{digest}"
    # Neither prefix: a hand-written or legacy value. Transliterate defensively.
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", (owner or "").strip()).strip("-").lower()
    return slug or "unknown"


# ── account store ─────────────────────────────────────────────────────────────

class AccountStore:
    """The pre-created accounts: ``{"accounts": {"<name>": "<password hash>"}}`` in a TOML file.

    A missing file simply means "no accounts" — every visitor is then a guest, which is exactly
    what a fresh checkout and the test suite want.
    """

    def __init__(self, path):
        self.path = Path(path)

    # ── reading ────────────────────────────────────────────────────────────────

    def _load(self) -> dict:
        if not self.path.exists():
            return {}
        with open(self.path, "rb") as f:
            data = tomllib.load(f)
        accounts = data.get("accounts") or {}
        return {normalize_account_name(k): v for k, v in accounts.items() if isinstance(v, str)}

    def names(self) -> list:
        return sorted(self._load())

    def exists(self, name: str) -> bool:
        return normalize_account_name(name) in self._load()

    def verify(self, name: str, password: str) -> bool:
        """Whether name+password identify an account. Runs the hash comparison even for an
        unknown name (against a throwaway hash) so a wrong *name* takes as long as a wrong
        *password* — otherwise the response time enumerates the account list."""
        stored = self._load().get(normalize_account_name(name))
        if stored is None:
            check_password_hash(_DUMMY_HASH, password or "")
            return False
        return check_password_hash(stored, password or "")

    # ── writing (tools/manage_accounts.py) ───────────────────────────────────────────

    def add(self, name: str, password: str) -> str:
        """Creates an account, returns its normalized name. Raises AccountError on a bad or
        duplicate name, or an empty password."""
        name = normalize_account_name(name)
        if not valid_account_name(name):
            raise AccountError(
                f"invalid account name {name!r}: 2–40 characters, a–z 0–9 . _ -, starting "
                "with a letter or digit")
        if not (password or "").strip():
            raise AccountError("empty password")
        accounts = self._load()
        if name in accounts:
            raise AccountError(f"account {name!r} already exists")
        accounts[name] = generate_password_hash(password)
        self._write(accounts)
        return name

    def set_password(self, name: str, password: str):
        name = normalize_account_name(name)
        accounts = self._load()
        if name not in accounts:
            raise AccountError(f"unknown account {name!r}")
        if not (password or "").strip():
            raise AccountError("empty password")
        accounts[name] = generate_password_hash(password)
        self._write(accounts)

    def remove(self, name: str):
        name = normalize_account_name(name)
        accounts = self._load()
        if name not in accounts:
            raise AccountError(f"unknown account {name!r}")
        del accounts[name]
        self._write(accounts)

    def _write(self, accounts: dict):
        """Rewrites the file. Hand-rolled TOML: the payload is two known string fields, and the
        stdlib has no writer. Both are quoted, and the alphabet of names and hashes excludes
        quotes and backslashes, so there is nothing to escape."""
        lines = ["# Accounts for the public demo — created with tools/manage_accounts.py.",
                 "# Passwords are stored as werkzeug hashes; there is no reset (create anew).",
                 "", "[accounts]"]
        lines += [f'"{name}" = "{accounts[name]}"' for name in sorted(accounts)]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        # The file holds password hashes and lives on a shared host.
        self.path.chmod(0o600)


# A hash of a value nobody can supply, for the constant-time path in verify().
_DUMMY_HASH = generate_password_hash(secrets.token_urlsafe(16))


# ── login rate limit ──────────────────────────────────────────────────────────

class RateLimiter:
    """Sliding-window counter, keyed by client address — the guard on password guessing.

    In-memory on purpose: with several gunicorn workers each keeps its own window, which
    weakens the limit by the worker count but needs no shared store. At ~20 accounts with
    operator-assigned passwords that is ample, and it cannot break the app when it is wrong.
    """

    def __init__(self, max_attempts: int = 10, window_seconds: int = 60, clock=monotonic):
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        self._clock = clock
        self._hits = {}

    def allow(self, key: str) -> bool:
        """Records an attempt for ``key`` and reports whether it is still within the limit."""
        now = self._clock()
        window = self._hits.setdefault(key, deque())
        while window and now - window[0] > self.window_seconds:
            window.popleft()
        if len(window) >= self.max_attempts:
            return False
        window.append(now)
        return True

    def reset(self, key: str):
        """Forgets a key's attempts — called after a successful login."""
        self._hits.pop(key, None)

    def prune(self):
        """Drops empty/expired windows so the dict cannot grow without bound."""
        now = self._clock()
        for key in list(self._hits):
            window = self._hits[key]
            while window and now - window[0] > self.window_seconds:
                window.popleft()
            if not window:
                del self._hits[key]
