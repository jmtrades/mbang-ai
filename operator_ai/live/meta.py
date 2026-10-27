"""Meta: signed lead webhook (X-Hub-Signature-256, HMAC-SHA256 over the raw body, constant
time, tenant app secret or a random per-tenant practice secret before connection, bodies over
256 KB rejected, GET handshake checks the tenant's verify token), lead fetch with the tenant's
token and appsecret_proof, and the real Ads adapter (coded, used only behind the switch,
allowlist and approvals)."""
from __future__ import annotations

import hashlib
import hmac

from .. import vault
from . import net

GRAPH = "https://graph.facebook.com/v21.0"
MAX_BODY = 256 * 1024


class WebhookRejected(Exception):
    def __init__(self, status: int, msg: str):
        super().__init__(msg)
        self.status = status


def app_secret(tenant) -> str:
    """The connected app secret, or a random practice secret so the webhook is never unsigned."""
    return vault.get(tenant, "meta_app_secret") or vault.ensure_practice_secret(tenant, "meta_practice_secret")


def sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def verify_signature(tenant, raw: bytes, header: str | None) -> None:
    if len(raw) > MAX_BODY:
        raise WebhookRejected(413, "Body too large")
    if not header or not header.startswith("sha256="):
        raise WebhookRejected(401, "Missing signature")
    if not hmac.compare_digest(header, sign(app_secret(tenant), raw)):
        raise WebhookRejected(401, "Bad signature")


def verify_handshake(tenant, mode: str | None, token: str | None, challenge: str | None) -> str:
    expected = vault.get(tenant, "meta_verify_token") or vault.ensure_practice_secret(tenant, "meta_practice_verify")
    if mode != "subscribe" or not token or not hmac.compare_digest(token, expected):
        raise WebhookRejected(403, "Verify token mismatch")
    return challenge or ""


def appsecret_proof(token: str, secret: str) -> str:
    return hmac.new(secret.encode(), token.encode(), hashlib.sha256).hexdigest()


def fetch_lead(tenant, leadgen_id: str) -> dict:
    token = vault.get(tenant, "meta_access_token")
    secret = vault.get(tenant, "meta_app_secret")
    if not token or not secret:
        raise net.LiveError("Meta login missing (meta_access_token / meta_app_secret)")
    r = net.request(tenant.id, "GET", f"{GRAPH}/{leadgen_id}",
                    params={"access_token": token, "appsecret_proof": appsecret_proof(token, secret),
                            "fields": "field_data,created_time,ad_id,form_id"})
    data = net.ok_json(r)
    fields = {f["name"]: (f.get("values") or [""])[0] for f in data.get("field_data", [])}
    return {"leadgen_id": leadgen_id, "fields": fields, "ad_id": data.get("ad_id"), "form_id": data.get("form_id")}


