#!/usr/bin/env python3
"""Server-side tenant administration. The only way to change live switches, the Meta
allowlist and AI budgets (the settings API refuses them with 403).

  tenant_admin.py show <tid>
  tenant_admin.py switch <tid> live_meta_enabled|live_calendar_enabled on|off [--owner-yes "<recorded reason>"]
  tenant_admin.py allowlist <tid> add|remove|clear [<account_id>] [--owner-yes "<reason>"]
  tenant_admin.py budget <tid> ai|critique <tokens> [--owner-yes "<reason>"]
  tenant_admin.py kb-scope <tid> course|empty|own
  tenant_admin.py block-attendee <tid> <email>

Turning anything on needs --owner-yes with the owner's recorded reason; it is written to the
audit log. Turning things off needs nothing. Values are never printed for secrets.
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from operator_ai import settings, tenancy  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("show")
    s.add_argument("tid")
    s = sub.add_parser("switch")
    s.add_argument("tid")
    s.add_argument("key", choices=["live_meta_enabled", "live_calendar_enabled"])
    s.add_argument("state", choices=["on", "off"])
    s.add_argument("--owner-yes", default="")
    s = sub.add_parser("allowlist")
    s.add_argument("tid")
    s.add_argument("op", choices=["add", "remove", "clear"])
    s.add_argument("account", nargs="?")
    s.add_argument("--owner-yes", default="")
    s = sub.add_parser("budget")
    s.add_argument("tid")
    s.add_argument("which", choices=["ai", "critique"])
    s.add_argument("tokens", type=int)
    s.add_argument("--owner-yes", default="")
    s = sub.add_parser("kb-scope")
    s.add_argument("tid")
    s.add_argument("scope", choices=list(tenancy.KB_SCOPES))
    s = sub.add_parser("block-attendee")
    s.add_argument("tid")
    s.add_argument("email")
    a = ap.parse_args(argv)

    tenancy.ensure_defaults()
    try:
        t = tenancy.get(a.tid)
    except tenancy.TenantNotFound:
        print(f"no tenant {a.tid}", file=sys.stderr)
        return 2
    try:
        if a.cmd == "show":
            v = settings.public_view(t)
            print(json.dumps({"tenant": t.to_dict(), "server_only": v["server_only"]}, indent=1))
        elif a.cmd == "switch":
            settings.set_box(t, a.key, a.state == "on", a.owner_yes)
            print(f"{a.key} = {a.state}")
        elif a.cmd == "allowlist":
            cur = list(t.get("meta_allowlist", []) or [])
            if a.op == "add":
                if not a.account:
                    raise ValueError("account id required")
                cur = sorted(set(cur) | {a.account})
            elif a.op == "remove":
                cur = [x for x in cur if x != a.account]
            else:
                cur = []
            reason = a.owner_yes if a.op == "add" else ""
            if a.op == "add":
                settings.set_box(t, "meta_allowlist", cur, reason)
            else:
                t.set("meta_allowlist", cur)
                from operator_ai import audit

                audit.log(t, "server-cli", "switch", {"key": "meta_allowlist", "to": f"{len(cur)} entries"})
            print(f"meta_allowlist: {len(cur)} entries")
        elif a.cmd == "budget":
            key = "ai_token_budget" if a.which == "ai" else "critique_token_budget"
            settings.set_box(t, key, a.tokens, a.owner_yes)
            print(f"{key} = {a.tokens}")
        elif a.cmd == "kb-scope":
            tenancy.set_kb_scope(a.tid, a.scope, actor="server-cli")
            print(f"kb_scope = {a.scope}")
        elif a.cmd == "block-attendee":
            cur = list(t.get("blocked_attendees", []) or [])
            cur.append(a.email)
            t.set("blocked_attendees", sorted(set(cur)))
            print(f"blocked_attendees: {len(set(cur))} entries")
    except tenancy.LicenceError as e:
        print(f"LicenceError: {e}", file=sys.stderr)
        return 3
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 4
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
