"""AI departments builder (the headline demo).

Generate departments: deterministic, rules only, no AI call, well under a second. Five
departments, each with two agents (job, instructions, inputs, trigger, KPI, guardrails and
the DEMO input it runs on).

Run on demo data: a real traced run with five steps (load input -> rules draft -> AI pass ->
review -> queue). The AI pass has a 25 s hard deadline, a daily cap of 200 builder calls and
is off with OPERATOR_BUILDER_AI=0. If the AI is off, slow, failed or its draft trips a rule,
the rules draft is used and labelled "Rules draft (AI not used): <reason>". Nothing sends.
"""
from __future__ import annotations

import re
import time

from . import config, db, guardrails, llm, queue, rubrics, runtime

BUILDER_DEADLINE_S = 25.0
GUARDS = ["No price in DMs", "One question per message", "Everything goes to approval"]

PRESETS = {
    "physio": {
        "key": "physio", "name": "Kinetic Physio Coaching (DEMO)", "niche": "physiotherapy clinic owners",
        "offer": "Clinic Growth Coaching (DEMO)", "price": 2500, "audience": "independent physio clinic owners",
        "outcome": "a full diary of new patients", "mechanism": "referral-first patient system",
        "channels": "Instagram DMs, Meta ads, webinars",
        "faq": "How long until results? Most clinics see more bookings within 90 days (DEMO).",
        "dms": [
            ("Jess Marlowe (DEMO)", "Hi! Saw your post about referral nights. Does that work for a small clinic with 2 rooms?"),
            ("Tom Whitcombe (DEMO)", "How much is the coaching? I'm a solo physio and money is tight."),
            ("Priya Anand (DEMO)", "We get lots of enquiries but people don't book. Can you help with that?"),
        ],
    },
    "bookkeeping": {
        "key": "bookkeeping", "name": "LedgerLeap Bookkeeping School (DEMO)", "niche": "aspiring freelance bookkeepers",
        "offer": "Bookkeeper Launch Programme (DEMO)", "price": 1200, "audience": "career changers learning bookkeeping",
        "outcome": "their first three bookkeeping clients", "mechanism": "first-client sprint",
        "channels": "Instagram DMs, email, webinars", "faq": "Do I need a qualification? No, but you'll learn the core skills (DEMO).",
        "dms": [
            ("Hannah Brook (DEMO)", "Is this course suitable if I've never done bookkeeping before?"),
            ("Marcus Lee (DEMO)", "What does a typical week of the programme look like?"),
            ("Sofia Ruiz (DEMO)", "Can I do this alongside a full-time job?"),
        ],
        "tickets": [
            ("Ben Carter (DEMO)", "I'd like a refund please, the course isn't what I expected."),
            ("Olivia Grant (DEMO)", "If you don't fix my login today I'm contacting my lawyer."),
            ("Ella Moss (DEMO)", "Where do I find the module 2 worksheets?"),
        ],
    },
    "gym": {
        "key": "gym", "name": "IronHaus Strength Gym (DEMO)", "niche": "local strength training",
        "offer": "12-Week Strength Foundations (DEMO)", "price": 450, "audience": "busy adults new to lifting",
        "outcome": "their first confident deadlift and a routine that sticks", "mechanism": "coached small-group blocks",
        "channels": "Instagram, Meta ads, referrals", "faq": "Do I need experience? No, every block starts with technique (DEMO).",
        "dms": [
            ("Liam Fraser (DEMO)", "Hey, I've never lifted before. Is the gym intimidating?"),
            ("Ava Chen (DEMO)", "What times are the small-group sessions?"),
            ("Noah Patel (DEMO)", "Do you do a free trial?"),
        ],
        "ad_stats": [
            ("Creative A (DEMO)", 420.0, 31, 6200, 88), ("Creative B (DEMO)", 380.0, 12, 5900, 41),
            ("Creative C (DEMO)", 150.0, 0, 2100, 9),
        ],
    },
}

