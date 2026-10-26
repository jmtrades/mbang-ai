"""/ready report: every dependency the app needs, with an honest state for each."""
from __future__ import annotations

import time

from . import config, tenancy


def ai_state(tenant=None) -> dict:
    from . import llm

    return llm.state(tenant)


def ready() -> dict:
    cfg = config.get()
    out: dict = {"ok": True, "checks": {}}
    checks = out["checks"]
    try:
        tenants = tenancy.list_all()
        checks["db"] = {"ok": True, "tenants": len(tenants)}
    except Exception as e:  # noqa: BLE001
        checks["db"] = {"ok": False, "error": type(e).__name__}
        out["ok"] = False
        tenants = []
    try:
        from . import index

        st = index.status()
        checks["index"] = st
        checks["embeddings"] = {"ok": True, "backend": st.get("embeddings", "unknown")}
    except Exception as e:  # noqa: BLE001
        checks["index"] = {"ok": False, "error": type(e).__name__}
        checks["embeddings"] = {"ok": False, "backend": "unavailable"}
    try:
        from . import workers

        checks["workers"] = {"ok": workers.running() > 0, "running": workers.running()}
    except Exception:  # noqa: BLE001
        checks["workers"] = {"ok": False, "running": 0}
    try:
        owner = tenancy.get(cfg.default_tenant)
        checks["llm"] = ai_state(owner)
        checks["ads_adapter"] = {"adapter": "meta" if owner.get("live_meta_enabled") else "mock"}
        checks["allowlist_size"] = len(owner.get("meta_allowlist", []) or [])
        checks["live_switches"] = {
            t.id: {"live_meta_enabled": bool(t.get("live_meta_enabled", False)),
                   "live_calendar_enabled": bool(t.get("live_calendar_enabled", False))}
            for t in tenants
        }
    except Exception as e:  # noqa: BLE001
        checks["llm"] = {"ok": False, "error": type(e).__name__}
    checks["tenants"] = [t.to_dict() for t in tenants]
    checks["corpus"] = {"dir": "fixture" if cfg.using_fixture_corpus else "inputs"}
    out["at"] = time.time()
    return out
