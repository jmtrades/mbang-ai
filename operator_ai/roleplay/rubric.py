"""Role-play rubric rules as data (roleplay_rubrics), tied to the SOP line each rule comes from.

The defaults are seeded from the values the old keyword rubric (operator_ai/rubrics.py) used, each marked
"default: confirm against your SOP". The owner edits them; a validator flags any rule whose cited file is
missing from the knowledge base, whose cited lines no longer exist, or whose cited lines no longer contain
the rule's parameter (for example '60' for a 60-word DM limit). Rules can also cite the research playbooks
shipped with the product ("playbook:<domain>/<entry>", lines "steps:2", "do:1" or "guidance").
"""
from __future__ import annotations

import json
import math
import re
from functools import lru_cache

from .. import db

DEFAULT_NOTE = "default: confirm against your SOP"
P_CALL = "course/transcripts/vid_p7q8r9.txt"
P_DM = "course/transcripts/vid_d4e5f6.txt"
PB_SALES = "playbook:sales-setting-closing"
PB_TRAIN = "playbook:training-science"

# rule_key: (label, weight, params, cite_file, cite_lines, must_contain, kind)
DEFAULTS: dict[str, tuple] = {
    "discovery": ("Discovery: goal, current numbers, what they tried, why now", 30, {},
                  P_CALL, "4", ["goal", "current numbers", "tried", "why now"], "criterion"),
    "decision_check": ("Decision check: who decides and how they'd fund it", 10, {},
                       f"{PB_SALES}/setter-qualification-criteria", "steps:6", ["fund", "never suggest opening new credit"],
                       "criterion"),
    "gap": ("Gap played back with facts they gave you", 15, {}, P_CALL, "5", ["gap", "play back"], "criterion"),
    "offer_order": ("Offer only after the gap is clear", 10, {}, P_CALL, "6", ["gap is clear", "offer"], "criterion"),
    "objection_think": ("\"Think about it\": ask what specifically", 15, {"retry_credit": 0.5},
                        P_CALL, "8", ["what specifically"], "objection"),
    "objection_price": ("\"Too expensive\": back to the gap and the cost of staying", 15, {"retry_credit": 0.5},
                        P_CALL, "10", ["gap", "cost of staying"], "objection"),
    "objection_partner": ("\"Ask my partner\": a follow-up call with both", 15, {"retry_credit": 0.5},
                          P_CALL, "13", ["both"], "objection"),
    "objection_agency": ("\"We have an agency\": clarify what is and isn't working", 15, {"retry_credit": 0.5},
                         f"{PB_SALES}/objection-handling-pause-question-isolate", "steps:2", ["clarify"], "objection"),
    "objection_send_info": ("\"Send me info\": ask what would be useful", 15, {"retry_credit": 0.5},
                            f"{PB_SALES}/objection-handling-pause-question-isolate", "steps:2", ["clarify"], "objection"),
    "objection_price_ask": ("Price asked in a DM: keep it for the call", 15, {"retry_credit": 0.5},
                            P_DM, "4", ["no price in dms"], "objection"),
    "objection_guarantee": ("Guarantee request: answer honestly, promise nothing", 15, {"retry_credit": 0.5},
                            f"{PB_SALES}/earnings-claims-and-testimonials-on-calls", "steps:2", ["guaranteed"], "objection"),
    "objection_income": ("Income question: no earnings claims", 15, {"retry_credit": 0.5},
                         f"{PB_SALES}/earnings-claims-and-testimonials-on-calls", "do:1", ["not income"], "objection"),
    "objection_time": ("Time question: a realistic commitment", 10, {"retry_credit": 0.5},
                       f"{PB_SALES}/closing-call-structure-discovery-gap-offer", "steps:7", ["realistic time commitment"],
                       "objection"),
    "objection_plant": ("Planted questions: refuse and name the rule", 20, {"retry_credit": 0.5},
                        "sops/mastering_webinars.txt", "17", ["never plant questions"], "objection"),
    "outcome": ("Outcome: the right next step", 15, {}, P_CALL, "2", ["next step"], "criterion"),
    "talk_ratio": ("They talk at least 40% of the time", 5, {"max_share": 0.6, "min_turns": 3},
                   f"{PB_SALES}/closing-call-structure-discovery-gap-offer", "do:1", ["40%"], "criterion"),
    "trust": ("Rapport and control (trust built)", 10, {"full_gain": 20},
              f"{PB_TRAIN}/roleplay-scorecard-design", "steps:2", ["rapport and control"], "criterion"),
    "dm_personal": ("Opener mentions something specific they posted", 10, {}, P_DM, "7", ["something specific"], "criterion"),
    "dm_one_question": ("One question per message", 10, {"max_questions": 1}, P_DM, "3", ["one question per message"],
                        "criterion"),
    "dm_length": ("Short DMs", 10, {"max_words": 60}, P_DM, "2", ["60"], "criterion"),
    "commit": ("Agree one focus behaviour", 15, {}, f"{PB_TRAIN}/coaching-cadence-and-span", "steps:2",
               ["agree one focus behaviour"], "criterion"),
    "no_discount": ("No discounting to close (penalty)", 10, {"per_occurrence": 10, "cap": 20}, P_CALL, "11",
                    ["never discount"], "penalty"),
    "monologue": ("No monologues (penalty)", 3, {"max_words": 120, "per_occurrence": 3},
                  f"{PB_SALES}/closing-call-structure-discovery-gap-offer", "guidance", ["real dialogue", "120"], "penalty"),
    "compliance": ("Compliance gate: no guarantees, income claims, fake proof, pressure or debt", 0, {},
                   "sops/mastering_webinars.txt", "10", ["verified results"], "gate"),
}
PLAYBOOK_DIR = None  # resolved lazily

