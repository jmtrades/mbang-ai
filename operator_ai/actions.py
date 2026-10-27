"""Action gate. Writes to ad accounts and terminal CRM stage moves are proposals first:
they become owner-only queue items, execute only when approved, store the prior state, and
can be rolled back in one click. Ad creates are forced to PAUSED with zero budget and re-read;
anything that comes back not paused is forced back to PAUSED and the action marked failed.

Partial failure: each op's prior snapshot is written to the action BEFORE that op's write, and an adapter
error stops the run. The action is then "failed_partial" with prior_state holding exactly what was
applied, so one-click rollback restores it, and the queue item is marked send_failed with the reason."""
from __future__ import annotations

from . import audit, db, queue
from .adapters.ads import adapter_for
from .redact import redact


class ActionError(Exception):
    pass


def _row(tenant, action_id: int) -> dict:
    with tenant.db() as c:
        r = db.row(c.execute("SELECT * FROM actions WHERE id=?", (action_id,)).fetchone())
    if r is None:
        raise ActionError("Action not found")
    for k in ("proposal", "prior_state", "result"):
        r[k] = db.jload(r[k], None)
    return r


def _insert(tenant, kind: str, proposal: dict, status: str, actor: str, rollback_of: int | None = None) -> int:
    with tenant.db() as c:
        return c.execute("INSERT INTO actions(kind,proposal,status,rollback_of,created,decided_by) VALUES(?,?,?,?,?,?)",
                         (kind, db.jdump(proposal), status, rollback_of, db.now(), actor)).lastrowid


def describe(p: dict) -> str:
    ops = p.get("ops", [])
    parts = []
    for o in ops:
        if o["op"] == "pause":
            parts.append(f"Pause {o['id']}")
        elif o["op"] == "activate":
            parts.append(f"Activate {o['id']} at {o.get('daily_budget', 0):.0f}/day")
        elif o["op"] == "set_budget":
            parts.append(f"Set {o['id']} budget to {o['daily_budget']:.0f}/day")
        elif o["op"] == "create_test":
            parts.append(f"Create PAUSED test '{o['name']}' ({len(o.get('creatives', []))} creatives, budget 0)")
        elif o["op"] == "crm_move":
            parts.append(f"Move contact {o['contact_id']} to {o['stage']}")
    return "; ".join(parts) + (f". Why: {p['why']}" if p.get("why") else "")


def propose(tenant, kind: str, proposal: dict, actor: str, title: str = "", run_id: int | None = None) -> dict:
    """Queue an owner-only approval. Nothing is written to the account until it's approved."""
    aid = _insert(tenant, kind, proposal, "proposed", actor)
    item = queue.add(tenant, kind, describe(proposal), channel="ads" if kind == "ad_change" else "crm",
                     recipient_ref=proposal.get("account"), title=title or describe(proposal)[:120],
                     meta={"action_id": aid, "owner_only": True}, run_id=run_id, actor=actor, demo=tenant.is_demo)
    with tenant.db() as c:
        c.execute("UPDATE actions SET queue_id=? WHERE id=?", (item["id"], aid))
    audit.log(tenant, actor, "action", {"id": aid, "kind": kind, "status": "proposed"})
    return {"action_id": aid, "queue_id": item["id"], "status": item["status"]}


def _save_prior(tenant, action_id: int | None, prior: list[dict]) -> None:
    """Persist what is about to change before changing it, so a crash or an adapter error mid-way
    still leaves a restorable record."""
    if action_id is None:
        return
    with tenant.db() as c:
        c.execute("UPDATE actions SET prior_state=?, status='executing' WHERE id=?", (db.jdump(prior), action_id))


