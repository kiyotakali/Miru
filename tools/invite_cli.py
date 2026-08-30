#!/usr/bin/env python3
"""Invitation code management CLI.

Usage (from project root):
    python -m tools.invite_cli new [--count N]     Generate N invitation codes
    python -m tools.invite_cli list                List all codes + status
    python -m tools.invite_cli users               List all users
    python -m tools.invite_cli suspend <user_id>   Suspend a user
    python -m tools.invite_cli activate <user_id>  Re-activate a user

Works in Docker:
    docker exec miru python -m tools.invite_cli new --count 5
"""

import argparse
import json
import os
import sys

# Ensure project root is importable
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import auth


def cmd_new(args):
    try:
        codes = auth.generate_invitation_codes(
            count=args.count,
            ip=args.ip,
            port=args.port,
        )
    except ValueError as e:
        print(f"Cannot generate invitation code: {e}", file=sys.stderr)
        sys.exit(2)
    print(f"Generated {len(codes)} invitation code(s):\n")
    for c in codes:
        print(f"  {c}")
    print()


def cmd_list(args):
    invitations = auth.list_invitations()
    if not invitations:
        print("No invitation codes found.")
        return
    print(f"{'Code':<16} {'Used By':<16} {'Created At':<20} {'Used At':<20}")
    print("-" * 72)
    for code, info in invitations.items():
        used = info.get("used_by") or "-"
        created = (info.get("created_at") or "")[:19]
        used_at = (info.get("used_at") or "-")[:19] if info.get("used_at") else "-"
        print(f"{code:<16} {used:<16} {created:<20} {used_at:<20}")
    print(f"\nTotal: {len(invitations)} codes, "
          f"{sum(1 for i in invitations.values() if i.get('used_by'))} used")


def cmd_users(args):
    users = auth.list_users()
    if not users:
        print("No users found.")
        return
    print(f"{'User ID':<16} {'Status':<10} {'Invitation':<16} {'Created At':<20}")
    print("-" * 62)
    for uid, info in users.items():
        status = info.get("status", "?")
        inv = info.get("invitation_code", "-")
        created = (info.get("created_at") or "")[:19]
        print(f"{uid:<16} {status:<10} {inv:<16} {created:<20}")
    print(f"\nTotal: {len(users)} users, "
          f"{sum(1 for u in users.values() if u.get('status') == 'active')} active")


def cmd_suspend(args):
    ok = auth.suspend_user(args.user_id)
    if ok:
        print(f"User {args.user_id} suspended.")
    else:
        print(f"User {args.user_id} not found.", file=sys.stderr)
        sys.exit(1)


def cmd_activate(args):
    ok = auth.activate_user(args.user_id)
    if ok:
        print(f"User {args.user_id} activated.")
    else:
        print(f"User {args.user_id} not found.", file=sys.stderr)
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(
        description="Miru invitation code management",
        prog="python -m tools.invite_cli",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_new = sub.add_parser("new", help="Generate invitation codes")
    p_new.add_argument("--count", "-n", type=int, default=1, help="Number of codes (default: 1)")
    p_new.add_argument("--ip", default=None, help="Public IPv4 to encode (default: SERVER_IP env)")
    p_new.add_argument("--port", "-p", type=int, default=5001, help="Server port (default: 5001)")
    p_new.set_defaults(func=cmd_new)

    p_list = sub.add_parser("list", help="List invitation codes")
    p_list.set_defaults(func=cmd_list)

    p_users = sub.add_parser("users", help="List all users")
    p_users.set_defaults(func=cmd_users)

    p_suspend = sub.add_parser("suspend", help="Suspend a user")
    p_suspend.add_argument("user_id", help="User ID to suspend")
    p_suspend.set_defaults(func=cmd_suspend)

    p_activate = sub.add_parser("activate", help="Re-activate a user")
    p_activate.add_argument("user_id", help="User ID to activate")
    p_activate.set_defaults(func=cmd_activate)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
