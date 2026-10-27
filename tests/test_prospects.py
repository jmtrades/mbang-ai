"""First-client engine: prospects, research, personalised drafts, caps, claims, DNC, booking, proposals, win -> client.

Positive and negative tests for the approval gate, guardrails, RBAC and cross-user isolation, the AI-off and AI-on
paths, empty data, malformed input and demo vs real workspace. Everything runs offline."""
from __future__ import annotations

import random
import time
from types import SimpleNamespace

import httpx
import pytest

from operator_ai import auth, db, events, guardrails, llm, queue, runtime, settings
from operator_ai import proposals as PR
from operator_ai import prospects as P
from tests.conftest import demo_login, owner_password
from tests.fakes import FakeModel

API = "/t/owner/api"
LB = {"lawful_basis": "legitimate_interest", "lawful_note": "Business contact details from their own website"}
CSV = """Full Name,IG Handle,E-mail Address,Company Website,Category,Follower Count,Bio,Active Ads,Favourite Colour
Mara Quill,@strongmums_x,mara.quill+promo@gmail.com,https://www.strongmums.example.invalid,fitness coaching,18.4k,"Helping busy mums get strong at home. Glow Method programme with a monthly webinar.",yes,blue
Jonas Pell,https://instagram.com/pellperf,,pellperformance.example.invalid,strength coaching,"42,100",Strength coaching for desk workers,no,red
,,,,,,,,green
Mara Q,@STRONGMUMS_X,,,,,,,
Bad Email Person,,not-an-email,,,,,,
,,also-bad,,,,,,
"""


@pytest.fixture
def world(app, tenant):
    from fastapi.testclient import TestClient

    users = {}
    with tenant.db() as c:
        o = c.execute("SELECT id FROM users WHERE username='owner'").fetchone()
    users["owner"] = {"id": o["id"], "username": "owner", "role": "owner"}
    for name, role in [("stu_a", "student"), ("stu_b", "student"), ("setter1", "setter"), ("coach1", "coach")]:
        users[name] = {"id": auth.create_user(tenant, name, f"pw-{name}-123456", role), "username": name, "role": role}
    clients: dict = {}

    def as_(name: str):
        if name not in clients:
            c = TestClient(app)
            pw = owner_password() if name == "owner" else f"pw-{name}-123456"
            r = c.post(f"{API}/auth/login", json={"username": name, "password": pw})
            assert r.status_code == 200, r.text
            clients[name] = c
        return clients[name]

    return SimpleNamespace(t=tenant, users=users, as_=as_, anon=TestClient(app))


def _import(w, who="stu_a", csv_text=CSV, **kw):
    r = w.as_(who).post(f"{API}/prospects/import", json={"csv": csv_text, "filename": "leads.csv", **LB, **kw})
    assert r.status_code == 200, r.text
    return r.json()


def _add(w, who, **fields):
    return w.as_(who).post(f"{API}/prospects", json={**LB, **fields})


def _pid(w, who, name):
    rows = w.as_(who).get(f"{API}/prospects").json()["prospects"]
    return next(p["id"] for p in rows if p["name"] == name)


def _opener_meta(p: dict, **extra) -> dict:
    return {"feature": "prospects", "prospect_id": p["id"], "owner_user_id": p["owner_user_id"], "prospect_facing": True,
            "lead_tier": "cold", "touch": "opener", **extra}


# --------------------------------------------------------------------------- canonical identifiers
def test_canonical_identifiers():
    assert P.canon_email("Mara.Quill+promo@Gmail.com") == "maraquill@gmail.com"
    assert P.canon_email("maraquill@googlemail.com") == "maraquill@gmail.com"
    assert P.canon_email("not-an-email") == ""
    assert P.canon_handle("https://www.instagram.com/StrongMums_X/") == "strongmums_x"
    assert P.canon_handle("@StrongMums_X") == "strongmums_x"
    assert P.canon_handle("has spaces!") == ""
    assert P.canon_domain("https://www.Strong.example.invalid/path?q=1") == "strong.example.invalid"
    assert P.canon_domain("linktr.ee/someone") == "" and P.canon_domain("instagram.com/x") == ""
    assert P.parse_count("18.4k") == 18400 and P.parse_count("42,100") == 42100 and P.parse_count("1.2M") == 1200000
    assert P.parse_count("lots") is None and P.parse_count(None) is None
    assert P.ads_active("3 active ads") is True and P.ads_active("no") is False and P.ads_active("") is None


# --------------------------------------------------------------------------- import
def test_csv_import_flexible_headers_and_summary(world):
    s = _import(world)
    assert s["rows_total"] == 6 and s["imported"] == 3 and s["skipped_total"] == 3
    used = {c["header"]: c["used_as"] for c in s["columns"]}
    assert used["Full Name"] == "name" and used["IG Handle"] == "handle" and used["E-mail Address"] == "email"
    assert used["Company Website"] == "website" and used["Category"] == "niche" and used["Follower Count"] == "followers"
    assert used["Bio"] == "notes" and used["Active Ads"] == "ad_activity" and used["Favourite Colour"] is None
    reasons = {r["reason"] for r in s["skipped"]}
    assert reasons == {"No name, handle, email or website", "Duplicate row in this file",
                       "The only contact detail is an email that isn't valid"}
    assert s["lawful_basis"] == P.LAWFUL_BASES["legitimate_interest"] and s["run_id"]
    rows = world.as_("stu_a").get(f"{API}/prospects").json()["prospects"]
    mara = next(p for p in rows if p["name"] == "Mara Quill")
    assert mara["followers"] == 18400 and mara["handle"] == "@strongmums_x" and mara["lawful_basis"] == "legitimate_interest"
    assert mara["stage"] == "new" and any(f["claim"].startswith("Followers") for f in mara["facts"])
    assert all(f["source"].startswith("Imported list: leads.csv") for f in mara["facts"])
    bad = next(p for p in rows if p["name"] == "Bad Email Person")
    assert bad["email"] is None
    run = runtime.get(world.t, s["run_id"])
    assert run["agent"] == "prospects" and run["status"] == "done" and len(run["steps"]) == 3
    # the same file again: everything is already there
    again = _import(world)
    assert again["imported"] == 0 and {r["reason"] for r in again["reasons"]} >= {"Already in your prospects"}


def test_import_needs_lawful_basis_and_identity_columns(world, monkeypatch):
    c = world.as_("stu_a")
    base = {"csv": CSV, "filename": "x.csv"}
    assert c.post(f"{API}/prospects/import", json={**base, "lawful_basis": "because", "lawful_note": "x" * 10}).status_code == 422
    assert c.post(f"{API}/prospects/import", json={**base, "lawful_basis": "consent", "lawful_note": ""}).status_code == 422
    assert c.post(f"{API}/prospects/import", json={**base, "lawful_note": "x" * 10}).status_code == 422
    r = c.post(f"{API}/prospects/import", json={"csv": "Colour,Size\nred,big\n", **LB})
    assert r.status_code == 422 and "name, handle, email or website" in r.json()["detail"]
    assert c.post(f"{API}/prospects/import", json={"csv": "   \n", **LB}).status_code == 422
    monkeypatch.setattr(P, "MAX_IMPORT_ROWS", 2)
    r = c.post(f"{API}/prospects/import", json={"csv": CSV, **LB})
    assert r.status_code == 422 and "up to 2" in r.json()["detail"]
    assert c.get(f"{API}/prospects").json()["prospects"] == []  # nothing half-imported


