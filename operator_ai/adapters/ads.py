"""Ads adapters. Mock (practice account in SQLite) by default. The real Meta adapter
(live/meta.py) is used only when the tenant's live switch is on, the per-tenant allowlist is
non-empty and contains the account, and every write is an approved action."""
from __future__ import annotations

import secrets

from .. import db


class MockAdsAdapter:
    name = "mock"

    def __init__(self, tenant):
        self.tenant = tenant
        self.writes = 0

    # reads ------------------------------------------------------------------
    def _parse(self, r) -> dict:
        d = dict(r)
        d["data"] = db.jload(d.get("data"), {})
        d["metrics"] = db.jload(d.get("metrics"), {})
        return d

    def accounts(self) -> list[dict]:
        with self.tenant.db() as c:
            return [self._parse(r) for r in c.execute("SELECT * FROM mock_ads WHERE kind='account'")]

    def list(self, kind: str | None = None, parent: str | None = None) -> list[dict]:
        q, args = "SELECT * FROM mock_ads WHERE 1=1", []
        if kind:
            q += " AND kind=?"
            args.append(kind)
        if parent:
            q += " AND parent=?"
            args.append(parent)
        with self.tenant.db() as c:
            return [self._parse(r) for r in c.execute(q + " ORDER BY id", args)]

    def get(self, obj_id: str) -> dict | None:
        with self.tenant.db() as c:
            r = c.execute("SELECT * FROM mock_ads WHERE id=?", (obj_id,)).fetchone()
        return self._parse(r) if r else None

    def tree(self, account_id: str) -> list[dict]:
        out = []
        for cmp in self.list("campaign", account_id):
            for ad_set in self.list("adset", cmp["id"]):
                for ad in self.list("ad", ad_set["id"]):
                    out.append({**ad, "campaign": cmp, "adset": ad_set})
        return out

    # writes (only through the action gate) ----------------------------------
    def create(self, kind: str, parent: str | None, name: str, data: dict | None = None, daily_budget: float = 0.0,
               status: str = "PAUSED") -> str:
        obj_id = f"{kind[:3]}_{secrets.token_hex(4)}"
        with self.tenant.db() as c:
            c.execute("INSERT INTO mock_ads(id,kind,parent,name,status,daily_budget,data,metrics,updated) VALUES(?,?,?,?,?,?,?,?,?)",
                      (obj_id, kind, parent, name, status, daily_budget, db.jdump(data or {}), "{}", db.now()))
        self.writes += 1
        return obj_id

    def update(self, obj_id: str, **fields) -> None:
        allowed = {k: v for k, v in fields.items() if k in ("status", "daily_budget", "name")}
        if not allowed:
            return
        sets = ", ".join(f"{k}=?" for k in allowed)
        with self.tenant.db() as c:
            c.execute(f"UPDATE mock_ads SET {sets}, updated=? WHERE id=?", (*allowed.values(), db.now(), obj_id))
        self.writes += 1

    def snapshot(self, obj_id: str) -> dict | None:
        o = self.get(obj_id)
        return {"id": o["id"], "status": o["status"], "daily_budget": o["daily_budget"], "name": o["name"]} if o else None

    def restore(self, snap: dict) -> None:
        self.update(snap["id"], status=snap["status"], daily_budget=snap["daily_budget"], name=snap["name"])


def adapter_for(tenant, account_id: str | None = None):
    """Mock unless: live switch on AND allowlist non-empty AND the account is on it."""
    if not tenant.get("live_meta_enabled", False):
        return MockAdsAdapter(tenant)
    allow = tenant.get("meta_allowlist", []) or []
    if not allow or (account_id and account_id not in allow):
        return MockAdsAdapter(tenant)
    from ..live import meta

    return meta.MetaAdsAdapter(tenant, account_id or allow[0])
