#!/usr/bin/env python3
"""Manage web UI accounts stored in traffic_analyzer/config/users.db.

Subcommands: add / list / remove / passwd. Passwords are given via
``--password`` or prompted interactively (getpass, no echo); never put real
passwords in shell history or help output.

可独立运行:python3 scripts/manage_users.py add alice
"""

from __future__ import annotations

import argparse
import getpass
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from traffic_analyzer.web import user_store  # noqa: E402


def _prompt_password(confirm: bool = True) -> str:
    password = getpass.getpass("Password: ")
    if not password:
        raise SystemExit("error: password must be non-empty")
    if confirm:
        again = getpass.getpass("Confirm password: ")
        if password != again:
            raise SystemExit("error: passwords do not match")
    return password


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Manage web UI accounts (SQLite users.db)."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_add = sub.add_parser("add", help="create an account")
    p_add.add_argument("username")
    p_add.add_argument("--password", help="omit to be prompted interactively")

    sub.add_parser("list", help="list all accounts")

    p_rm = sub.add_parser("remove", help="delete an account")
    p_rm.add_argument("username")

    p_pw = sub.add_parser("passwd", help="change an account's password")
    p_pw.add_argument("username")
    p_pw.add_argument("--password", help="omit to be prompted interactively")

    args = parser.parse_args()

    if args.command == "add":
        password = args.password or _prompt_password()
        if user_store.add_user(args.username, password):
            print(f"added: {args.username}")
            return 0
        print(f"error: user already exists: {args.username}", file=sys.stderr)
        return 1

    if args.command == "list":
        users = user_store.list_users()
        if not users:
            print("(no accounts)")
            return 0
        for u in users:
            created = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(u["created_at"]))
            status = "active" if u["active"] else "disabled"
            print(f"{u['username']}\t{status}\tcreated {created}")
        return 0

    if args.command == "remove":
        if user_store.remove_user(args.username):
            print(f"removed: {args.username}")
            return 0
        print(f"error: no such user: {args.username}", file=sys.stderr)
        return 1

    if args.command == "passwd":
        if user_store.get_user(args.username) is None:
            print(f"error: no such user: {args.username}", file=sys.stderr)
            return 1
        password = args.password or _prompt_password()
        user_store.set_password(args.username, password)
        print(f"password updated: {args.username}")
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