def test_semicolon_csv_and_first_last_name(world):
    s = _import(world, csv_text="First Name;Last Name;Email\nIda;Brook;ida@example.invalid\n")
    assert s["imported"] == 1
    assert world.as_("stu_a").get(f"{API}/prospects").json()["prospects"][0]["name"] == "Ida Brook"


# --------------------------------------------------------------------------- claims, RBAC, isolation
def test_cross_cohort_claims_hide_details_and_owner_can_release(world):
    _import(world, "stu_a")
    pid = _pid(world, "stu_a", "Mara Quill")
    r = _add(world, "stu_b", name="M Q", email="MaraQuill@googlemail.com")
    assert r.status_code == 409 and r.json()["detail"] == "Already being worked by a teammate"
    assert "stu_a" not in r.text and "Mara" not in r.text and str(pid) not in r.text
    s = _import(world, "stu_b", csv_text="Name,Instagram\nAnother,https://instagram.com/strongmums_x\n")
    assert s["imported"] == 0 and s["reasons"] == [{"reason": "Already being worked by a teammate", "count": 1}]
    # B can't move an existing prospect of theirs onto A's contact details either
    assert _add(world, "stu_b", name="Own Lead", email="own@example.invalid").status_code == 200
    own = _pid(world, "stu_b", "Own Lead")
    r = world.as_("stu_b").put(f"{API}/prospects/{own}", json={"handle": "@strongmums_x"})
    assert r.status_code == 409 and "teammate" in r.json()["detail"]
    # only the owner releases
    assert world.as_("stu_a").post(f"{API}/prospects/{pid}/release").status_code == 403
    assert world.as_("owner").post(f"{API}/prospects/{pid}/release").status_code == 200
    assert _add(world, "stu_b", name="Mara Quill", email="maraquill@gmail.com").status_code == 200
    # the original holder can't message a released prospect
    r = world.as_("stu_a").post(f"{API}/prospects/{pid}/drafts", json={"move": "question"})
    assert r.status_code == 409 and "released" in r.json()["detail"]


def test_rbac_and_cross_user_isolation(world):
    _import(world, "stu_a")
    pid = _pid(world, "stu_a", "Mara Quill")
    b = world.as_("stu_b")
    assert b.get(f"{API}/prospects").json()["prospects"] == []
    for method, path, body in [("get", f"/prospects/{pid}", None), ("put", f"/prospects/{pid}", {"niche": "x"}),
                               ("post", f"/prospects/{pid}/stage", {"stage": "lost"}), ("post", f"/prospects/{pid}/dnc", {}),
                               ("post", f"/prospects/{pid}/drafts", {"move": "question"}),
                               ("post", f"/prospects/{pid}/research", {}), ("get", f"/prospects/{pid}/suggestions", None),
                               ("post", f"/prospects/{pid}/reply", {}), ("get", f"/prospects/{pid}/brief", None),
                               ("post", f"/prospects/{pid}/facts", {"claim": "abc", "source": "me"}),
                               ("get", f"/prospects/{pid}/slots", None)]:
        r = getattr(b, method)(API + path, **({"json": body} if body is not None else {}))
        assert r.status_code == 404, (path, r.status_code)
    assert world.as_("stu_a").get(f"{API}/prospects/{pid}").status_code == 200
    owner_rows = world.as_("owner").get(f"{API}/prospects").json()["prospects"]
    assert any(p["id"] == pid and p["owner_name"] for p in owner_rows)
    assert world.as_("setter1").get(f"{API}/prospects").json()["prospects"] == []
    assert world.as_("coach1").get(f"{API}/prospects").status_code == 403
    assert world.as_("stu_a").put(f"{API}/prospects/config", json={"caps": {"dm": 1}}).status_code == 403
    assert world.anon.get(f"{API}/prospects").status_code == 401


# --------------------------------------------------------------------------- do-not-contact and never-invite
def test_dnc_is_honoured_everywhere(world):
    t = world.t
    _import(world, "stu_a")
    pid = _pid(world, "stu_a", "Mara Quill")
    a = world.as_("stu_a")
    d = a.post(f"{API}/prospects/{pid}/drafts", json={"move": "question"}).json()
    assert a.post(f"{API}/prospects/{pid}/dnc", json={"reason": "Asked to stop"}).json()["prospect"]["stage"] == "dnc"
    assert queue.get(t, d["queue_id"])["status"] == "rejected"  # open drafts withdrawn
    assert any(e["payload"].get("party_id") == pid for e in events.recent(t, "dnc_set"))
    assert a.post(f"{API}/prospects/{pid}/stage", json={"stage": "replied"}).status_code == 409
    assert a.post(f"{API}/prospects/{pid}/drafts", json={"move": "question"}).status_code == 409
    assert a.put(f"{API}/prospects/{pid}", json={"niche": "x"}).status_code == 409
    # suppression: re-import by anyone, or adding by another student, is refused
    assert _import(world, "stu_b", csv_text="Email\nmaraquill@gmail.com\n")["reasons"][0]["reason"] == \
        "On the do-not-contact list"
    assert _add(world, "stu_b", name="X", handle="@strongmums_x").status_code == 409
    # any queue item about this prospect is blocked at queue time, whoever writes it
    item = queue.add(t, "mission_draft", "Hi Mara, quick question?", channel="instagram_dm", recipient_ref="@strongmums_x",
                     meta={"prospect_id": pid, "prospect_facing": True, "owner_user_id": world.users["stu_a"]["id"]})
    assert item["status"] == "held" and "do_not_contact" in {f["code"] for f in item["flags"]}


def test_dnc_checked_again_at_approval_and_from_other_features(world):
    t = world.t
    _import(world, "stu_a")
    a = world.as_("stu_a")
    pid = _pid(world, "stu_a", "Mara Quill")
    d = a.post(f"{API}/prospects/{pid}/drafts", json={"move": "question"}).json()
    assert d["status"] == "pending"
    with t.db() as c:  # an opt-out recorded elsewhere, after the draft was written
        c.execute("UPDATE prospects SET do_not_contact=1 WHERE id=?", (pid,))
    r = a.post(f"{API}/queue/{d['queue_id']}/approve")
    assert r.status_code == 409 and queue.get(t, d["queue_id"])["status"] == "held"
    with t.db() as c:
        c.execute("UPDATE prospects SET do_not_contact=0 WHERE id=?", (pid,))
    jonas = _pid(world, "stu_a", "Jonas Pell")
    events.emit(t, "dnc_set", {"party_type": "prospect", "party_id": jonas})  # e.g. the inbound feature saw "stop"
    assert P._row(t, jonas)["stage"] == "dnc"


def test_never_invite_list(world):
    t = world.t
    t.set("blocked_attendees", ["mara.quill@gmail.com"])
    s = _import(world, "stu_a")
    assert {"reason": "On the never-invite list", "count": 1} in s["reasons"]
    assert all(p["name"] != "Mara Quill" for p in world.as_("stu_a").get(f"{API}/prospects").json()["prospects"])