class MetaAdsAdapter:
    """Graph API ads adapter with the same interface as the mock. Creates are always PAUSED
    with zero budget; the action gate re-reads and forces anything else back to PAUSED."""

    name = "meta"

    def __init__(self, tenant, account_id: str):
        allow = tenant.get("meta_allowlist", []) or []
        if not tenant.get("live_meta_enabled", False) or account_id not in allow:
            raise PermissionError("Meta adapter needs the live switch and an allowlisted account")
        self.tenant = tenant
        self.account = account_id
        self.writes = 0

    def _auth(self) -> dict:
        token = vault.get(self.tenant, "meta_access_token")
        secret = vault.get(self.tenant, "meta_app_secret")
        if not token or not secret:
            raise net.LiveError("Meta login missing")
        return {"access_token": token, "appsecret_proof": appsecret_proof(token, secret)}

    def tree(self, account_id: str, date_preset: str | None = None) -> list[dict]:
        """`date_preset` (e.g. "last_7d") limits the insights window; without it Meta's default window applies."""
        insights_field = ("insights" + (f".date_preset({date_preset})" if date_preset else "")
                          + "{spend,actions,impressions,clicks}")
        r = net.request(self.tenant.id, "GET", f"{GRAPH}/{account_id}/ads",
                        params={**self._auth(), "fields": "id,name,status,adset{id,name,daily_budget},campaign{id,name},"
                                                          + insights_field})
        out = []
        for a in net.ok_json(r).get("data", []):
            ins = ((a.get("insights") or {}).get("data") or [{}])[0]
            leads = sum(int(x.get("value", 0)) for x in ins.get("actions", []) if x.get("action_type") == "lead")
            out.append({"id": a["id"], "name": a["name"], "status": a["status"], "campaign": a.get("campaign", {}),
                        "adset": a.get("adset", {}), "metrics": {"spend": float(ins.get("spend", 0)), "leads": leads,
                                                                 "impressions": int(ins.get("impressions", 0)),
                                                                 "clicks": int(ins.get("clicks", 0))}})
        return out

    def get(self, obj_id: str) -> dict | None:
        r = net.request(self.tenant.id, "GET", f"{GRAPH}/{obj_id}", params={**self._auth(),
                                                                          "fields": "id,name,status,daily_budget"})
        if r.status_code == 404:
            return None
        d = net.ok_json(r)
        return {"id": d["id"], "name": d.get("name"), "status": d.get("status"),
                "daily_budget": float(d.get("daily_budget") or 0) / 100}

    def read_account(self, account_id: str) -> dict | None:
        """Read an ad account. None when it isn't visible to this business (no partner access yet): Graph answers
        that with error 100 / 10 / 200; anything else (a bad login, an outage) raises."""
        r = net.request(self.tenant.id, "GET", f"{GRAPH}/{account_id}",
                        params={**self._auth(), "fields": "id,name,account_status"})
        if r.status_code in (400, 403, 404):
            try:
                code = int(((r.json() or {}).get("error") or {}).get("code"))
            except (ValueError, TypeError, AttributeError):
                code = None
            if r.status_code == 404 or code in (10, 100, 200):
                return None
        d = net.ok_json(r)
        return {"id": d["id"], "name": d.get("name"), "account_status": d.get("account_status")}

    def snapshot(self, obj_id: str) -> dict | None:
        return self.get(obj_id)

    def create(self, kind: str, parent: str | None, name: str, data: dict | None = None, daily_budget: float = 0.0,
               status: str = "PAUSED") -> str:
        edge = {"campaign": f"{self.account}/campaigns", "adset": f"{self.account}/adsets", "ad": f"{self.account}/ads"}[kind]
        body = {"name": name, "status": "PAUSED"}  # always PAUSED, whatever the caller asked
        if kind == "campaign":
            body.update(objective="OUTCOME_LEADS", special_ad_categories=[])
        if kind == "adset":
            body.update(campaign_id=parent, daily_budget=0, billing_event="IMPRESSIONS")
        if kind == "ad":
            body.update(adset_id=parent, creative={"title": (data or {}).get("hook", name)})
        r = net.request(self.tenant.id, "POST", f"{GRAPH}/{edge}", params=self._auth(), json_body=body, idempotent=False)
        self.writes += 1
        return net.ok_json(r)["id"]

    def update(self, obj_id: str, **fields) -> None:
        body = {}
        if "status" in fields:
            body["status"] = fields["status"]
        if "daily_budget" in fields:
            body["daily_budget"] = int(round(float(fields["daily_budget"]) * 100))
        if "name" in fields:
            body["name"] = fields["name"]
        r = net.request(self.tenant.id, "POST", f"{GRAPH}/{obj_id}", params=self._auth(), json_body=body,
                        idempotent=True)  # setting absolute values is safe to repeat
        net.ok_json(r)
        self.writes += 1

    def restore(self, snap: dict) -> None:
        self.update(snap["id"], status=snap["status"], daily_budget=snap["daily_budget"], name=snap["name"])
