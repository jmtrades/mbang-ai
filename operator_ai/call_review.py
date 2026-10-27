"""Consent-first sales-call review. No processing without a consent record from every party.
Stage scores with citations, the biggest leak, rep trends, and objection drills with spaced
repetition (SM-2)."""
from __future__ import annotations

import re
import time

from . import audit, db, retrieval, runtime

STAGES = [
    ("rapport", "Rapport", ["thanks for", "how are", "saw your", "great to", "nice to"], "call has five stages rapport"),
    ("discovery", "Discovery", ["goal", "current", "tried", "why now", "how many", "what's working"],
     "discovery is the longest stage goal current numbers tried why now"),
    ("gap", "Gap", ["gap", "difference", "where you want", "cost of staying", "if nothing changes"],
     "the gap play back the difference"),
    ("offer", "Offer", ["programme", "program", "what's included", "here's how we", "the offer"],
     "only when the gap is clear present the offer"),
    ("next_step", "Next step", ["next step", "book", "start date", "send over", "onboarding"],
     "call stages next step"),
]
OBJECTIONS = [
    ("think", r"\b(think about it|think it over|sleep on it)\b", "I need to think about it",
     "need to think about it ask what specifically"),
    ("price", r"\b(too expensive|can'?t afford|out of (my|our) budget|costs? too much)\b", "It's too expensive",
     "too expensive go back to the gap cost of staying"),
    ("partner", r"\b(ask my (partner|wife|husband|spouse)|talk to my (partner|wife|husband|spouse))\b",
     "I need to ask my partner", "ask my partner follow-up call with both"),
]


class ConsentMissing(Exception):
    pass


def create_call(tenant, title: str, rep: str, parties: list[str], transcript: str) -> int:
    with tenant.db() as c:
        return c.execute("INSERT INTO calls(title,rep,transcript,parties,status,created,demo) VALUES(?,?,?,?,?,?,?)",
                         (title, rep, transcript, db.jdump(parties), "awaiting_consent", db.now(),
                          1 if tenant.is_demo else 0)).lastrowid


def record_consent(tenant, call_id: int, party: str, method: str, actor: str) -> dict:
    with tenant.db() as c:
        call = c.execute("SELECT parties FROM calls WHERE id=?", (call_id,)).fetchone()
        if call is None:
            raise KeyError(call_id)
        if party not in db.jload(call["parties"], []):
            raise ValueError("not a party on this call")
        c.execute("INSERT INTO call_consents(call_id,party,method,at) VALUES(?,?,?,?)", (call_id, party, method[:80], db.now()))
    audit.log(tenant, actor, "consent", {"call": call_id, "party": party, "method": method[:80]})
    return consent_status(tenant, call_id)


def consent_status(tenant, call_id: int) -> dict:
    with tenant.db() as c:
        call = c.execute("SELECT parties FROM calls WHERE id=?", (call_id,)).fetchone()
        got = {r["party"] for r in c.execute("SELECT party FROM call_consents WHERE call_id=?", (call_id,))}
    if call is None:
        raise KeyError(call_id)
    parties = db.jload(call["parties"], [])
    missing = [p for p in parties if p not in got]
    return {"parties": parties, "consented": sorted(got), "missing": missing, "complete": bool(parties) and not missing}