# --------------------------------------------------------------------------- fit score
def test_fit_score_is_transparent_and_owner_weights_rescore(world):
    a = world.as_("stu_a")
    r = a.put(f"{API}/prospects/icp", json={"niche": "fitness coaching", "audience_min": 5000, "audience_max": 50000,
                                             "must_haves": ["programme"], "disqualifiers": ["no offer yet"],
                                             "signals": ["webinar"], "price_min": 500, "price_max": 5000})
    assert r.status_code == 200
    _import(world, "stu_a")
    _add(world, "stu_a", name="Free Only", niche="fitness coaching", notes="Free classes, no offer yet")
    rows = {p["name"]: p for p in a.get(f"{API}/prospects").json()["prospects"]}
    mara = rows["Mara Quill"]
    bd = {b["key"]: b for b in mara["fit_breakdown"]}
    assert bd["niche"]["points"] == 25 and bd["audience"]["points"] == 20 and bd["ads"]["points"] == 20
    assert bd["must_haves"]["points"] == 15 and bd["signals"]["points"] == 10 and bd["price"]["points"] == 0
    assert mara["fit_score"] == sum(b["points"] for b in mara["fit_breakdown"]) == 90
    assert rows["Free Only"]["fit_score"] == 0 and rows["Free Only"]["fit_breakdown"][-1]["key"] == "disqualified"
    assert a.put(f"{API}/prospects/icp", json={"price_min": 10, "price_max": 1}).status_code == 422
    o = world.as_("owner")
    assert o.put(f"{API}/prospects/config", json={"weights": {"ads": 0}}).status_code == 200
    mara2 = next(p for p in a.get(f"{API}/prospects").json()["prospects"] if p["name"] == "Mara Quill")
    assert mara2["fit_score"] == round(100 * 70 / 80)
    assert o.put(f"{API}/prospects/config", json={"weights": {"ads": 500}}).status_code == 422


# --------------------------------------------------------------------------- guardrail plug-in
def test_unverified_personalisation_guard(world):
    t = world.t
    _import(world, "stu_a")
    p = P._row(t, _pid(world, "stu_a", "Mara Quill"))
    meta = _opener_meta(p)

    def codes(text, m=meta):
        return {f["code"] for f in guardrails.check_for(t, text, m)}

    assert "unverified_personalisation" not in codes("Hi Mara, loved your Glow Method programme. How's it going?")
    assert "unverified_personalisation" in codes("Hi Mara, loved your Peak Shred Challenge. How's it going?")
    assert "unverified_personalisation" not in codes("Hi Mara, 18,400 followers is a real community. Who joins most?")
    assert "unverified_personalisation" not in codes("Hi Mara, 18.4k followers is a real community. Who joins most?")
    assert "unverified_personalisation" in codes("Hi Mara, you just passed 50,000 followers. Who joins most?")
    assert "unverified_personalisation" not in codes('Hi Mara, your line "Helping busy mums get strong at home" stood out.')
    assert "unverified_personalisation" in codes('Hi Mara, your line "Strong is the new skinny" stood out.')
    assert "unverified_personalisation" not in codes("Hi Mara, open to a 15-minute chat next week?")
    sig = "Hi Mara, quick question about your webinar?\n\nAva\n--\n12 High Street, Example City\nReply no to opt out."
    assert "unverified_personalisation" not in codes(sig)
    # booking messages carry times; the rule is for openers and follow-ups only
    assert "unverified_personalisation" not in codes("Would Thu 29 Oct 10:00 suit?", {**meta, "touch": "booking"})
    # other features' practice prospects and leads aren't checked against this table
    assert "unverified_personalisation" not in codes("Loved your 50k launch", {"prospect_id": p["id"], "practice": True,
                                                                               "prospect_facing": True})
    assert P.guard_rules("Loved your Peak Shred Challenge", "outreach", meta, None) == []
    # adding the fact (with its source) makes the same line pass
    world.as_("stu_a").post(f"{API}/prospects/{p['id']}/facts",
                            json={"claim": "Runs the Peak Shred Challenge", "source": "Their Instagram highlights",
                                  "source_url": "https://instagram.com/strongmums_x", "quote": "Peak Shred Challenge"})
    assert "unverified_personalisation" not in codes("Hi Mara, loved your Peak Shred Challenge. How's it going?")


def test_fact_validation(world):
    _import(world, "stu_a")
    pid = _pid(world, "stu_a", "Mara Quill")
    a = world.as_("stu_a")
    assert a.post(f"{API}/prospects/{pid}/facts", json={"claim": "ab", "source": "x"}).status_code == 422
    r = a.post(f"{API}/prospects/{pid}/facts", json={"claim": "Runs ads", "source": "Ad library", "source_url": "javascript:x"})
    assert r.status_code == 422


# --------------------------------------------------------------------------- suggestions, drafts, approval gate
def test_suggestions_ai_off_are_templates_and_clean(world):
    _import(world, "stu_a")
    a = world.as_("stu_a")
    pid = _pid(world, "stu_a", "Mara Quill")
    s = a.get(f"{API}/prospects/{pid}/suggestions", params={"channel": "instagram_dm", "ai": "true"}).json()
    assert s["mode"] == "rules" and "AI off" in s["label"] and s["run_id"]
    by = {v["move"]: v for v in s["variants"]}
    assert by["compliment"]["available"] and '"Helping busy mums get strong at home"' in by["compliment"]["text"]
    assert not by["mutual"]["available"] and "mutual" in by["mutual"]["reason"].lower()
    assert all(not guardrails.blocked(v["flags"]) for v in s["variants"])
    assert s["recommended"]["how"] == "explore"
    _add(world, "stu_a", name="Handle Only", handle="@handle_only")  # no bio, no ads -> nothing specific to compliment
    bare = _pid(world, "stu_a", "Handle Only")
    js = a.get(f"{API}/prospects/{bare}/suggestions").json()
    assert not next(v for v in js["variants"] if v["move"] == "compliment")["available"]
    assert a.get(f"{API}/prospects/{bare}/suggestions", params={"channel": "email"}).status_code == 409  # no email
    assert a.get(f"{API}/prospects/{pid}/suggestions", params={"channel": "sms"}).status_code == 422


def test_draft_is_queued_for_own_approval_then_followups_scheduled(world):
    t = world.t
    _import(world, "stu_a")
    a, b = world.as_("stu_a"), world.as_("stu_b")
    pid = _pid(world, "stu_a", "Mara Quill")
    uid = world.users["stu_a"]["id"]
    d = a.post(f"{API}/prospects/{pid}/drafts", json={"move": "compliment"}).json()
    item = queue.get(t, d["queue_id"])
    assert item["kind"] == "prospect_dm" and item["status"] == "pending" and item["recipient_ref"] == "@strongmums_x"
    m = item["meta"]
    assert m["owner_user_id"] == uid and m["prospect_facing"] is True and m["lead_tier"] == "cold"
    assert m["prospect_id"] == pid and m["touch"] == "opener" and m["move"] == "compliment"
    assert with_no_send(t)  # drafting sent nothing
    assert a.post(f"{API}/prospects/{pid}/drafts", json={"move": "question"}).status_code == 409  # one first message
    # nobody else may approve it; the author may
    assert b.post(f"{API}/queue/{d['queue_id']}/approve").status_code == 403
    assert b.get(f"{API}/queue/{d['queue_id']}").status_code == 404
    assert world.as_("setter1").post(f"{API}/queue/{d['queue_id']}/approve").status_code == 403
    before = time.time()
    assert a.post(f"{API}/queue/{d['queue_id']}/approve").status_code == 200
    p = P._row(t, pid)
    assert p["stage"] == "contacted"
    assert any(e["payload"] == {"prospect_id": pid, "stage": "contacted", "owner_user_id": uid}
               for e in events.recent(t, "prospect_stage"))
    fus = [x for x in a.get(f"{API}/prospects/{pid}").json()["drafts"] if x["touch"] == "followup"]
    assert len(fus) == 3
    for f, days in zip(sorted(fus, key=lambda x: x["send_after"]), P.FOLLOWUP_DAYS):
        q = queue.get(t, f["id"])
        assert q["kind"] == "followup" and q["status"] == "pending" and q["meta"]["prior_contact"] is True
        assert q["meta"]["cancel_if"] == ["lead_replied_since", "prospect_dnc"]
        assert abs(q["meta"]["send_after"] - (before + days * 86400)) < 120
        assert q["meta"]["owner_user_id"] == uid and q["flags"] == []
    assert P.usage(t, uid)["dm"]["sent_today"] == 1