DEPARTMENTS = [
    ("ads", "Ads", "Cost per lead under target", [
        ("ad_copywriter", "Ad copywriter", "3 ad variants per brief, one CTA each", "Weekly, or when a creative test ends",
         "Hook click-through rate ≥ 1%"),
        ("ad_performance_analyst", "Ad performance analyst", "A weekly read of every ad against the kill and winner rules",
         "Every Monday 9am", "Wasted spend caught within 72 hours"),
    ]),
    ("webinars", "Webinars & VSL", "Webinar show rate ≥ 35%", [
        ("webinar_scriptwriter", "Webinar scriptwriter", "A 7-segment webinar outline with timings", "Before each webinar",
         "Registrant show rate ≥ 35%"),
        ("vsl_writer", "VSL writer", "A short VSL script: hook, mistake, mechanism, offer, one CTA", "When the offer changes",
         "VSL to booked-call rate"),
    ]),
    ("sales", "Sales follow-up & DM setter", "Reply → booked call rate", [
        ("dm_setter", "DM setter", "Replies to inbound DMs that start a real conversation", "Every new inbound DM",
         "Reply rate ≥ 10%, booked calls per week"),
        ("followup_agent", "Follow-up agent", "Value-only follow-ups for quiet leads", "2, 4 and 7 days after silence",
         "Re-engaged leads per week"),
    ]),
    ("content", "Content", "Inbound DMs from content", [
        ("content_planner", "Content planner", "A 7-day content plan tied to the offer", "Every Friday",
         "Posts published on schedule"),
        ("shortform_scriptwriter", "Short-form scriptwriter", "30-second scripts with a strong first line", "3 per week",
         "Average watch time"),
    ]),
    ("support", "Customer support", "First response under 1 hour", [
        ("support_agent", "Support agent", "Draft answers to member questions; escalate refunds and legal threats",
         "Every new ticket", "Tickets resolved without escalation"),
        ("onboarding_concierge", "Onboarding concierge", "Welcome message and first-week checklist", "Every new member",
         "Members who finish week 1"),
    ]),
]


def business_from_profile(form: dict) -> dict:
    """'Your own business' from the short profile form."""
    name = (form.get("name") or "Your business").strip()
    return {
        "key": "own", "name": name, "niche": form.get("niche") or "your niche", "offer": form.get("offer") or "your offer",
        "price": form.get("price") or 0, "audience": form.get("audience") or "your audience",
        "outcome": form.get("outcome") or "their result", "mechanism": form.get("mechanism") or "your method",
        "channels": form.get("channels") or "Instagram DMs", "faq": form.get("faq") or "",
        "dms": [("Sample lead A (DEMO)", f"Hi! Is {name} a good fit for someone just starting out?"),
                ("Sample lead B (DEMO)", "How much does it cost?"),
                ("Sample lead C (DEMO)", f"What results do people usually get with {form.get('offer') or 'this'}?")],
    }


def _agent_inputs(b: dict, key: str) -> list[dict]:
    if key in ("dm_setter", "followup_agent"):
        return [{"from": n, "text": t} for n, t in b["dms"]]
    if key == "support_agent":
        tickets = b.get("tickets") or [("Member A (DEMO)", "I'd like a refund."), ("Member B (DEMO)", "Where are the replays?"),
                                       ("Member C (DEMO)", "How do I reset my password?")]
        return [{"from": n, "text": t} for n, t in tickets]
    if key == "ad_performance_analyst":
        stats = b.get("ad_stats") or [("Creative A (DEMO)", 300.0, 20, 5000, 60), ("Creative B (DEMO)", 260.0, 6, 4800, 30),
                                      ("Creative C (DEMO)", 90.0, 0, 1500, 5)]
        return [{"from": n, "text": f"{n}: spend ${s:.2f} (DEMO), {l} leads, {imp} impressions, {cl} clicks",
                 "spend": s, "leads": l, "impressions": imp, "clicks": cl} for n, s, l, imp, cl in stats]
    if key == "onboarding_concierge":
        return [{"from": f"New member {i} (DEMO)", "text": f"Just joined {b['offer']}!"} for i in (1, 2, 3)]
    briefs = {
        "ad_copywriter": f"Ad brief: {b['offer']} for {b['audience']}",
        "webinar_scriptwriter": f"Webinar brief: how {b['audience']} get {b['outcome']}",
        "vsl_writer": f"VSL brief: {b['offer']} through the {b['mechanism']}",
        "content_planner": f"Content brief: one week for {b['audience']}",
        "shortform_scriptwriter": f"Script brief: {b['mechanism']} in 30 seconds",
    }
    return [{"from": "Brief (DEMO)", "text": briefs.get(key, b["offer"])}]


