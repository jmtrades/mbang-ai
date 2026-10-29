"""Webinar engine / execution chain. One chain run produces: offer (USP formula), title and
hooks, a 7-segment slide script with timings, registration and thank-you page previews
(static HTML), show-up and replay sequences, run-of-show, chat and offer-moment prompts, and
the hand-off to sales. Each step feeds the next, each output is its own queue item linked by
chain_id, and re-running a step re-runs only the steps that depend on it."""
from __future__ import annotations

import html
import secrets

from . import brief as brief_mod
from . import db, queue, retrieval, runtime

STEPS = ["offer", "title_hooks", "slide_script", "reg_page", "thankyou_page", "showup_sequence", "replay_sequence",
         "run_of_show", "chat_prompts", "sales_handoff"]
DEPS = {
    "offer": [], "title_hooks": ["offer"], "slide_script": ["offer", "title_hooks"], "reg_page": ["offer", "title_hooks"],
    "thankyou_page": ["reg_page"], "showup_sequence": ["title_hooks"], "replay_sequence": ["title_hooks"],
    "run_of_show": ["slide_script"], "chat_prompts": ["slide_script"], "sales_handoff": ["offer"],
}
LABELS = {
    "offer": "Offer (USP formula)", "title_hooks": "Title and hooks", "slide_script": "7-segment slide script",
    "reg_page": "Registration page preview", "thankyou_page": "Thank-you page preview", "showup_sequence": "Show-up sequence",
    "replay_sequence": "Replay sequence", "run_of_show": "Run-of-show", "chat_prompts": "Chat and offer-moment prompts",
    "sales_handoff": "Hand-off to sales",
}
SEGMENTS = [("Hook and promise", 5), ("Origin and credibility", 7), ("The big mistake", 10), ("The new mechanism", 15),
            ("Proof framework", 10), ("The offer", 12), ("Questions and close", 6)]
KIND = {"reg_page": "page", "thankyou_page": "page", "showup_sequence": "email_sequence", "replay_sequence": "email_sequence",
        "sales_handoff": "dm"}


def descendants(step: str) -> list[str]:
    out: list[str] = []
    for s in STEPS:
        if s == step:
            continue
        stack = list(DEPS[s])
        seen = set()
        while stack:
            d = stack.pop()
            if d == step:
                out.append(s)
                break
            if d not in seen:
                seen.add(d)
                stack.extend(DEPS[d])
    return out


def _page(title: str, sub: str, bullets: list[str], cta: str) -> str:
    lis = "".join(f"<li>{html.escape(b)}</li>" for b in bullets)
    return (f"<!doctype html><html><head><meta charset='utf-8'><title>{html.escape(title)}</title>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<style>body{font-family:system-ui;max-width:640px;margin:40px auto;padding:0 16px;color:#16181d}"
            "h1{font-size:28px}a.cta{display:inline-block;background:#5b6cff;color:#fff;padding:12px 18px;border-radius:10px;"
            "text-decoration:none}</style></head><body>"
            f"<h1>{html.escape(title)}</h1><p>{html.escape(sub)}</p><ul>{lis}</ul><a class='cta' href='#'>{html.escape(cta)}</a>"
            "<p style='font-size:12px;color:#5d6270'>Preview only. Not published.</p></body></html>")