def with_no_send(t) -> bool:
    with t.db() as c:
        return c.execute("SELECT COUNT(*) FROM channel_messages WHERE direction='out'").fetchone()[0] == 0


def test_edited_draft_with_invented_claim_is_held_and_cannot_be_approved(world):
    t = world.t
    _import(world, "stu_a")
    a = world.as_("stu_a")
    pid = _pid(world, "stu_a", "Mara Quill")
    d = a.post(f"{API}/prospects/{pid}/drafts",
               json={"move": "compliment", "text": "Hi Mara, congrats on the Shredded Summer Bootcamp selling out!"}).json()
    assert d["status"] == "held" and "unverified_personalisation" in {f["code"] for f in d["flags"]}
    assert a.post(f"{API}/queue/{d['queue_id']}/approve").status_code == 409
    assert with_no_send(t)
    # a draft with a guarantee is held too (core guardrails still apply)
    q = a.put(f"{API}/queue/{d['queue_id']}", json={"body": "Hi Mara, I guarantee you more clients."}).json()
    assert q["status"] == "held"
    q = a.put(f"{API}/queue/{d['queue_id']}", json={"body": "Hi Mara, how are new clients finding you right now?"}).json()
    assert q["status"] == "pending"


def test_email_variant_is_honest_and_needs_a_postal_address(world):
    t = world.t
    _import(world, "stu_a")
    a = world.as_("stu_a")
    pid = _pid(world, "stu_a", "Mara Quill")
    d = a.post(f"{API}/prospects/{pid}/drafts", json={"move": "question", "channel": "email"}).json()
    item = queue.get(t, d["queue_id"])
    assert item["kind"] == "prospect_email" and item["status"] == "held"
    assert {f["code"] for f in item["flags"]} >= {"unfilled_placeholder"}
    assert not item["meta"]["subject"].lower().startswith(("re:", "fwd:"))
    assert "{{unsubscribe_url}}" in item["body"] and "won't contact you again" in item["body"]
    a.put(f"{API}/prospects/icp", json={"niche": "fitness coaches", "sender": {
        "name": "Sam Sender", "business": "Sender Growth Co", "postal_address": "12 High Street, Example City"}})
    a.post(f"{API}/queue/{d['queue_id']}/reject")
    d2 = a.post(f"{API}/prospects/{pid}/drafts", json={"move": "question", "channel": "email"}).json()
    item2 = queue.get(t, d2["queue_id"])
    assert item2["status"] == "pending", item2["flags"]
    assert "Sam Sender\nSender Growth Co\n--\n12 High Street, Example City" in item2["body"]


def test_daily_caps_count_confirmed_sends_and_waiting_first_messages(world):
    t = world.t
    o, a = world.as_("owner"), world.as_("stu_a")
    assert o.put(f"{API}/prospects/config", json={"caps": {"dm": 2, "email": 5}}).json()["caps"] == {"dm": 2, "email": 5}
    rows = "\n".join(f"Lead {i},@lead_{i}" for i in range(5))
    _import(world, "stu_a", csv_text=f"Name,Instagram\n{rows}\n")
    r = a.post(f"{API}/prospects/draft-openers", json={"channel": "instagram_dm", "limit": 10}).json()
    assert len(r["queued"]) == 2 and sum(1 for s in r["skipped"] if s["reason"] == "Daily cap reached") == 3
    uid = world.users["stu_a"]["id"]
    assert P.usage(t, uid)["dm"] == {"cap": 2, "sent_today": 0, "waiting": 2, "remaining": 0}
    a.post(f"{API}/queue/{r['queued'][0]['queue_id']}/approve")
    assert P.usage(t, uid)["dm"] == {"cap": 2, "sent_today": 1, "waiting": 1, "remaining": 0}
    a.post(f"{API}/queue/{r['queued'][1]['queue_id']}/reject")
    assert P.usage(t, uid)["dm"]["remaining"] == 1
    pid = r["skipped"][0]["prospect_id"]
    assert a.post(f"{API}/prospects/{pid}/drafts", json={"move": "question"}).status_code == 200
    other = r["skipped"][1]["prospect_id"]
    d = a.post(f"{API}/prospects/{other}/drafts", json={"move": "question"})
    assert d.status_code == 409 and "cap" in d.json()["detail"]
    # caps are per person: another student is unaffected
    assert P.usage(t, world.users["stu_b"]["id"])["dm"]["remaining"] == 2


def test_on_sent_ignores_other_features_items(world):
    t = world.t
    it = queue.add(t, "followup", "Here's a short video on show rates. Want it?", channel="instagram_dm",
                   recipient_ref="@someone", meta={"lead_id": 1, "prospect_facing": True, "prior_contact": True})
    queue.approve(t, it["id"], "owner(owner)")  # no prospects row involved: must not raise
    with t.db() as c:
        assert c.execute("SELECT COUNT(*) FROM prospect_touches").fetchone()[0] == 0


# --------------------------------------------------------------------------- opener stats + chooser
def _touches(t, uid, spec):
    now = db.now()
    with t.db() as c:
        for move, (n, r) in spec.items():
            for i in range(n):
                c.execute("INSERT INTO prospect_touches(prospect_id,owner_user_id,touch,move,channel,family,sent_at,replied_at,"
                          "created) VALUES(NULL,?,'opener',?,'instagram_dm','dm',?,?,?)",
                          (uid, move, now - 86400, now if i < r else None, now - 86400))