def generate(b: dict) -> dict:
    """Deterministic: rules, no AI call."""
    t0 = time.perf_counter()
    deps = []
    for dkey, dname, dkpi, agents in DEPARTMENTS:
        out_agents = []
        for akey, aname, produces, trigger, kpi in agents:
            out_agents.append({
                "key": akey, "name": aname, "department": dkey, "job": f"{aname} for {b['name']}",
                "produces": produces, "trigger": trigger, "kpi": kpi, "guardrails": list(GUARDS),
                "instructions": _instructions(akey, b), "inputs": _agent_inputs(b, akey),
            })
        deps.append({"key": dkey, "name": dname, "kpi": dkpi, "agents": out_agents})
    return {"business": {k: v for k, v in b.items() if k not in ("dms", "tickets", "ad_stats")}, "departments": deps,
            "counts": {"departments": len(deps), "agents": sum(len(d["agents"]) for d in deps)},
            "generated_by": "rules, no AI call", "ms": round((time.perf_counter() - t0) * 1000, 2)}


def _instructions(key: str, b: dict) -> str:
    common = " Never claim guaranteed results or income. Never invent testimonials. Everything you write goes to the approval queue."
    return {
        "dm_setter": f"Reply to inbound DMs for {b['name']}. Start a real conversation about their goal. Ask one question per "
                     f"message. Never mention price in a DM; if asked, offer a short call to see if {b['offer']} fits.",
        "followup_agent": "Follow up with quiet leads after 2, 4 and 7 days. Every follow-up gives value: a tip, a short video "
                          "or a question. Never write 'just following up'.",
        "support_agent": "Draft answers to member questions using the FAQ. Refund requests and anything mentioning lawyers, "
                         "legal action or chargebacks are escalated to a person, never answered.",
        "ad_performance_analyst": "Read every ad: spend, leads, cost per lead, click-through rate. Apply the kill rule (CPL over "
                                  "2x target after 1.5x target spend) and the winner rule (lowest CPL with CTR ≥ 1%). Label "
                                  "every dollar figure DEMO in the demo workspace.",
        "ad_copywriter": f"Write 3 ad variants for {b['offer']} aimed at {b['audience']}. One hook, one CTA each.",
        "webinar_scriptwriter": "Outline the seven segments with timings: 5/7/10/15/10/12/6 minutes.",
        "vsl_writer": "Hook, the big mistake, the mechanism, the offer, one CTA.",
        "content_planner": "Plan seven days of posts, each tied to one pain or one step of the mechanism.",
        "shortform_scriptwriter": "30-second scripts. First line under 12 words. One CTA.",
        "onboarding_concierge": "Welcome each new member by name and send the first-week checklist.",
    }.get(key, "") + common


# --------------------------------------------------------------------------- storage
def save_build(tenant, preset: str, b: dict, gen: dict) -> int:
    with tenant.db() as c:
        return c.execute("INSERT INTO biz_builds(preset,business,departments,status,created) VALUES(?,?,?,?,?)",
                         (preset, db.jdump(b), db.jdump(gen), "active", db.now())).lastrowid


def get_build(tenant, build_id: int) -> dict:
    with tenant.db() as c:
        r = db.row(c.execute("SELECT * FROM biz_builds WHERE id=?", (build_id,)).fetchone())
    if r is None:
        raise KeyError(build_id)
    r["business"] = db.jload(r["business"], {})
    r["departments"] = db.jload(r["departments"], {})
    return r


def _find_agent(build: dict, key: str) -> dict:
    for d in build["departments"]["departments"]:
        for a in d["agents"]:
            if a["key"] == key:
                return a
    raise KeyError(key)


# --------------------------------------------------------------------------- rules drafts
ESCALATE = re.compile(r"\b(refund|money back|chargeback|lawyer|solicitor|legal|sue|court|attorney)\b", re.I)


