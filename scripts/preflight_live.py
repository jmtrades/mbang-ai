#!/usr/bin/env python3
"""Go-live preflight: lists every check as OK or MISSING. Offline, read-only, never prints a value.

  scripts/preflight_live.py [--tenant owner]
"""
import argparse
import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from operator_ai import config, console, llm, tenancy, vault  # noqa: E402


def checks(tid: str) -> list[tuple[str, bool, str]]:
    cfg = config.get()
    tenancy.ensure_defaults()
    t = tenancy.get(tid)
    out: list[tuple[str, bool, str]] = []
    add = lambda name, ok, need="": out.append((name, bool(ok), need))
    add("Web app built (web/dist)", (config.ROOT / "web" / "dist" / "index.html").exists(), "cd web && npm run build")
    add("Course material in inputs/", not cfg.using_fixture_corpus, "copy the course folders into inputs/")
    add("Secure cookies (OPERATOR_COOKIE_SECURE=1)", cfg.cookie_secure, "set OPERATOR_COOKIE_SECURE=1 behind HTTPS")
    add("Public URL / domain (OPERATOR_PUBLIC_URL)", bool(os.environ.get("OPERATOR_PUBLIC_URL")), "set OPERATOR_PUBLIC_URL")
    add("Caddyfile (automatic HTTPS)", (config.ROOT / "deploy" / "Caddyfile").exists(), "deploy/Caddyfile")
    add("Backups (scripts/backup.sh)", (config.ROOT / "scripts" / "backup.sh").exists(), "scripts/backup.sh + timer")
    add("AI model key in environment", bool(config.llm_key()), "ANTHROPIC_API_KEY or OPENAI_API_KEY")
    add("AI token budget > 0 (optional)", llm.budget(t) > 0, "scripts/tenant_admin.py budget <tid> ai <n> --owner-yes ...")
    add("Embedding model cached locally (optional)", shutil.which("python3") is not None and
        __import__("operator_ai.embeddings", fromlist=["x"]).model_cached(), "scripts/fetch_embeddings.py")
    for it in console.INTEGRATIONS:
        if not it["fields"]:
            continue
        st = vault.status(t, it["fields"])
        missing = [f for f, v in st.items() if v != "set"]
        add(f"{it['name']} login", not missing, ", ".join(missing))
    add("Meta live switch", t.get("live_meta_enabled", False), "tenant_admin.py switch <tid> live_meta_enabled on --owner-yes ...")
    add("Meta account allowlist non-empty", bool(t.get("meta_allowlist", [])), "tenant_admin.py allowlist <tid> add <act_id> ...")
    add("Calendar live switch", t.get("live_calendar_enabled", False), "tenant_admin.py switch <tid> live_calendar_enabled on ...")
    add("Demo DB separate from real", tenancy.get("demo").db_path != t.db_path, "")
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tenant", default=config.get().default_tenant)
    a = ap.parse_args(argv)
    rows = checks(a.tenant)
    for name, ok, need in rows:
        print(f"{'OK     ' if ok else 'MISSING'}  {name}" + ("" if ok or not need else f"  ->  {need}"))
    missing = sum(1 for _, ok, _ in rows if not ok)
    print(f"\n{len(rows) - missing}/{len(rows)} OK, {missing} MISSING")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