def _apply_ad_ops(tenant, ad, ops: list[dict], action_id: int | None = None) -> tuple[list[dict], list[dict], bool, str | None]:
    """Apply ad ops; returns (prior snapshots, results, ok, error). Stops at the first adapter error."""
    prior, results, ok = [], [], True
    for i, o in enumerate(ops):
        try:
            if o["op"] in ("pause", "activate", "set_budget"):
                snap = ad.snapshot(o["id"])
                if snap is None:
                    results.append({"op": o["op"], "id": o["id"], "error": "not found"})
                    ok = False
                    continue
                prior.append(snap)
                _save_prior(tenant, action_id, prior)
                if o["op"] == "pause":
                    ad.update(o["id"], status="PAUSED")
                elif o["op"] == "activate":
                    ad.update(o["id"], status="ACTIVE", daily_budget=float(o.get("daily_budget", snap["daily_budget"])))
                else:
                    ad.update(o["id"], daily_budget=float(o["daily_budget"]))
                results.append({"op": o["op"], "id": o["id"], "after": ad.snapshot(o["id"])})
            elif o["op"] == "create_test":
                created: list[str] = []

                def _made(cid: str) -> str:
                    created.append(cid)
                    prior.append({"id": cid, "created": True})
                    _save_prior(tenant, action_id, prior)
                    return cid

                cmp = _made(ad.create("campaign", o["account"], o["name"], {"objective": "LEADS"}, 0.0, "PAUSED"))
                ad_set = _made(ad.create("adset", cmp, f"{o['name']} ad set", {"audience": o.get("audience", "")}, 0.0, "PAUSED"))
                for j, cr in enumerate(o.get("creatives", [])):
                    _made(ad.create("ad", ad_set, f"{o['name']} creative {chr(65 + j)}", {"hook": cr}, 0.0, "PAUSED"))
                # re-read: anything not PAUSED (or with budget) is forced back and the action fails
                bad = []
                for cid in created:
                    now = ad.snapshot(cid)
                    if now is None or now["status"] != "PAUSED" or (now["daily_budget"] or 0) != 0:
                        bad.append(cid)
                        ad.update(cid, status="PAUSED", daily_budget=0.0)
                if bad:
                    ok = False
                results.append({"op": "create_test", "created": created, "forced_paused": bad})
            else:
                results.append({"op": o["op"], "error": "unknown op"})
                ok = False
        except Exception as e:  # noqa: BLE001 - stop here; what was applied is in prior and can be rolled back
            err = redact(f"Step {i + 1} of {len(ops)} ({o.get('op')}) failed: {type(e).__name__}")
            results.append({"op": o.get("op"), "id": o.get("id"), "error": err})
            return prior, results, False, err
    return prior, results, ok, None


def _apply_crm_ops(tenant, ops: list[dict], action_id: int | None = None) -> tuple[list[dict], list[dict], bool, str | None]:
    from . import crm

    prior, results, ok = [], [], True
    for i, o in enumerate(ops):
        try:
            p, r, k = crm.apply_ops(tenant, [o])
        except Exception as e:  # noqa: BLE001 - stop; earlier moves are in prior and can be rolled back
            err = redact(f"Step {i + 1} of {len(ops)} (crm_move) failed: {type(e).__name__}")
            results.append({"contact_id": o.get("contact_id"), "error": err})
            return prior, results, False, err
        prior += p
        results += r
        ok = ok and k
        _save_prior(tenant, action_id, prior)
    return prior, results, ok, None