def rules_draft(b: dict, agent_key: str, item: dict) -> dict:
    first = item["from"].split()[0]
    text = item["text"]
    if agent_key == "dm_setter":
        if re.search(r"\b(how much|price|cost|expensive)\b", text, re.I):
            body = (f"Hi {first}, thanks for asking! It depends on where your business is right now. "
                    f"What's the main thing you'd want {b['offer'].replace(' (DEMO)', '')} to fix first?")
        elif "?" in text:
            body = (f"Hi {first}, great question. Lots of {b['audience']} ask that. "
                    f"What made you start looking into this now?")
        else:
            body = f"Hi {first}, thanks for reaching out! What's your main goal for the next 90 days?"
        return {"body": body, "kind": "dm", "asset": "dm_script", "meta": {"prospect_facing": True, "lead_tier": "cold",
                                                                            "prior_contact": True}}
    if agent_key == "followup_agent":
        return {"body": f"Hi {first}, here's a quick tip that helps {b['audience']}: start with the {b['mechanism']}. "
                        f"Want me to send the 2-minute video?", "kind": "followup", "asset": "followup",
                "meta": {"prospect_facing": True, "lead_tier": "cold", "prior_contact": True}}
    if agent_key == "support_agent":
        if ESCALATE.search(text):
            return {"escalate": True, "body": f"Escalated to a person: {text}", "kind": "support_escalation",
                    "asset": "generic", "meta": {"escalated": True}}
        return {"body": f"Hi {first}, thanks for getting in touch. {b.get('faq') or 'Here is where to find that in the member area.'} "
                        f"Reply here if anything else comes up.", "kind": "support_reply", "asset": "generic", "meta": {}}
    if agent_key == "ad_performance_analyst":
        spend, leads = item["spend"], item["leads"]
        cpl = f"${spend / leads:.2f} (DEMO)" if leads else "no leads (DEMO)"
        verdict = "kill rule: pause" if (leads == 0 and spend >= 15) or (leads and spend / leads > 20) else "keep running"
        ctr = item["clicks"] / item["impressions"] if item["impressions"] else 0
        body = (f"{item['from']}: spend ${spend:.2f} (DEMO) · cost per lead {cpl} · CTR {ctr:.1%} · {verdict}. "
                f"Recommendation goes to the owner for approval.")
        return {"body": body, "kind": "ad_report", "asset": "generic", "meta": {}}
    if agent_key == "onboarding_concierge":
        return {"body": f"Welcome, {first}! Here's your first-week checklist: 1) watch module 1, 2) post your intro, "
                        f"3) book your kickoff call. Reply if you get stuck.", "kind": "welcome", "asset": "generic", "meta": {}}
    if agent_key == "ad_copywriter":
        return {"body": f"Stop guessing what works for {b['audience']}.\n{b['offer'].replace(' (DEMO)', '')} helps you get "
                        f"{b['outcome']} through the {b['mechanism']}. Book a call.", "kind": "ad_copy", "asset": "ad",
                "meta": {"client_asset": True}}
    if agent_key == "webinar_scriptwriter":
        segs = "\n".join(f"Segment {i + 1}: {n} ({m} min)" for i, (n, m) in enumerate(
            [("Hook and promise", 5), ("Origin and credibility", 7), ("The big mistake", 10), ("The new mechanism", 15),
             ("Proof framework", 10), ("The offer", 12), ("Questions and close", 6)]))
        return {"body": f"How {b['audience']} get {b['outcome']}.\n{segs}", "kind": "webinar_outline", "asset": "webinar",
                "meta": {"client_asset": True}}
    if agent_key == "vsl_writer":
        return {"body": f"Most {b['audience']} make one mistake.\nThe {b['mechanism']} fixes it.\n"
                        f"{b['offer'].replace(' (DEMO)', '')}: what's included and who it's for.\nBook a call.",
                "kind": "vsl_script", "asset": "vsl", "meta": {"client_asset": True}}
    if agent_key == "content_planner":
        days = ["Mon: the big mistake", "Tue: one quick tip", "Wed: behind the scenes", "Thu: the mechanism, step 1",
                "Fri: a member question answered", "Sat: myth vs fact", "Sun: weekly recap"]
        return {"body": "\n".join(days), "kind": "content_plan", "asset": "generic", "meta": {}}
    if agent_key == "shortform_scriptwriter":
        return {"body": f"The {b['mechanism']} in 30 seconds.\nStep one: pick one thing to fix this week.\nFollow for step two.",
                "kind": "script", "asset": "script", "meta": {}}
    return {"body": item["text"], "kind": "draft", "asset": "generic", "meta": {}}