def review(tenant, call_id: int, user: dict | None = None) -> dict:
    cs = consent_status(tenant, call_id)
    if not cs["complete"]:
        raise ConsentMissing(f"No processing without consent from every party. Missing: {', '.join(cs['missing'])}")
    with tenant.db() as c:
        call = db.row(c.execute("SELECT * FROM calls WHERE id=?", (call_id,)).fetchone())
    text = call["transcript"] or ""
    low = text.lower()
    actor = f"{user['username']}({user['role']})" if user else "system"

    def fn(run: runtime.Run):
        run.note("consent", {}, cs, label="Check consent (all parties)")

        def _stages():
            out = []
            for i, (key, label, cues, q) in enumerate(STAGES):
                hits = sum(1 for cue in cues if cue in low)
                score = min(10, hits * 4)
                h = retrieval.search(tenant, q, k=1)
                out.append({"key": key, "label": label, "score": score, "evidence": hits,
                            "citation": retrieval.cite({**h[0], "id": f"S{i + 1}"}) if h else None})
            return out

        stages = run.step("stages", {}, _stages, "Score each stage (cited)")
        leak = min(stages, key=lambda s: s["score"])

        def _objections():
            found = []
            for key, pat, label, q in OBJECTIONS:
                if re.search(pat, text, re.I):
                    handled = {"think": "what specifically", "price": "gap", "partner": "both"}[key] in low
                    found.append({"key": key, "objection": label, "handled": handled, "query": q})
            return found

        objs = run.step("objections", {}, _objections, "Find objections")
        discount = bool(re.search(r"\b(discount|knock (some|it) off|special price)\b", text, re.I))
        drills = run.step("drills", {}, lambda: [make_drill(tenant, o["objection"], o["query"], f"call:{call_id}")
                                                  for o in objs], "Create objection drills")
        result = {"call_id": call_id, "stages": stages, "overall": round(sum(s["score"] for s in stages) / len(stages) * 10),
                  "biggest_leak": leak, "objections": objs, "discounted": discount, "drills": drills}
        with tenant.db() as c:
            c.execute("UPDATE calls SET status='reviewed', review=? WHERE id=?", (db.jdump(result), call_id))
        return result

    out = runtime.execute(tenant, "call_review", f"Call review: {call['title']}", ["Check consent (all parties)",
                                                                                  "Score each stage (cited)", "Find objections",
                                                                                  "Create objection drills"], fn, actor)
    return {**out["result"], "run_id": out["run_id"]}


def rep_trends(tenant) -> list[dict]:
    with tenant.db() as c:
        rows = c.execute("SELECT rep, review, created FROM calls WHERE status='reviewed' ORDER BY created").fetchall()
    by: dict[str, list] = {}
    for r in rows:
        rv = db.jload(r["review"], {})
        by.setdefault(r["rep"], []).append({"at": r["created"], "overall": rv.get("overall", 0),
                                            "leak": (rv.get("biggest_leak") or {}).get("label")})
    out = []
    for rep, items in by.items():
        first, last = items[0]["overall"], items[-1]["overall"]
        out.append({"rep": rep, "calls": len(items), "latest": last, "trend": "up" if last > first else "down" if last < first else "flat",
                    "common_leak": max({i["leak"] for i in items}, key=lambda l: sum(1 for i in items if i["leak"] == l))})
    return out


# --------------------------------------------------------------------------- objection drills (SM-2)
def make_drill(tenant, objection: str, query: str, source: str) -> dict:
    with tenant.db() as c:
        r = c.execute("SELECT id FROM drills WHERE objection=?", (objection,)).fetchone()
        if r:
            return {"id": r["id"], "objection": objection, "existing": True}
    found = _academy_answer(tenant, objection, query)
    if found is not None:
        answer, cite = found["answer"], found["citation"]
    else:
        hits = retrieval.search(tenant, query, k=3)
        answer, cite = _extractive_answer(tenant, objection, hits)
    with tenant.db() as c:
        did = c.execute("INSERT INTO drills(objection,answer,citation,source,created) VALUES(?,?,?,?,?)",
                        (objection, answer, db.jdump(cite), source, db.now())).lastrowid
    return {"id": did, "objection": objection, "answer": answer, "citation": cite}


def _academy_answer(tenant, objection: str, query: str) -> dict | None:
    """The course's own 'When someone says X, Y' answer with its exact line (Academy extraction)."""
    try:
        from .academy import build as academy_build
    except ImportError:
        return None
    try:
        return academy_build.objection_answer(tenant, objection, query)
    except Exception:  # noqa: BLE001 - fall back to retrieval
        return None