def _make(step: str, b: dict, outs: dict) -> dict:
    """Pure template builders. Each returns {title, body, meta}."""
    if step == "offer":
        usp = (f"I help {b['audience']} get {b['outcome']} in {b['timeframe']} through our {b['mechanism']} "
               f"without {b['pain']}.")
        return {"title": "Offer (USP)", "body": usp, "data": {"usp": usp}}
    if step == "title_hooks":
        # the people the client serves, never the niche label ("How new traders get", not "How trading education get")
        title = f"How {b['audience']} get {b['outcome']} without {b['pain']}"
        hooks = [f"The {b['mechanism']}, step by step, in 60 minutes.",
                 f"Why most {b['audience']} stay stuck (and the simple fix).",
                 f"How to get {b['outcome'].replace('their ', 'your ')} in {b['timeframe']}: the exact plan.",
                 "Live Q&A: bring your questions."]
        return {"title": title, "body": title + "\n" + "\n".join(f"- {h}" for h in hooks), "data": {"title": title, "hooks": hooks}}
    if step == "slide_script":
        t = outs["title_hooks"]["data"]["title"]
        lines = [f"Segment {i + 1}: {name} ({mins} min)" for i, (name, mins) in enumerate(SEGMENTS)]
        notes = {
            1: f"Promise: by the end you'll know exactly how {b['audience']} get {b['outcome']}.",
            2: "Your story: where you started, what changed, and why you teach this.",
            3: f"The big mistake most {b['audience']} make, and what it costs them.",
            4: f"Teach the {b['mechanism']} step by step.",
            5: "Proof: show how to check the method works; only verified results with permission.",
            6: f"The offer: {b['offer']}; who it is for and who it is not for.", 7: "Answer real questions from the chat.",
        }
        body = f"{t}\n" + "\n".join(l + (f" — {notes[i + 1]}" if i + 1 in notes else "") for i, l in enumerate(lines))
        return {"title": f"Slide script: {t}", "body": body, "data": {"segments": SEGMENTS, "minutes": sum(m for _, m in SEGMENTS)}}
    if step == "reg_page":
        t = outs["title_hooks"]["data"]["title"]
        page = _page(t, f"Free live training on {b['date']}.", outs["title_hooks"]["data"]["hooks"], "Save my seat")
        return {"title": f"Registration page: {t}", "body": page, "data": {"html": page}}
    if step == "thankyou_page":
        page = _page("You're registered", f"See you {b['date']}. Add it to your calendar and reply to the confirmation email.",
                     ["Watch the 2-minute primer video", "Write down your one question for the Q&A"], "Add to calendar")
        return {"title": "Thank-you page", "body": page, "data": {"html": page}}
    if step == "showup_sequence":
        t = outs["title_hooks"]["data"]["title"]
        emails = [f"Subject: You're in: {t}\nHere's the link and the time ({b['date']}). One thing to think about before we start: what's your biggest blocker right now?",
                  "Subject: Tomorrow: one quick prep step\nHere's a 2-minute video to watch before the live training.",
                  "Subject: Starting in one hour\nGrab a pen. Here's the link.",
                  "Subject: We're live\nJoin now; we start with the big mistake most people make."]
        return {"title": "Show-up sequence (4)", "body": "\n\n".join(emails), "data": {"count": len(emails)}}
    if step == "replay_sequence":
        emails = ["Subject: Your replay\nHere's the replay. It stays up for 48 hours because we update the examples for each live session.",
                  f"Subject: The one slide people asked about\nHere's the {b['mechanism']} slide again with a short walkthrough.",
                  "Subject: Replay comes down tonight\nThe replay comes down at midnight because we update it before the next live session."]
        return {"title": "Replay sequence (3)", "body": "\n\n".join(emails), "data": {"count": len(emails)}}
    if step == "run_of_show":
        t = 0
        rows = []
        for name, mins in SEGMENTS:
            rows.append(f"{t:02d}:00 – {name} ({mins} min)")
            t += mins
        return {"title": "Run-of-show", "body": "\n".join(rows) + f"\nTotal: {t} min", "data": {"total": t}}
    if step == "chat_prompts":
        prompts = ["First 2 minutes: type one word that describes your week.",
                   "Segment 3: type 'me' if you've made this mistake.", "Segment 4: what's the first step you'd try?",
                   "Offer moment: 'Would it be useful if I showed you how we can help you do this?' (ask permission first)"]
        return {"title": "Chat and offer-moment prompts", "body": "\n".join(f"- {p}" for p in prompts), "data": {"prompts": prompts}}
    if step == "sales_handoff":
        # written to the attendee about their own business, never naming the operator's business as theirs
        dm = (f"Thanks for joining the training. What stood out most for you? If it's useful, I can show you how the "
              f"{b['mechanism']} could work for you.")
        return {"title": "Hand-off to sales (setter DM)", "body": dm,
                "data": {"rule": "Anyone who asks about the offer gets a setter DM within one hour."}}
    raise KeyError(step)


def _meta(step: str, chain_id: str) -> dict:
    m = {"chain_id": chain_id, "step": step}
    if step == "sales_handoff":
        m.update(prospect_facing=True, lead_tier="cold", prior_contact=True)
    else:
        m.update(client_asset=True, prior_contact=False)
    return m


def _cite(tenant, step: str) -> list[dict]:
    q = {"slide_script": "seven segment webinar timings", "showup_sequence": "reminders one day before one hour before go live",
         "replay_sequence": "send the replay within two hours forty-eight hours", "chat_prompts": "running the chat engagement question",
         "offer": "USP formula who outcome timeframe mechanism pain", "sales_handoff": "hand-off to sales setter DM within one hour",
         "run_of_show": "seven segment webinar timings", "title_hooks": "hook and promise", "reg_page": "registration confirmation",
         "thankyou_page": "registration confirmation date time link"}.get(step)
    if not q:
        return []
    hits = retrieval.search(tenant, q, k=1)
    return [retrieval.cite({**hits[0], "id": "S1"})] if hits else []


def _run_steps(tenant, chain_id: str, steps: list[str], b: dict, outs: dict, run: runtime.Run, actor: str, version: int) -> list[int]:
    qids = []
    for st in steps:
        def _go(st=st):
            o = _make(st, b, outs)
            o["citations"] = _cite(tenant, st)
            return o

        o = run.step(st, {"deps": DEPS[st]}, _go, LABELS[st])
        outs[st] = o
        item = queue.add(tenant, KIND.get(st, "webinar_asset"), o["body"], channel="manual" if st != "sales_handoff" else "instagram_dm",
                         title=f"[{LABELS[st]}] {o['title']}"[:200], meta={**_meta(st, chain_id), "version": version,
                                                                             "citations": o["citations"]},
                         run_id=run.id, actor=actor, demo=tenant.is_demo)
        with tenant.db() as c:
            c.execute("INSERT INTO chain_steps(chain_id,step,deps,output,queue_id,run_id,version,created) VALUES(?,?,?,?,?,?,?,?)",
                      (chain_id, st, db.jdump(DEPS[st]), db.jdump(o), item["id"], run.id, version, db.now()))
        qids.append(item["id"])
    return qids