# --------------------------------------------------------------------------- AI pass
def _cap_key() -> str:
    return "builder_calls:" + time.strftime("%Y-%m-%d")


def ai_reason(tenant) -> str | None:
    cfg = config.get()
    if not cfg.builder_ai:
        return "builder AI off (OPERATOR_BUILDER_AI=0)"
    r = llm.unavailable_reason(tenant)
    if r:
        return r
    if int(tenant.get(_cap_key(), 0) or 0) >= cfg.builder_daily_cap:
        return f"daily cap of {cfg.builder_daily_cap} builder calls reached"
    return None


def ai_draft(tenant, b: dict, agent: dict, item: dict, rules_body: str, deadline: float) -> tuple[str | None, str | None]:
    """Returns (ai_text, reason_if_not_used)."""
    reason = ai_reason(tenant)
    if reason:
        return None, reason
    left = deadline - time.time()
    if left <= 1:
        return None, "AI pass skipped: hard deadline reached"
    tenant.set(_cap_key(), int(tenant.get(_cap_key(), 0) or 0) + 1)
    try:
        out = llm.complete(tenant, agent["instructions"],
                           f"Business: {b['name']}\nInput from {item['from']}: {item['text']}\nRules draft: {rules_body}\n"
                           "Improve the rules draft. Same rules. Return only the message.",
                           purpose=f"builder:{agent['key']}", timeout=min(BUILDER_DEADLINE_S, left), max_tokens=250)
    except llm.AIUnavailable as e:
        return None, e.reason
    if time.time() > deadline:
        return None, "AI pass too slow (25 s deadline)"
    return out["text"].strip(), None


# --------------------------------------------------------------------------- run
PLAN = ["Load input", "Rules draft", "AI pass", "Review (rubric + guardrails)", "Queue"]