# The parameters each rule really uses, with their type and allowed range. Anything else is refused (an editable
# number that changes nothing would mislead the owner), and a bad value is refused instead of breaking scoring.
_RETRY = {"retry_credit": (float, 0.0, 1.0)}
PARAM_SPEC: dict[str, dict[str, tuple]] = {
    **{k: _RETRY for k, v in DEFAULTS.items() if v[6] == "objection"},
    "talk_ratio": {"max_share": (float, 0.2, 0.95), "min_turns": (int, 1, 30)},
    "trust": {"full_gain": (float, 1.0, 100.0)},
    "dm_one_question": {"max_questions": (int, 1, 5)},
    "dm_length": {"max_words": (int, 10, 300)},
    "no_discount": {"per_occurrence": (float, 0.0, 50.0), "cap": (float, 0.0, 100.0)},
    "monologue": {"max_words": (int, 30, 600), "per_occurrence": (float, 0.0, 30.0)},
}
_NUM_WORDS = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five"}


class RuleError(ValueError):
    """A rule edit the owner needs to fix (422)."""


def clean_params(rule_key: str, params) -> dict:
    """Check an owner's parameters against the rule's spec; returns typed values or raises RuleError."""
    if not isinstance(params, dict):
        raise RuleError("Parameters must be an object, for example {\"max_words\": 60}")
    spec = PARAM_SPEC.get(rule_key, {})
    out = {}
    for k, v in params.items():
        if k not in spec:
            allowed = ", ".join(sorted(spec)) or "none"
            raise RuleError(f"'{k}' isn't a setting this rule uses (it uses: {allowed})")
        typ, lo, hi = spec[k]
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
            raise RuleError(f"'{k}' must be a number")
        if not lo <= v <= hi:
            raise RuleError(f"'{k}' must be between {lo:g} and {hi:g}")
        if typ is int and float(v) != int(v):
            raise RuleError(f"'{k}' must be a whole number")
        out[k] = typ(v)
    return out


def param_terms(rule_key: str, params: dict) -> list[list[str]]:
    """The parameter values the cited SOP lines must state (each inner list: any one of these spellings)."""
    out = []
    if rule_key in ("dm_length", "monologue") and "max_words" in params:
        out.append([str(int(params["max_words"]))])
    if rule_key == "dm_one_question" and "max_questions" in params:
        n = int(params["max_questions"])
        out.append([str(n)] + ([_NUM_WORDS[n]] if n in _NUM_WORDS else []))
    if rule_key == "talk_ratio" and "max_share" in params:
        share = float(params["max_share"])
        out.append([f"{round(share * 100)}%", f"{round((1 - share) * 100)}%"])
    return out