def test_move_stats_wilson_and_thompson_after_min_sample(world):
    t = world.t
    uid = world.users["stu_a"]["id"]
    _touches(t, uid, {"question": (30, 15), "compliment": (30, 3), "resource": (5, 1), "mutual": (30, 3)})
    st = {s["move"]: s for s in P.move_stats(t, uid, "dm")}
    assert st["question"]["sent"] == 30 and st["question"]["replied"] == 15 and st["question"]["reply_rate"] == 0.5
    from operator_ai.outreach_loop import wilson

    lo, hi = wilson(15, 30)
    assert st["question"]["reply_ci95"] == [round(lo, 3), round(hi, 3)]
    pick = P.choose_move(t, uid, list(P.MOVES), "dm", random.Random(1))
    assert pick == {"move": "resource", "how": "explore", "why": pick["why"]} and "20 sends" in pick["why"]
    _touches(t, uid, {"resource": (25, 2)})
    rng = random.Random(7)
    picks = [P.choose_move(t, uid, list(P.MOVES), "dm", rng) for _ in range(200)]
    assert {p["how"] for p in picks} == {"thompson"}
    assert sum(p["move"] == "question" for p in picks) > 180
    assert P.choose_move(t, uid, [], "dm")["move"] is None
    # stats are per person (owner sees all)
    assert all(s["sent"] == 0 for s in P.move_stats(t, world.users["stu_b"]["id"], "dm"))
    stats = world.as_("stu_a").get(f"{API}/prospects/stats").json()
    assert stats["moves"]["dm"][0]["sent"] == 30 and stats["min_sample"] == 20


# --------------------------------------------------------------------------- replies, events
def test_reply_moves_stage_withdraws_followups_and_emits(world):
    t = world.t
    _import(world, "stu_a")
    a = world.as_("stu_a")
    pid = _pid(world, "stu_a", "Mara Quill")
    d = a.post(f"{API}/prospects/{pid}/drafts", json={"move": "question"}).json()
    a.post(f"{API}/queue/{d['queue_id']}/approve")
    r = a.post(f"{API}/prospects/{pid}/reply", json={"text": "Tell me more", "classification": "interested"})
    assert r.json()["prospect"]["stage"] == "replied"
    fus = [x for x in a.get(f"{API}/prospects/{pid}").json()["drafts"] if x["touch"] == "followup"]
    assert fus and all(f["status"] == "rejected" for f in fus)
    ev = events.recent(t, "reply_received")[0]["payload"]
    assert ev["party_type"] == "prospect" and ev["party_id"] == pid and ev["source"] == "prospects"
    st = {s["move"]: s for s in P.move_stats(t, world.users["stu_a"]["id"], "dm")}
    assert st["question"]["replied"] == 1
    assert a.post(f"{API}/prospects/{pid}/reply", json={"classification": "maybe"}).status_code == 422
    # replies coming from the inbound feature
    jonas = _pid(world, "stu_a", "Jonas Pell")
    events.emit(t, "reply_received", {"party_type": "prospect", "party_id": jonas, "classification": "not_now"})
    assert P._row(t, jonas)["stage"] == "nurture"
    bad = _pid(world, "stu_a", "Bad Email Person")
    events.emit(t, "reply_received", {"party_type": "prospect", "party_id": bad, "classification": "unsubscribe"})
    assert P._row(t, bad)["do_not_contact"] == 1
    events.emit(t, "reply_received", {"party_type": "lead", "party_id": pid, "classification": "unsubscribe"})
    assert P._row(t, pid)["do_not_contact"] == 0  # leads are a different table


def test_manual_won_creates_client_and_emits(world):
    t = world.t
    _import(world, "stu_a")
    pid = _pid(world, "stu_a", "Jonas Pell")
    r = world.as_("stu_a").post(f"{API}/prospects/{pid}/stage", json={"stage": "won", "reason": "Signed on a call"})
    p = r.json()["prospect"]
    assert p["stage"] == "won" and p["client_id"]
    with t.db() as c:
        cl = dict(c.execute("SELECT * FROM clients WHERE id=?", (p["client_id"],)).fetchone())
    assert cl["name"] == "Jonas Pell" and cl["demo"] == 0 and cl["metrics"] == "{}"
    ev = events.recent(t, "client_won")[0]["payload"]
    assert ev == {"client_id": p["client_id"], "prospect_id": pid, "owner_user_id": world.users["stu_a"]["id"]}
    # idempotent
    assert world.as_("stu_a").post(f"{API}/prospects/{pid}/stage", json={"stage": "won"}).json()["prospect"]["client_id"] == \
        p["client_id"]
    assert world.as_("stu_a").post(f"{API}/prospects/{pid}/stage", json={"stage": "bogus"}).status_code == 422


# --------------------------------------------------------------------------- booking
def test_booking_offer_record_and_brief(world, monkeypatch):
    t = world.t
    _import(world, "stu_a")
    a = world.as_("stu_a")
    pid = _pid(world, "stu_a", "Mara Quill")
    a.post(f"{API}/prospects/{pid}/reply", json={"text": "Sounds good, what times work?"})
    slots = a.get(f"{API}/prospects/{pid}/slots").json()["slots"]
    assert len(slots) == 3 and all(s["start"] > time.time() for s in slots)
    off = a.post(f"{API}/prospects/{pid}/book", json={"mode": "offer"}).json()
    q = queue.get(t, off["queue_id"])
    assert q["kind"] == "booking_dm" and q["status"] == "pending" and q["meta"]["owner_user_id"] == world.users["stu_a"]["id"]
    assert a.post(f"{API}/prospects/{pid}/book", json={"mode": "record", "start": time.time() - 86400}).status_code == 422
    assert a.post(f"{API}/prospects/{pid}/book", json={"mode": "record"}).status_code == 422
    rec = a.post(f"{API}/prospects/{pid}/book", json={"mode": "record", "start": slots[0]["start"]})
    assert rec.status_code == 200, rec.text
    body = rec.json()
    assert body["prospect"]["stage"] == "call_booked" and body["brief"]["questions"] and body["brief"]["do_not_say"]
    assert body["brief"]["their_words"] == ["Sounds good, what times work?"]
    assert any(e["payload"].get("party_id") == pid for e in events.recent(t, "booking_made"))
    with t.db() as c:
        ev = c.execute("SELECT attendees FROM calendar_events WHERE id=?", (body["event_id"],)).fetchone()
    assert ev["attendees"] == "[]"  # a calendar hold, not an invite
    taken = a.post(f"{API}/prospects/{pid}/book", json={"mode": "record", "start": slots[0]["start"]})
    assert taken.status_code == 409
    # no free times: a clear 409, never a crash
    monkeypatch.setattr(P, "slots", lambda tenant, n=3: [])
    r = a.post(f"{API}/prospects/{pid}/book", json={"mode": "offer"})
    assert r.status_code == 409 and "No free times" in r.json()["detail"]
    assert a.get(f"{API}/prospects/{pid}/slots").json() == {"slots": [], "message": "No free times in the next two weeks."}
    events.emit(t, "booking_cancelled", {"party_type": "prospect", "party_id": pid})
    assert P._row(t, pid)["stage"] == "replied"


# --------------------------------------------------------------------------- proposals
PRICING = {"currency": "GBP", "retainer": 2000, "performance_pct": 10, "pilot_fee": 1200, "pilot_days": 30, "term_months": 3}


def _proposal(world, who="stu_a", name="Mara Quill", **kw):
    pid = _pid(world, who, name)
    body = {"prospect_id": pid, "call_notes": "We make $40k a month from the group programme. I spend my evenings answering "
                                              "DMs by hand. We want more booked calls without hiring.",
            "numbers": {"leads": 120, "booked": 14, "showed": 9, "closed": 3, "offer_price": 900}, "pricing": PRICING, **kw}
    r = world.as_(who).post(f"{API}/prospects/proposals", json=body)
    assert r.status_code == 200, r.text
    return pid, r.json()["proposal"]