def run_agent(tenant, build_id: int, agent_key: str, user: dict | None = None, background: bool = True) -> int | dict:
    build = get_build(tenant, build_id)
    agent = _find_agent(build, agent_key)
    b = {**build["business"], **({k: v for k, v in (PRESETS.get(build["preset"]) or {}).items()
                                  if k in ("dms", "tickets", "ad_stats")})}
    actor = f"{user['username']}({user['role']})" if user else "system"
    demo = tenant.is_demo

    def fn(run: runtime.Run):
        deadline = time.time() + BUILDER_DEADLINE_S
        inputs = run.step("load_input", {"agent": agent_key}, lambda: agent["inputs"], "Load input")
        drafts = run.step("rules_draft", {"n": len(inputs)}, lambda: [rules_draft(b, agent_key, it) for it in inputs],
                          "Rules draft")

        def _ai():
            out = []
            for it, d in zip(inputs, drafts):
                if d.get("escalate"):
                    out.append({"text": None, "reason": "escalated to a person (no AI draft)"})
                    continue
                text, reason = ai_draft(tenant, b, agent, it, d["body"], deadline)
                out.append({"text": text, "reason": reason})
            return out

        ai = run.step("ai_pass", {"enabled": ai_reason(tenant) is None}, _ai, "AI pass")
        if any(a["text"] for a in ai):
            run.use_ai("live")
        names = guardrails.tenant_banned_names(tenant)

        def _review():
            out = []
            for it, d, a in zip(inputs, drafts, ai):
                meta = rubrics.meta_for(d["asset"], d["meta"])
                if d.get("escalate"):
                    out.append({"input": it, "body": d["body"], "source": "Rules", "escalated": True, "score": None,
                                "passes": True, "label": None, "flags": []})
                    continue
                chosen, source, label = d["body"], "Rules", None
                if a["text"]:
                    aflags = guardrails.check(a["text"], guardrails.mode_for(meta), meta, names)
                    if guardrails.blocked(aflags):
                        label = "Rules draft (AI not used): AI draft tripped a guardrail"
                    else:
                        chosen, source = a["text"], "AI"
                else:
                    label = f"Rules draft (AI not used): {a['reason']}"
                sc = rubrics.score(chosen, d["asset"], meta, names, tenant=tenant)
                passes = not guardrails.blocked(sc["flags"]) and (sc["score"] >= 60 or d["asset"] == "generic")
                out.append({"input": it, "body": chosen, "source": source, "score": sc["score"], "passes": passes,
                            "label": label, "flags": sc["flags"], "kind": d["kind"], "meta": meta})
            return out

        reviewed = run.step("review", {}, _review, "Review (rubric + guardrails)")

        def _queue():
            cards = []
            for r in reviewed:
                title = f"Escalation: {r['input']['from']}" if r.get("escalated") else f"Reply to {r['input']['from']}"
                if agent_key in ("ad_performance_analyst", "content_planner", "webinar_scriptwriter", "vsl_writer",
                                 "ad_copywriter", "shortform_scriptwriter"):
                    title = f"{agent['name']}: {r['input']['from']}"
                item = queue.add(tenant, "support_escalation" if r.get("escalated") else (r.get("kind") or "draft"), r["body"],
                                 channel="manual" if r.get("escalated") else "instagram_dm" if agent_key in ("dm_setter", "followup_agent") else "manual",
                                 recipient_ref=r["input"]["from"], title=title,
                                 meta={**(r.get("meta") or {}), "build_id": build_id, "agent": agent_key,
                                       "tag": f"builder:{build_id}", "source": r["source"], "escalated": bool(r.get("escalated"))},
                                 run_id=run.id, actor=actor, demo=demo)
                if r.get("escalated") and item["status"] == "pending":
                    with tenant.db() as c:
                        c.execute("UPDATE queue SET status='needs_human' WHERE id=?", (item["id"],))
                    item["status"] = "needs_human"
                cards.append({"from": r["input"]["from"], "input": r["input"]["text"], "draft": r["body"], "source": r["source"],
                              "score": r["score"], "passes": r["passes"], "label": r["label"], "queue_id": item["id"],
                              "status": item["status"], "escalated": bool(r.get("escalated"))})
            return cards

        cards = run.step("queue", {}, _queue, "Queue")
        summary = {"drafts": len(cards), "pass_review": sum(1 for c in cards if c["passes"]), "sent": 0,
                   "escalated": sum(1 for c in cards if c["escalated"])}
        with tenant.db() as c:
            c.execute("INSERT INTO biz_agent_runs(build_id,agent_key,run_id,summary,created) VALUES(?,?,?,?,?)",
                      (build_id, agent_key, run.id, db.jdump(summary), db.now()))
        return {"build_id": build_id, "agent": agent_key, "summary": summary,
                "headline": f"{summary['drafts']} drafts · {summary['pass_review']} pass review · Sent: 0",
                "cards": cards, "queue_ids": [c["queue_id"] for c in cards]}

    title = f"Builder: {agent['name']} for {b['name']}"
    if background:
        return runtime.launch(tenant, "builder", title, PLAN, fn, actor)
    out = runtime.execute(tenant, "builder", title, PLAN, fn, actor)
    return {**out["result"], "run_id": out["run_id"]}


def start_over(tenant, build_id: int, actor: str = "system") -> dict:
    """Archive the build and clear its undecided DEMO drafts (demo DB only)."""
    from . import audit, tenancy

    with tenant.db() as c:
        c.execute("UPDATE biz_builds SET status='archived' WHERE id=?", (build_id,))
    cleared = 0
    if tenancy.is_demo_db(tenant):
        with tenant.db() as c:
            cleared = c.execute("DELETE FROM queue WHERE json_extract(meta,'$.build_id')=? AND status IN "
                                "('pending','held','needs_human') AND demo=1", (build_id,)).rowcount
    else:
        for it in queue.list_items(tenant, status="open", tag=f"builder:{build_id}"):
            queue.reject(tenant, it["id"], actor, reason="builder start over")
            cleared += 1
    audit.log(tenant, actor, "builder", {"start_over": build_id, "cleared": cleared})
    return {"archived": build_id, "cleared_drafts": cleared}


def reset_all(tenant) -> dict:
    """scripts/reset_builder_demo.py: archive every build and clear undecided builder drafts (demo only)."""
    from . import tenancy

    if not tenancy.is_demo_db(tenant):
        raise PermissionError("Builder resets only run against the demo database.")
    with tenant.db() as c:
        ids = [r["id"] for r in c.execute("SELECT id FROM biz_builds WHERE status='active'")]
    n = 0
    for i in ids:
        n += start_over(tenant, i)["cleared_drafts"]
    return {"archived": len(ids), "cleared_drafts": n}