def _extractive_answer(tenant, objection: str, hits: list[dict]) -> tuple[str, dict | None]:
    """Fallback: the best two sentences from the top passages, cited to the exact line the first one is on."""
    if not hits:
        return "Not covered by the course material.", None
    from . import index
    from .agents import coach

    text = coach.extractive(objection, [{**h, "id": f"S{i + 1}"} for i, h in enumerate(hits)], max_sentences=2)
    first_id = re.search(r"\[S(\d+)\]", text)
    h = hits[int(first_id.group(1)) - 1] if first_id else hits[0]
    sentences = [re.sub(r"\s*\[S\d+\]\s*$", "", s.lstrip("• ").strip()) for s in text.split("\n") if s.strip()]
    answer = " ".join(sentences) or h["snippet"]
    cite = retrieval.cite({**h, "id": "S1"})
    if sentences:
        doc = index.for_tenant(tenant).docs.get(h["file"])
        a, _, b = h["lines"].partition("-")
        for n in range(int(a), int(b or a) + 1):
            if doc is not None and n <= len(doc.lines) and sentences[0][:60] in doc.lines[n - 1]:
                cite["lines"] = f"{n}-{n}"
                cite["snippet"] = sentences[0][:300]
                break
    return answer, cite


def seed_drills(tenant) -> int:
    n = 0
    for _, _, label, q in OBJECTIONS:
        if not make_drill(tenant, label, q, "library").get("existing"):
            n += 1
    return n


def due_cards(tenant, user_id: int) -> list[dict]:
    """Objection drill cards. With the Academy installed they live in its one spaced-repetition store and the
    bank is seeded at Academy build time (or by a call review), never on read."""
    try:
        from .academy import srs as academy_srs
    except ImportError:
        academy_srs = None
    if academy_srs is not None:
        return academy_srs.drill_cards(tenant, user_id)
    seed_drills(tenant)
    now = time.time()
    with tenant.db() as c:
        drills = db.rows(c.execute("SELECT * FROM drills ORDER BY id"))
        cards = {r["drill_id"]: dict(r) for r in c.execute("SELECT * FROM drill_cards WHERE user_id=?", (user_id,))}
        for d in drills:
            if d["id"] not in cards:
                cid = c.execute("INSERT INTO drill_cards(drill_id,user_id,due) VALUES(?,?,?)", (d["id"], user_id, now)).lastrowid
                cards[d["id"]] = {"id": cid, "drill_id": d["id"], "ease": 2.5, "interval": 0, "reps": 0, "due": now}
    out = []
    for d in drills:
        card = cards[d["id"]]
        out.append({"card_id": card["id"], "drill_id": d["id"], "objection": d["objection"], "answer": d["answer"],
                    "citation": db.jload(d["citation"], None), "due": card["due"], "is_due": card["due"] <= now,
                    "interval_days": card["interval"], "reps": card["reps"]})
    return sorted(out, key=lambda x: x["due"])


def sm2(ease: float, interval: float, reps: int, grade: int) -> tuple[float, float, int]:
    """SuperMemo-2: grade 0-5. Returns (ease, interval_days, reps)."""
    grade = max(0, min(5, int(grade)))
    if grade < 3:
        return max(1.3, ease - 0.2), 1.0, 0
    reps += 1
    interval = 1.0 if reps == 1 else 6.0 if reps == 2 else round(interval * ease, 1)
    ease = max(1.3, ease + 0.1 - (5 - grade) * (0.08 + (5 - grade) * 0.02))
    return ease, interval, reps


def grade(tenant, user_id: int, card_id: int, g: int) -> dict:
    try:
        from .academy import srs as academy_srs
    except ImportError:
        academy_srs = None
    if academy_srs is not None:
        return academy_srs.grade_drill(tenant, user_id, card_id, g)
    with tenant.db() as c:
        card = c.execute("SELECT * FROM drill_cards WHERE id=? AND user_id=?", (card_id, user_id)).fetchone()
        if card is None:
            raise KeyError(card_id)
        ease, interval, reps = sm2(card["ease"], card["interval"], card["reps"], g)
        due = time.time() + interval * 86400
        c.execute("UPDATE drill_cards SET ease=?, interval=?, reps=?, due=?, last_grade=? WHERE id=?",
                  (ease, interval, reps, due, g, card_id))
    return {"card_id": card_id, "ease": round(ease, 2), "interval_days": interval, "reps": reps, "due": due}