def test_proposal_sections_assumptions_and_guardrails(world):
    _import(world, "stu_a")
    _, pr = _proposal(world)
    keys = [s["key"] for s in pr["sections"]]
    assert keys == ["problem", "bottleneck", "plan", "deliverables", "options", "terms", "projection", "no_guarantee"]
    sec = {s["key"]: s for s in pr["sections"]}
    assert "$40k" not in sec["problem"]["text"] and "evenings answering DMs" in sec["problem"]["text"]
    assert "Left out 1 line" in sec["problem"]["text"]
    assert "£" in sec["bottleneck"]["text"] and sec["bottleneck"]["assumptions"]
    assert "(model)" in sec["projection"]["text"] and sec["projection"]["assumptions"]
    assert "don't guarantee" in sec["no_guarantee"]["text"]
    assert "net cash collected" in sec["terms"]["text"]
    assert [o["key"] for o in pr["options"]] == ["retainer", "retainer_performance", "pilot"]
    assert pr["options"][0]["amount"] == 2000 and pr["options"][0]["currency"] == "GBP"
    assert pr["status"] == "draft" and not pr["blocked"] and pr["mode"] == "rules" and pr["url"] is None
    # no numbers: no cost and no projection, said plainly
    _, pr2 = _proposal(world, name="Jonas Pell", numbers={}, call_notes="")
    sec2 = {s["key"]: s for s in pr2["sections"]}
    assert "projection" not in sec2 and "No numbers were shared" in sec2["bottleneck"]["text"]
    assert "Add your call notes" in sec2["problem"]["text"]


def test_proposal_inputs_validated(world):
    _import(world, "stu_a")
    pid = _pid(world, "stu_a", "Mara Quill")
    a = world.as_("stu_a")
    base = {"prospect_id": pid, "pricing": PRICING}
    assert a.post(f"{API}/prospects/proposals", json={**base, "numbers": {"leads": "lots"}}).status_code == 422
    assert a.post(f"{API}/prospects/proposals", json={**base, "pricing": {**PRICING, "retainer": 0}}).status_code == 422
    assert a.post(f"{API}/prospects/proposals", json={**base, "pricing": {**PRICING, "currency": "XYZ"}}).status_code == 422
    assert a.post(f"{API}/prospects/proposals",
                  json={**base, "pricing": {**PRICING, "options": ["free_forever"]}}).status_code == 422
    assert a.post(f"{API}/prospects/proposals", json={"prospect_id": pid}).status_code == 422
    assert world.as_("stu_b").post(f"{API}/prospects/proposals", json=base).status_code == 404


def test_blocked_proposal_cannot_be_sent(world):
    _import(world, "stu_a")
    _, pr = _proposal(world)
    a = world.as_("stu_a")
    r = a.put(f"{API}/prospects/proposals/{pr['id']}",
              json={"sections": [{"key": "plan", "text": "We guarantee you'll double revenue in the first month."}]})
    assert r.json()["proposal"]["blocked"]
    s = a.post(f"{API}/prospects/proposals/{pr['id']}/send", json={"channel": "email"})
    assert s.status_code == 409 and "flagged" in s.json()["detail"]
    ok = a.put(f"{API}/prospects/proposals/{pr['id']}",
               json={"sections": [{"key": "plan", "text": "- Days 1 to 30: measure and fix the booking step."}]})
    assert not ok.json()["proposal"]["blocked"]
    assert a.put(f"{API}/prospects/proposals/{pr['id']}", json={"sections": [{"key": "plan", "text": ""}]}).status_code == 422


def test_proposal_send_approve_public_page_accept_win(world):
    t = world.t
    _import(world, "stu_a")
    pid, pr = _proposal(world)
    a, b = world.as_("stu_a"), world.as_("stu_b")
    s = a.post(f"{API}/prospects/proposals/{pr['id']}/send", json={"channel": "email"}).json()
    item = queue.get(t, s["queue_id"])
    assert item["kind"] == "proposal" and item["status"] == "pending" and item["meta"]["client_asset"] is True
    assert item["meta"]["owner_user_id"] == world.users["stu_a"]["id"] and "prospect_facing" not in item["meta"]
    with t.db() as c:
        token = c.execute("SELECT token FROM proposals WHERE id=?", (pr["id"],)).fetchone()["token"]
    page = f"/t/owner/p/{token}"
    assert world.anon.get(page).status_code == 404  # not live until approved
    assert world.anon.post(page + "/accept", json={"name": "Mara Quill", "agree": True, "option": "retainer"}).status_code == 404
    assert b.post(f"{API}/queue/{s['queue_id']}/approve").status_code == 403
    assert b.get(f"{API}/prospects/proposals/{pr['id']}").status_code == 404
    assert a.post(f"{API}/queue/{s['queue_id']}/approve").status_code == 200
    assert P._row(t, pid)["stage"] == "proposal"
    r = world.anon.get(page)
    assert r.status_code == 200 and "Proposal for" in r.text and "don&#x27;t guarantee" in r.text
    assert r.headers["x-robots-tag"].startswith("noindex") and "<script>alert" not in r.text
    a.get(page)  # the team's own views don't count
    got = a.get(f"{API}/prospects/proposals/{pr['id']}").json()["proposal"]
    assert got["views"] == 1 and got["url"].endswith(page) and got["status"] == "published"
    acc = world.anon.post
    assert acc(page + "/accept", json={"name": "M", "agree": True, "option": "retainer"}).status_code == 422
    assert acc(page + "/accept", json={"name": "Mara Quill", "agree": False, "option": "retainer"}).status_code == 422
    assert acc(page + "/accept", json={"name": "Mara Quill", "agree": True, "option": "free"}).status_code == 422
    assert acc(page + "/accept", content=b"{broken", headers={"content-type": "application/json"}).status_code == 422
    ok = acc(page + "/accept", json={"name": "Mara Quill", "agree": True, "option": "pilot"})
    assert ok.status_code == 200 and ok.json()["option"] == "pilot"
    assert acc(page + "/accept", json={"name": "Mara Quill", "agree": True, "option": "pilot"}).status_code == 409
    p = P._row(t, pid)
    assert p["stage"] == "won" and p["client_id"]
    with t.db() as c:
        cl = c.execute("SELECT * FROM clients WHERE id=?", (p["client_id"],)).fetchone()
        row = c.execute("SELECT * FROM proposals WHERE id=?", (pr["id"],)).fetchone()
    assert cl["price"] == 1200 and cl["offer"] == "Paid pilot" and cl["demo"] == 0
    assert row["accepted_name"] == "Mara Quill" and row["accepted_at"] and len(row["accepted_ip_hash"]) == 32
    assert "testclient" not in row["accepted_ip_hash"]
    assert events.recent(t, "client_won")[0]["payload"]["prospect_id"] == pid
    assert "Accepted by Mara Quill" in world.anon.get(page).text
    assert world.anon.get("/t/owner/p/not-a-real-token").status_code == 404
    assert world.anon.get("/t/nope/p/x").status_code == 404


