#!/usr/bin/env python3
"""
Account maintenance for the public demo (see auth.py).

The demo knows two kinds of visitor: anonymous guests, who get a throwaway sandbox and need no
account, and a handful of *known* users with real persistence. The latter are created here —
there is no self-registration and no password reset in the app.

Run from `xai_viewer/`:

    python tools/manage_accounts.py list
    python tools/manage_accounts.py add anna              # asks for the password twice
    python tools/manage_accounts.py add anna --password s3cret
    python tools/manage_accounts.py passwd anna
    python tools/manage_accounts.py remove anna

The file it writes (``accounts.toml``, next to the JSON stores unless configured otherwise) is
never uploaded by a deploy — it holds password hashes and would overwrite the server's accounts
with local test ones. Server accounts are therefore created *on the server*, in the deployment
directory: ``python xai_viewer/tools/manage_accounts.py add <name>``.

Removing an account does NOT remove the data it owns; that is deliberate, so a mistyped removal
is recoverable by recreating the account under the same name.
"""

import argparse
import sys
import tomllib
from getpass import getpass
from pathlib import Path

HERE = Path(__file__).resolve().parent
XAI_DIR = HERE.parent
# sys.path bootstrap for the flat app modules — see tools/build_metadata.py.
if str(XAI_DIR) not in sys.path:
    sys.path.insert(0, str(XAI_DIR))

from auth import AccountError, AccountStore, accounts_path   # noqa: E402


def _config() -> dict:
    """The app's own config.toml, for ``paths.accounts_file`` (same source as app.py reads)."""
    path = XAI_DIR / "config.toml"
    if not path.exists():
        return {}
    with open(path, "rb") as f:
        return tomllib.load(f)


def _ask_password(name: str) -> str:
    first = getpass(f"Password for {name!r}: ")
    if first != getpass("Repeat: "):
        sys.exit("Passwords do not match.")
    if not first.strip():
        sys.exit("Empty password.")
    return first


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[1].strip())
    parser.add_argument("--file", help="accounts file (default: as the app resolves it)")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="list account names")
    for command, help_text in (("add", "create an account"), ("passwd", "change a password")):
        p = sub.add_parser(command, help=help_text)
        p.add_argument("name")
        p.add_argument("--password", help="skip the prompt (visible in the shell history!)")
    p = sub.add_parser("remove", help="delete an account (its data is kept)")
    p.add_argument("name")

    args = parser.parse_args(argv)
    path = Path(args.file) if args.file else accounts_path(XAI_DIR, _config())
    store = AccountStore(path)

    try:
        if args.command == "list":
            names = store.names()
            print(f"{path} — {len(names)} account(s)")
            for name in names:
                print(f"  {name}")
        elif args.command == "add":
            created = store.add(args.name, args.password or _ask_password(args.name))
            print(f"Created {created!r} in {path}")
        elif args.command == "passwd":
            store.set_password(args.name, args.password or _ask_password(args.name))
            print(f"Password of {args.name!r} changed in {path}")
        elif args.command == "remove":
            store.remove(args.name)
            print(f"Removed {args.name!r} from {path} (its notes and collections are kept)")
    except AccountError as exc:
        sys.exit(f"Error: {exc}")


if __name__ == "__main__":
    main()