def run_chain(tenant, partial_brief: dict | None = None, user: dict | None = None) -> dict:
    actor = f"{user['username']}({user['role']})" if user else "system"
    built = brief_mod.build(tenant, partial_brief)
    b = built["brief"]
    chain_id = f"wc-{secrets.token_hex(4)}"

    def fn(run: runtime.Run):
        run.note("brief", partial_brief or {}, {"brief": b, "missing": built["missing"]}, label="Build brief")
        with tenant.db() as c:
            c.execute("INSERT INTO chain_steps(chain_id,step,deps,output,version,created) VALUES(?,?,?,?,?,?)",
                      (chain_id, "_brief", "[]", db.jdump(b), 0, db.now()))
        qids = _run_steps(tenant, chain_id, STEPS, b, {}, run, actor, 1)
        return {"chain_id": chain_id, "queue_ids": qids, "steps": STEPS, "brief": b, "missing": built["missing"]}

    out = runtime.execute(tenant, "webinar_chain", f"Webinar chain {chain_id}", ["Build brief"] + [LABELS[s] for s in STEPS], fn, actor)
    return {**out["result"], "run_id": out["run_id"]}


def state(tenant, chain_id: str) -> dict:
    with tenant.db() as c:
        rows = db.rows(c.execute("SELECT * FROM chain_steps WHERE chain_id=? ORDER BY id", (chain_id,)))
    if not rows:
        raise KeyError(chain_id)
    latest: dict[str, dict] = {}
    b = {}
    for r in rows:
        r["output"] = db.jload(r["output"], {})
        if r["step"] == "_brief":
            b = r["output"]
            continue
        latest[r["step"]] = r
    return {"chain_id": chain_id, "brief": b, "steps": [
        {"step": s, "label": LABELS[s], "deps": DEPS[s], "version": latest[s]["version"] if s in latest else 0,
         "queue_id": latest[s]["queue_id"] if s in latest else None,
         "output": latest[s]["output"] if s in latest else None} for s in STEPS]}


def rerun_step(tenant, chain_id: str, step: str, brief_patch: dict | None = None, user: dict | None = None) -> dict:
    """Re-run one step and only the steps that depend on it. Superseded open drafts are rejected."""
    if step not in STEPS:
        raise KeyError(step)
    actor = f"{user['username']}({user['role']})" if user else "system"
    st = state(tenant, chain_id)
    b = {**st["brief"], **(brief_patch or {})}
    outs = {s["step"]: s["output"] for s in st["steps"] if s["output"]}
    targets = [step] + descendants(step)
    targets = [s for s in STEPS if s in targets]
    version = max(s["version"] for s in st["steps"]) + 1

    def fn(run: runtime.Run):
        for s in st["steps"]:
            if s["step"] in targets and s["queue_id"]:
                item = queue.get(tenant, s["queue_id"])
                if item and item["status"] in queue.OPEN:
                    queue.reject(tenant, s["queue_id"], actor, reason=f"superseded by version {version}")
        if brief_patch:
            with tenant.db() as c:
                c.execute("INSERT INTO chain_steps(chain_id,step,deps,output,version,created) VALUES(?,?,?,?,?,?)",
                          (chain_id, "_brief", "[]", db.jdump(b), version, db.now()))
        qids = _run_steps(tenant, chain_id, targets, b, outs, run, actor, version)
        return {"chain_id": chain_id, "rerun": targets, "queue_ids": qids, "version": version}

    out = runtime.execute(tenant, "webinar_chain", f"Re-run {step} in {chain_id}", [LABELS[s] for s in targets], fn, actor)
    return {**out["result"], "run_id": out["run_id"]}


# --------------------------------------------------------------------------- rehearsal scorer (B10)
def rehearsal_score(tenant, script: str, minutes: dict[str, float] | None = None) -> dict:
    """Score a webinar rehearsal transcript/script against the 7-segment SOP."""
    from . import critique

    res = critique.critique(tenant, script, "webinar", {"client_asset": True})
    seg_scores = []
    for i, (name, target) in enumerate(SEGMENTS, start=1):
        got = (minutes or {}).get(str(i)) or (minutes or {}).get(name)
        if got is None:
            seg_scores.append({"segment": i, "name": name, "target": target, "actual": None, "ok": None})
        else:
            ok = target * 0.6 <= float(got) <= target * 1.4
            seg_scores.append({"segment": i, "name": name, "target": target, "actual": float(got), "ok": ok})
    with tenant.db() as c:
        c.execute("INSERT INTO rehearsals(title,script,score,created) VALUES(?,?,?,?)",
                  (script[:80], script, db.jdump({"score": res["score"], "segments": seg_scores}), db.now()))
    return {"score": res["score"], "segments": seg_scores, "fixes": [c for c in res["criteria"] if c["score"] < 10],
            "citations": res["citations"], "flags": res["flags"]}