def test_withdraw_and_rejected_proposal(world):
    t = world.t
    _import(world, "stu_a")
    _, pr = _proposal(world)
    a = world.as_("stu_a")
    s = a.post(f"{API}/prospects/proposals/{pr['id']}/send", json={"channel": "email"}).json()
    a.post(f"{API}/queue/{s['queue_id']}/reject")  # rejected at approval: back to draft
    assert a.get(f"{API}/prospects/proposals/{pr['id']}").json()["proposal"]["status"] == "draft"
    s2 = a.post(f"{API}/prospects/proposals/{pr['id']}/send", json={"channel": "email"}).json()
    w = a.post(f"{API}/prospects/proposals/{pr['id']}/withdraw").json()["proposal"]
    assert w["status"] == "withdrawn" and queue.get(t, s2["queue_id"])["status"] == "rejected"
    assert a.post(f"{API}/prospects/proposals/{pr['id']}/withdraw").status_code == 409
    assert a.post(f"{API}/prospects/proposals/{pr['id']}/send", json={}).status_code == 409


# --------------------------------------------------------------------------- website research (switch, allowlist, robots)
class _Site:
    def __init__(self, robots: str | None = "User-agent: *\nAllow: /\n", redirect: str | None = None):
        self.calls: list[str] = []
        self.robots, self.redirect = robots, redirect

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(f"{request.url.host}{request.url.path}")
        if request.url.path == "/robots.txt":
            return httpx.Response(404) if self.robots is None else httpx.Response(200, text=self.robots)
        if self.redirect:
            return httpx.Response(302, headers={"location": self.redirect})
        return httpx.Response(200, text="<html><head><title>Strong Mums Club</title><meta name=\"description\" "
                                         "content=\"Home workouts for busy mums\"></head><body><h1>Get strong at home</h1>"
                                         "<a href='https://calendly.com/x'>Book a call</a> Join for $97 a month."
                                         "<script>fbq('init','1')</script></body></html>")


@pytest.fixture
def site():
    from operator_ai.live import net

    holder = {}

    def use(s: _Site):
        holder["s"] = s
        net.set_transport(httpx.MockTransport(s.handler))
        return s

    yield use
    net.set_transport(None)


def test_web_research_switch_off_by_default(world, site):
    t = world.t
    s = site(_Site())
    _import(world, "stu_a")
    pid = _pid(world, "stu_a", "Mara Quill")
    r = world.as_("stu_a").post(f"{API}/prospects/{pid}/research", json={"web": True}).json()
    assert r["web"]["status"] == "off" and s.calls == [] and r["prospect"]["stage"] == "researched"
    assert world.as_("stu_a").get(f"{API}/prospects").json()["web_research"] is False
    with pytest.raises(ValueError):
        P.set_web_research(t, True, "")
    assert t.get("prospect_web_research") is None


def test_web_research_robots_and_domain_allowlist(world, site):
    t = world.t
    P.set_web_research(t, True, "owner agreed in writing")
    _import(world, "stu_a")
    pid = _pid(world, "stu_a", "Mara Quill")
    a = world.as_("stu_a")
    s = site(_Site(robots="User-agent: *\nDisallow: /\n"))
    r = a.post(f"{API}/prospects/{pid}/research", json={"web": True}).json()
    assert r["web"]["status"] == "blocked" and s.calls == ["strongmums.example.invalid/robots.txt"]
    s = site(_Site(redirect="https://elsewhere.example.invalid/"))
    r = a.post(f"{API}/prospects/{pid}/research", json={"web": True}).json()
    assert r["web"]["status"] == "blocked" and all(c.startswith("strongmums.example.invalid") for c in s.calls)
    s = site(_Site())
    r = a.post(f"{API}/prospects/{pid}/research", json={"web": True}).json()
    assert r["web"]["status"] == "ok"
    facts = [f for f in r["prospect"]["facts"] if f["kind"] == "web"]
    claims = {f["claim"] for f in facts}
    assert {"Page title", "Site description", "Main headline", "Has a booking or application step",
            "Meta pixel present (likely runs or plans ads)"} <= claims
    assert all(f["source_url"] == "https://strongmums.example.invalid/" for f in facts)
    assert set(s.calls) == {"strongmums.example.invalid/robots.txt", "strongmums.example.invalid/"}
    # a prospect without a website never triggers a request
    s = site(_Site())
    jonas_bad = _pid(world, "stu_a", "Bad Email Person")
    r = a.post(f"{API}/prospects/{jonas_bad}/research", json={"web": True}).json()
    assert r["web"]["status"] == "no_website" and s.calls == []


# --------------------------------------------------------------------------- AI on (optional) path
@pytest.fixture
def fake_model(monkeypatch):
    fm = FakeModel()
    llm.set_transport(fm.transport)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-key-123456789")
    monkeypatch.setenv("OPERATOR_DISABLE_LLM", "0")
    yield fm
    llm.set_transport(None)


def test_ai_polish_is_guardrailed_and_labelled(world, fake_model):
    t = world.t
    _import(world, "stu_a")
    pid = _pid(world, "stu_a", "Mara Quill")
    a = world.as_("stu_a")
    # budget 0: no request at all
    s = a.get(f"{API}/prospects/{pid}/suggestions", params={"ai": "true"}).json()
    assert s["mode"] == "rules" and fake_model.calls == []
    settings.set_box(t, "ai_token_budget", 100000, "owner approved test budget")
    fake_model.reply = "Congrats on hitting 50,000 followers with the Peak Shred Challenge! How's it going?"
    s = a.get(f"{API}/prospects/{pid}/suggestions", params={"ai": "true"}).json()
    assert fake_model.calls and s["mode"] == "rules" and "guardrails" in s["label"]
    assert all("Peak Shred" not in v.get("text", "") for v in s["variants"])
    fake_model.reply = "what's the main way new mums find the Glow Method programme right now?"
    s = a.get(f"{API}/prospects/{pid}/suggestions", params={"ai": "true"}).json()
    assert s["mode"] == "live" and s["label"] == "AI-polished from the research"
    assert any("Glow Method programme right now" in v.get("text", "") for v in s["variants"])
    assert runtime.get(t, s["run_id"])["ai_mode"] == "live"
    # without ?ai=true the AI is never used
    n = len(fake_model.calls)
    assert a.get(f"{API}/prospects/{pid}/suggestions").json()["mode"] == "rules" and len(fake_model.calls) == n


# --------------------------------------------------------------------------- malformed input, empty data
def test_malformed_bodies_get_422_never_500(world):
    _import(world, "stu_a")
    pid = _pid(world, "stu_a", "Mara Quill")
    a = world.as_("stu_a")
    for path in ["/prospects", "/prospects/import", "/prospects/draft-openers", "/prospects/proposals",
                 f"/prospects/{pid}/stage", f"/prospects/{pid}/dnc", f"/prospects/{pid}/research", f"/prospects/{pid}/facts",
                 f"/prospects/{pid}/drafts", f"/prospects/{pid}/reply", f"/prospects/{pid}/book"]:
        for body in (b"{broken", b"[1,2]", b'"text"'):
            r = a.post(API + path, content=body, headers={"content-type": "application/json"})
            assert r.status_code == 422, (path, body, r.status_code)
        r = a.post(API + path, content=b'{"stage": 5, "move": [], "mode": {}, "csv": 7, "web": "maybe", "limit": "x"}',
                   headers={"content-type": "application/json"})
        assert r.status_code < 500, (path, r.status_code)
    assert a.put(f"{API}/prospects/icp", json={"must_haves": "x" * 10, "audience_min": "big"}).status_code == 422
    assert a.put(f"{API}/prospects/{pid}", json={"followers": {"a": 1}}).status_code == 422
    assert a.get(f"{API}/prospects", params={"stage": "everything"}).status_code == 422
    assert a.get(f"{API}/prospects/999999").status_code == 404
    assert a.post(f"{API}/prospects/draft-openers", json={"channel": "fax"}).status_code == 422
    r = _add(world, "stu_a", followers="lots", name="Count Unknown")
    assert r.status_code == 200 and r.json()["prospect"]["followers"] is None
    assert _add(world, "stu_a", website="not a site", lawful_basis="consent").status_code == 409  # no identity