def execute(tenant, action_id: int, actor: str) -> dict:
    a = _row(tenant, action_id)
    if a["status"] not in ("proposed", "approved"):
        raise ActionError(f"Action is {a['status']}")
    with tenant.db() as c:  # claim: a second approval of the same action never runs it twice
        if c.execute("UPDATE actions SET status='executing' WHERE id=? AND status IN ('proposed','approved')",
                     (action_id,)).rowcount != 1:
            raise ActionError("Action is already running")
    p = a["proposal"]
    if a["kind"] == "ad_change":
        try:
            ad = adapter_for(tenant, p.get("account"))
        except Exception as e:  # noqa: BLE001 - nothing was applied
            prior, results, ok, err = [], [], False, redact(f"Could not open the ad account: {type(e).__name__}")
        else:
            prior, results, ok, err = _apply_ad_ops(tenant, ad, p.get("ops", []), action_id)
    elif a["kind"] == "crm_move":
        prior, results, ok, err = _apply_crm_ops(tenant, p.get("ops", []), action_id)
    else:
        with tenant.db() as c:
            c.execute("UPDATE actions SET status=? WHERE id=?", (a["status"], action_id))
        raise ActionError("Unknown action kind")
    applied = sum(1 for r in results if not r.get("error"))
    if ok:
        status = "done"
    elif err and prior:
        status = "failed_partial"  # prior holds every snapshot taken, including the step that failed mid-write
    else:
        status = "failed"
    with tenant.db() as c:
        c.execute("UPDATE actions SET status=?, prior_state=?, result=?, decided_by=? WHERE id=?",
                  (status, db.jdump(prior), db.jdump({"results": results, "error": err, "applied": applied}
                                                     if err else results), actor, action_id))
    audit.log(tenant, actor, "action", {"id": action_id, "kind": a["kind"], "status": status})
    return {"action_id": action_id, "status": status, "results": results, "error": err, "applied": applied,
            "steps": len(p.get("ops", []))}


def run_direct(tenant, kind: str, proposal: dict, actor: str) -> dict:
    """Only for creating PAUSED, zero-budget objects on the mock practice account. Still
    recorded as an action with prior state, so it can be rolled back."""
    ad = adapter_for(tenant, proposal.get("account"))
    if ad.name != "mock" or any(o["op"] != "create_test" for o in proposal.get("ops", [])):
        raise ActionError("Only PAUSED test builds on the practice account skip the approval queue")
    aid = _insert(tenant, kind, proposal, "approved", actor)
    return execute(tenant, aid, actor)


@queue.on_approve("ad_change")
@queue.on_approve("crm_move")
def _approved(tenant, item: dict, actor: str) -> None:
    aid = item["meta"].get("action_id")
    if not aid:
        return
    try:
        res = execute(tenant, aid, actor)
    except ActionError as e:
        raise queue.HookFailed(str(e), 409)
    if res["status"] == "failed_partial":
        raise queue.HookFailed(f"Only part of this change was applied ({res['applied']} of {res['steps']} steps). "
                               f"{res['error']}. Roll it back from the action history, then propose it again.", 502)
    if res["status"] == "failed":
        raise queue.HookFailed(res.get("error") or "The change could not be applied: "
                               + "; ".join(str(r.get("error")) for r in res["results"] if r.get("error"))[:300], 409)


def rollback(tenant, action_id: int, actor: str) -> dict:
    a = _row(tenant, action_id)
    if a["status"] not in ("done", "failed", "failed_partial"):
        raise ActionError(f"Only executed actions can be rolled back (this one is {a['status']})")
    prior = a["prior_state"] or []
    if a["kind"] == "ad_change":
        ad = adapter_for(tenant, a["proposal"].get("account"))
        for snap in prior:
            if snap.get("created"):
                ad.update(snap["id"], status="ARCHIVED", daily_budget=0.0)
            else:
                ad.restore(snap)
    else:
        from . import crm

        crm.restore(tenant, prior)
    rid = _insert(tenant, a["kind"], {"rollback_of": action_id}, "done", actor, rollback_of=action_id)
    with tenant.db() as c:
        c.execute("UPDATE actions SET status='rolled_back' WHERE id=?", (action_id,))
        c.execute("UPDATE actions SET prior_state=? WHERE id=?", (db.jdump(prior), rid))
    audit.log(tenant, actor, "action.rollback", {"id": action_id, "rollback_action": rid})
    return {"rolled_back": action_id, "rollback_action": rid}


def history(tenant, limit: int = 100) -> list[dict]:
    with tenant.db() as c:
        rows = db.rows(c.execute("SELECT * FROM actions ORDER BY id DESC LIMIT ?", (limit,)))
    for r in rows:
        for k in ("proposal", "prior_state", "result"):
            r[k] = db.jload(r[k], None)
        r["summary"] = describe(r["proposal"]) if isinstance(r["proposal"], dict) and r["proposal"].get("ops") else (
            f"Rollback of action #{r['rollback_of']}" if r["rollback_of"] else "")
    return rows