def _now() -> float:
    return db.now()


def ensure(tenant) -> None:
    with tenant.db() as c:
        have = {r["rule_key"] for r in c.execute("SELECT rule_key FROM roleplay_rubrics")}
        for k, (label, w, params, f, lines, must, kind) in DEFAULTS.items():
            if k in have:
                continue
            c.execute("INSERT OR IGNORE INTO roleplay_rubrics(rule_key,label,weight,params,cite_file,cite_lines,must_contain,kind,"
                      "status,note,updated_by,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                      (k, label, w, db.jdump(params), f, lines, db.jdump(must), kind, "default", DEFAULT_NOTE, "system", _now()))


def _row(r) -> dict:
    d = dict(r)
    raw = db.jload(d.get("params"), {}) or {}
    spec = PARAM_SPEC.get(d["rule_key"], {})
    d["params"] = {k: v for k, v in raw.items() if k in spec} if isinstance(raw, dict) else {}
    d["must_contain"] = db.jload(d.get("must_contain"), []) or []
    d["confirmed"] = d.get("status") == "confirmed"
    return d


def rules(tenant) -> dict[str, dict]:
    ensure(tenant)
    with tenant.db() as c:
        return {r["rule_key"]: _row(r) for r in c.execute("SELECT * FROM roleplay_rubrics ORDER BY id")}


def update(tenant, rule_key: str, patch: dict, actor: str) -> dict:
    from .. import audit

    ensure(tenant)
    with tenant.db() as c:
        r = c.execute("SELECT * FROM roleplay_rubrics WHERE rule_key=?", (rule_key,)).fetchone()
        if r is None:
            raise KeyError(rule_key)
        cur = _row(r)
        if patch.get("params") is not None:
            patch = {**patch, "params": clean_params(rule_key, patch["params"])}
        for k in ("label", "weight", "params", "cite_file", "cite_lines", "must_contain"):
            if k in patch and patch[k] is not None:
                cur[k] = patch[k]
        changed = any(k in patch and patch[k] is not None for k in
                      ("label", "weight", "params", "cite_file", "cite_lines", "must_contain"))
        if patch.get("confirmed") is True:
            status = "confirmed"
        elif changed or patch.get("confirmed") is False:
            status = "edited"
        else:
            status = cur["status"]
        note = "confirmed by the owner" if status == "confirmed" else ("edited: confirm against your SOP" if status == "edited"
                                                                       else DEFAULT_NOTE)
        c.execute("UPDATE roleplay_rubrics SET label=?, weight=?, params=?, cite_file=?, cite_lines=?, must_contain=?, status=?, "
                  "note=?, updated_by=?, updated=? WHERE rule_key=?",
                  (cur["label"], float(cur["weight"]), db.jdump(cur["params"]), cur["cite_file"], cur["cite_lines"],
                   db.jdump(cur["must_contain"]), status, note, actor, _now(), rule_key))
    audit.log(tenant, actor, "roleplay.rubric", {"rule": rule_key, "status": status,
                                                 "fields": sorted(k for k in patch if patch[k] is not None)})
    return rules(tenant)[rule_key]


# --------------------------------------------------------------------------- citations
@lru_cache(maxsize=16)
def _playbook(domain: str) -> dict | None:
    from ..config import ROOT

    if not re.match(r"^[a-z0-9-]{2,60}$", domain):
        return None
    p = ROOT / "operator_ai" / "knowledge" / "playbooks" / f"{domain}.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return None


def _resolve_playbook(cite_file: str, lines: str) -> tuple[str | None, str, str]:
    """-> (text or None, title, problem)."""
    ref = cite_file[len("playbook:"):]
    domain, _, entry_id = ref.partition("/")
    pb = _playbook(domain)
    if pb is None:
        return None, ref, "The cited playbook doesn't exist"
    entry = next((e for e in pb.get("entries", []) if e.get("id") == entry_id), None)
    if entry is None:
        return None, ref, "The cited playbook entry doesn't exist"
    title = f"Playbook: {entry.get('title', entry_id)}"
    lines = (lines or "guidance").strip()
    if lines == "guidance":
        return entry.get("guidance", ""), title, ""
    part, _, n = lines.partition(":")
    items = entry.get(part)
    try:
        i = int(n)
    except ValueError:
        return None, title, "The cited lines aren't in the form steps:N, do:N, dont:N or guidance"
    if not isinstance(items, list) or not 1 <= i <= len(items):
        return None, title, "The cited lines no longer exist"
    return items[i - 1], title, ""


def resolve(tenant, cite_file: str, cite_lines: str) -> dict:
    """The cited SOP text: {text, title, file, lines, kind, problem}."""
    if not cite_file:
        return {"text": None, "title": "", "file": "", "lines": "", "kind": "none", "problem": "No source cited"}
    if cite_file.startswith("playbook:"):
        text, title, problem = _resolve_playbook(cite_file, cite_lines)
        return {"text": text, "title": title, "file": cite_file, "lines": cite_lines or "guidance", "kind": "external",
                "problem": problem}
    from .. import index

    try:
        idx = index.for_tenant(tenant)
    except Exception:  # noqa: BLE001 - a licence or index error means the file can't be checked
        idx = None
    doc = idx.docs.get(cite_file) if idx is not None else None
    if doc is None:
        return {"text": None, "title": cite_file, "file": cite_file, "lines": cite_lines, "kind": "sop",
                "problem": "The cited file isn't in your knowledge base"}
    a, _, b = (cite_lines or "").partition("-")
    try:
        start, end = int(a), int(b or a)
    except ValueError:
        return {"text": None, "title": doc.title, "file": cite_file, "lines": cite_lines, "kind": doc.kind,
                "problem": "The cited lines aren't line numbers"}
    if start < 1 or end > len(doc.lines) or end < start:
        return {"text": None, "title": doc.title, "file": cite_file, "lines": cite_lines, "kind": doc.kind,
                "problem": "The cited lines no longer exist"}
    text = " ".join(doc.lines[start - 1:end]).strip()
    return {"text": text, "title": doc.title, "file": cite_file, "lines": f"{start}-{end}", "kind": doc.kind, "problem": ""}


def validate_rule(tenant, rule: dict) -> dict:
    res = resolve(tenant, rule.get("cite_file") or "", rule.get("cite_lines") or "")
    issues = []
    if res["problem"]:
        issues.append(res["problem"])
    elif res["text"] is not None:
        low = res["text"].lower()
        for term in rule.get("must_contain") or []:
            if str(term).lower() not in low:
                issues.append(f"The cited lines no longer contain \"{term}\"")
        # The rule's own numbers must be what the SOP says (a 45-word limit can't cite a line that says 60).
        for options in param_terms(rule["rule_key"], rule.get("params") or {}):
            if not any(re.search(r"(?<![\w.])" + re.escape(o.lower()) + r"(?![\w])" if not o.endswith("%")
                                 else r"(?<![\w.])" + re.escape(o.lower()), low) for o in options):
                issues.append(f"The cited lines don't state the rule's value ({options[0]})")
    return {"rule_key": rule["rule_key"], "ok": not issues, "issues": issues, "quote": (res["text"] or "")[:300],
            "title": res["title"]}


def validate(tenant) -> list[dict]:
    from .. import guardrails

    names = guardrails.tenant_banned_names(tenant)
    out = []
    for r in rules(tenant).values():
        v = validate_rule(tenant, r)
        v["quote"] = guardrails.scrub(v["quote"], names)
        v["status"] = r["status"]
        v["note"] = r["note"]
        out.append(v)
    return out


def citation(tenant, rule: dict, cid: str) -> dict:
    """A citation chip for a scored criterion: the SOP passage the rule comes from."""
    res = resolve(tenant, rule.get("cite_file") or "", rule.get("cite_lines") or "")
    return {"id": cid, "file": res["file"], "lines": res["lines"], "title": res["title"] or res["file"],
            "snippet": (res["text"] or "")[:300], "kind": res["kind"], "rule_key": rule["rule_key"],
            "rule_status": rule.get("status", "default"), "problem": res["problem"]}