def test_empty_workspace_answers_cleanly(world):
    a = world.as_("setter1")
    ov = a.get(f"{API}/prospects").json()
    assert ov["prospects"] == [] and ov["lists"] == [] and ov["icp_ready"] is False
    assert {s["key"] for s in ov["stages"]} == set(P.STAGES)
    st = a.get(f"{API}/prospects/stats").json()
    assert all(r["count"] == 0 for r in st["pipeline"]) and st["proposals"] == {}
    assert a.get(f"{API}/prospects/proposals").json()["proposals"] == []
    assert a.get(f"{API}/prospects/imports").json()["imports"] == []
    assert a.post(f"{API}/prospects/draft-openers", json={}).json()["queued"] == []
    icp = a.get(f"{API}/prospects/icp").json()["icp"]
    assert icp["niche"] == "" and icp["sender"]["postal_address"] == ""


# --------------------------------------------------------------------------- demo vs real
def test_demo_seed_is_fictional_and_real_workspace_is_empty(client, demo_tenant, tenant):
    with demo_tenant.db() as c:
        uid = c.execute("SELECT id FROM users WHERE username='demo-student'").fetchone()["id"]
        rows = [dict(r) for r in c.execute("SELECT * FROM prospects")]
        props = [dict(r) for r in c.execute("SELECT * FROM proposals")]
    assert len(rows) == 12 and all(r["owner_user_id"] == uid and r["demo"] == 1 for r in rows)
    assert all(r["name"].endswith("(DEMO)") and r["email"].endswith("@example.invalid") for r in rows)
    assert len({r["stage"] for r in rows}) >= 7 and len(props) == 1 and props[0]["status"] == "queued"
    assert P.get_icp(demo_tenant, uid)["niche"]
    with demo_tenant.db() as c:
        q = c.execute("SELECT status, demo FROM queue WHERE id=?", (props[0]["queue_id"],)).fetchone()
    assert q["status"] == "pending" and q["demo"] == 1
    # the real workspace never gets demo rows
    with tenant.db() as c:
        assert c.execute("SELECT COUNT(*) FROM prospects").fetchone()[0] == 0
    assert P.seed_demo(tenant) == {"seeded": 0}
    with tenant.db() as c:
        assert c.execute("SELECT COUNT(*) FROM prospects").fetchone()[0] == 0
    # demo API: the student sees their 12, a setter sees none, the owner sees all
    demo_login(client, "student")
    assert len(client.get("/t/demo/api/prospects").json()["prospects"]) == 12
    demo_login(client, "setter")
    assert client.get("/t/demo/api/prospects").json()["prospects"] == []
    demo_login(client, "owner")
    assert len(client.get("/t/demo/api/prospects").json()["prospects"]) == 12
    demo_login(client, "coach")
    assert client.get("/t/demo/api/prospects").status_code == 403


def test_demo_student_full_flow(client, demo_tenant):
    demo_login(client, "student")
    rows = client.get("/t/demo/api/prospects").json()["prospects"]
    p = next(r for r in rows if r["stage"] == "researched")
    s = client.get(f"/t/demo/api/prospects/{p['id']}/suggestions").json()
    move = next(v["move"] for v in s["variants"] if v["available"])
    d = client.post(f"/t/demo/api/prospects/{p['id']}/drafts", json={"move": move}).json()
    assert d["status"] == "pending"
    assert client.post(f"/t/demo/api/queue/{d['queue_id']}/approve").status_code == 200  # mock transport in the demo
    det = client.get(f"/t/demo/api/prospects/{p['id']}").json()
    assert det["prospect"]["stage"] == "contacted" and sum(1 for x in det["drafts"] if x["touch"] == "followup") == 3
    prop = client.get("/t/demo/api/prospects/proposals").json()["proposals"][0]
    assert prop["status"] == "queued" and not prop["blocked"]
    # approving the demo proposal (the student's own) moves that prospect to "Proposal sent"
    assert client.post(f"/t/demo/api/queue/{prop['queue_id']}/approve").status_code == 200
    det = client.get(f"/t/demo/api/prospects/{prop['prospect_id']}").json()
    assert det["prospect"]["stage"] == "proposal" and det["proposals"][0]["url"]


# --------------------------------------------------------------------------- extra coverage
def test_projection_changes_only_the_bottleneck_and_labels_assumptions(world):
    _import(world, "stu_a")
    _, pr = _proposal(world)
    proj = next(s for s in pr["sections"] if s["key"] == "projection")
    lines = proj["text"].split("\n")
    assert lines[0].startswith("- As now: about 3.0 sales a month") and "£2,700" in lines[0]
    assert lines[2].startswith("- At the benchmark:") and "(model)" in lines[2]
    assert any(a.startswith("Only enquiry to booked call changes") for a in proj["assumptions"])
    bn = next(s for s in pr["sections"] if s["key"] == "bottleneck")
    assert "enquiry to booked call: 12%" in bn["text"] and "30%" in bn["text"]
    # without their sales a month there is no projection rather than a guess
    _, pr2 = _proposal(world, name="Jonas Pell", numbers={"leads": 100, "booked": 10, "offer_price": 500})
    assert "projection" not in {s["key"] for s in pr2["sections"]}


def test_followup_edits_are_checked_against_research_too(world):
    _import(world, "stu_a")
    a = world.as_("stu_a")
    pid = _pid(world, "stu_a", "Mara Quill")
    d = a.post(f"{API}/prospects/{pid}/drafts", json={"move": "question"}).json()
    a.post(f"{API}/queue/{d['queue_id']}/approve")
    fu = next(x for x in a.get(f"{API}/prospects/{pid}").json()["drafts"] if x["touch"] == "followup")
    r = a.put(f"{API}/queue/{fu['id']}", json={"body": "Saw the Iron Mum Retreat sold 300 places. Want the checklist?"}).json()
    assert r["status"] == "held" and "unverified_personalisation" in {f["code"] for f in r["flags"]}


def test_email_signature_uses_sender_details(world):
    t = world.t
    a = world.as_("stu_a")
    a.put(f"{API}/prospects/icp", json={"sender": {"name": "Sam Sender", "business": "Sender Growth Co",
                                                    "postal_address": "12 High Street, Example City",
                                                    "reply_email": "sam@example.invalid"}})
    _import(world, "stu_a")
    pid = _pid(world, "stu_a", "Mara Quill")
    d = a.post(f"{API}/prospects/{pid}/drafts", json={"move": "resource", "channel": "email"}).json()
    body = queue.get(t, d["queue_id"])["body"]
    assert "Sam Sender\nSender Growth Co\nsam@example.invalid\n--\n12 High Street, Example City" in body
    assert d["status"] == "pending"
