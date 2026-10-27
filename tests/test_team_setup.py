"""Team & invites, workspace setup, first-run checklists, per-user memory and data rights.

Positive and negative paths: the approval gate for invite emails, one-time hashed tokens, rate limits, RBAC and
cross-user isolation, guardrails, AI off, empty workspace, malformed input, demo vs real tenant."""
from __future__ import annotations

import hashlib
import time

import pytest
from fastapi.testclient import TestClient

from operator_ai import audit, auth, db, llm, memory, queue, setup, team, tenancy
from tests.conftest import demo_login, login

O = "/t/owner/api"
D = "/t/demo/api"
STRONG = "Plum-Garden-Lamp-42"


def _invite(client, email="coach.one@example.com", role="coach", **kw):
    body = {"email": email, "role": role, "origin": "http://testserver", **kw}
    return client.post(f"{O}/team/invites", json=body)


def _token(res: dict) -> str:
    return res["link"].split("token=", 1)[1]


def _other(app) -> TestClient:
    return TestClient(app)


def _join(app, token, password=STRONG, tid="owner", display=None):
    c = _other(app)
    body = {"token": token, "password": password}
    if display:
        body["display"] = display
    return c, c.post(f"/t/{tid}/api/join/accept", json=body)


def _uid(t, username):
    with t.db() as c:
        return c.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()["id"]


# --------------------------------------------------------------------------- invites: approval gate + one-time hashed token
def test_invite_link_shown_once_only_hash_stored_and_email_queued_not_sent(owner, tenant):
    r = _invite(owner)
    assert r.status_code == 200, r.text
    res = r.json()
    tok = _token(res)
    assert res["shown_once"] and res["full_link"].startswith("http://testserver/t/owner/#/join?token=")
    assert "AI not used" in res["label"]
    with tenant.db() as c:
        inv = dict(c.execute("SELECT * FROM invites").fetchone())
        dumped = " ".join(str(v) for v in inv.values())
        assert inv["token_hash"] == hashlib.sha256(tok.encode()).hexdigest() and tok not in dumped
        assert inv["expires"] - inv["created"] == pytest.approx(7 * 86400, abs=5)
        # nothing has gone anywhere: the email is a pending queue item and no transport saw it
        assert c.execute("SELECT COUNT(*) FROM channel_messages").fetchone()[0] == 0
        audit_text = " ".join(r["detail"] for r in c.execute("SELECT detail FROM audit"))
        run = c.execute("SELECT steps, result FROM runs WHERE agent='team'").fetchone()
    item = queue.get(tenant, res["queue_item"]["id"])
    assert item["kind"] == "invite" and item["channel"] == "email" and item["status"] == "pending"
    assert item["recipient_ref"] == "coach.one@example.com" and item["flags"] == []
    assert tok in item["body"] and "Subject" not in item["body"] and item["meta"]["subject"]
    assert tok not in audit_text and "coach.one@example.com" not in audit_text  # audit keeps a masked address only
    assert tok not in run["steps"] and tok not in run["result"]  # the trace never holds the token
    assert res["run_id"]


def test_invite_email_guardrails_and_banned_name_holds_it(owner, tenant):
    owner.put(f"{O}/settings", json={"key": "banned_names", "value": ["Pat Sample"]})
    r = _invite(owner, "pat@example.com", "setter", display="Pat Sample").json()
    assert r["queue_item"]["status"] == "held"
    assert any(f["code"] == "banned_name" for f in r["queue_item"]["flags"])
    # the clean template itself passes every rule
    subject, body = team.draft_invite_email(tenant, name="Ava", role="student", inviter="Owner", link="http://x/t/owner/#/join?token=abc",
                                            expires=time.time() + 86400)
    assert team.queue.guardrails.check_for(tenant, f"Subject: {subject}\n{body}", {}) == []


def test_owner_approves_invite_email_mock_in_real_workspace_keeps_copy(owner, tenant):
    res = _invite(owner).json()
    qid = res["queue_item"]["id"]
    a = owner.post(f"{O}/queue/{qid}/approve")
    assert a.status_code == 200 and a.json()["status"] == "approved"
    # practice delivery reached nobody, so the owner keeps the link to send by hand
    assert _token(res) in queue.get(tenant, qid)["body"]


def test_demo_invite_scrubbed_after_send(client):
    demo_login(client, "owner")
    res = client.post(f"{D}/team/invites", json={"email": "lee.park@example.invalid", "role": "closer"}).json()
    qid = res["queue_item"]["id"]
    assert client.post(f"{D}/queue/{qid}/approve").json()["status"] == "approved"
    t = tenancy.get("demo")
    assert _token(res) not in queue.get(t, qid)["body"] and "[link removed]" in queue.get(t, qid)["body"]
    with t.db() as c:
        bodies = " ".join(r["body"] for r in c.execute("SELECT body FROM channel_messages"))
    assert _token(res) not in bodies


def test_accept_creates_account_signs_in_and_link_works_once(app, owner, tenant):
    res = _invite(owner, "closer.one@example.com", "closer", display="Casey Closer").json()
    tok = _token(res)
    c = _other(app)
    chk = c.post(f"{O}/join/check", json={"token": tok})
    assert chk.status_code == 200 and chk.json()["role"] == "closer" and chk.json()["email"] == "closer.one@example.com"
    weak = c.post(f"{O}/join/accept", json={"token": tok, "password": "password1"})
    assert weak.status_code == 422 and "10 characters" in weak.json()["detail"]
    assert c.post(f"{O}/join/accept", json={"token": tok, "password": "closer.one-Abc-123"}).status_code == 422  # contains email
    ok = c.post(f"{O}/join/accept", json={"token": tok, "password": STRONG})
    assert ok.status_code == 200 and ok.json()["user"]["role"] == "closer"
    me = c.get(f"{O}/me").json()
    assert me["user"]["display"] == "Casey Closer" and "sales" in me["tabs"] and "team" not in me["tabs"]
    again = _other(app).post(f"{O}/join/accept", json={"token": tok, "password": STRONG})
    assert again.status_code == 410
    # the unapproved email is withdrawn (rejected) and its link scrubbed
    item = queue.get(tenant, res["queue_item"]["id"])
    assert item["status"] == "rejected" and tok not in item["body"]
    # the new person signs in with their email
    login(_other(app), username="closer.one@example.com", password=STRONG)
    kinds = [a["kind"] for a in audit.recent(tenant, 50)]
    assert "team.join" in kinds and "team.invite" in kinds


def test_student_invite_links_existing_record_and_coach(app, owner, tenant):
    _, cr = None, _invite(owner, "coach.two@example.com", "coach").json()
    _, acc = _join(app, _token(cr))
    coach_id = acc.json()["user"]["id"]
    imp = owner.post(f"{O}/team/students/import", json={"text": "name,email\nAva Learner, ava.learner@example.com, physio clinics"})
    sid = imp.json()["created"][0]["student_id"]
    inv = owner.post(f"{O}/team/invites", json={"role": "student", "student_id": sid, "coach_id": coach_id,
                                                "origin": "http://testserver"})
    assert inv.status_code == 200 and inv.json()["invite"]["email"] == "ava.learner@example.com"
    c, r = _join(app, _token(inv.json()))
    assert r.status_code == 200
    u = r.json()["user"]
    assert u["student_id"] == sid and u["coach_id"] == coach_id
    with tenant.db() as cx:
        assert cx.execute("SELECT coach_id FROM students WHERE id=?", (sid,)).fetchone()[0] == coach_id
        assert cx.execute("SELECT COUNT(*) FROM students").fetchone()[0] == 1  # linked, not duplicated
    assert c.post(f"{O}/checkin", json={"outreach": 10}).status_code == 200
    # the same record can't be invited twice once linked
    assert owner.post(f"{O}/team/invites", json={"role": "student", "student_id": sid}).status_code == 409


def test_student_invite_without_record_creates_one(app, owner, tenant):
    res = _invite(owner, "new.student@example.com", "student", display="Noa Student").json()
    _, r = _join(app, _token(res))
    sid = r.json()["user"]["student_id"]
    with tenant.db() as c:
        s = dict(c.execute("SELECT * FROM students WHERE id=?", (sid,)).fetchone())
    assert s["email"] == "new.student@example.com" and s["name"] == "Noa Student" and s["demo"] == 0


def test_expired_and_cancelled_links(app, owner, tenant):
    a = _invite(owner, "late@example.com", "setter").json()
    with tenant.db() as c:
        c.execute("UPDATE invites SET expires=? WHERE id=?", (time.time() - 10, a["invite"]["id"]))
    assert _other(app).post(f"{O}/join/check", json={"token": _token(a)}).status_code == 410
    owner.get(f"{O}/team")  # lazy housekeeping retires the expired link's email
    assert queue.get(tenant, a["queue_item"]["id"])["status"] == "rejected"
    b = _invite(owner, "gone@example.com", "setter").json()
    rv = owner.post(f"{O}/team/invites/{b['invite']['id']}/revoke", json={})
    assert rv.status_code == 200 and rv.json()["state"] == "cancelled"
    item = queue.get(tenant, b["queue_item"]["id"])
    assert item["status"] == "rejected" and _token(b) not in item["body"]
    assert _other(app).post(f"{O}/join/accept", json={"token": _token(b), "password": STRONG}).status_code == 410
    # a new invite to the same address cancels the older open one
    c1 = _invite(owner, "twice@example.com", "setter").json()
    c2 = _invite(owner, "twice@example.com", "setter").json()
    assert _other(app).post(f"{O}/join/check", json={"token": _token(c1)}).status_code == 410
    assert _other(app).post(f"{O}/join/check", json={"token": _token(c2)}).status_code == 200


def test_accept_attempts_rate_limited(app, owner):
    good = _token(_invite(owner, "rl@example.com", "setter").json())
    c = _other(app)
    for i in range(team.JOIN_MAX_FAILS):
        r = c.post(f"{O}/join/accept", json={"token": f"not-a-real-token-{i:04d}xx", "password": STRONG})
        assert r.status_code == 404
    assert c.post(f"{O}/join/check", json={"token": good}).status_code == 429
    assert c.post(f"{O}/join/accept", json={"token": good, "password": STRONG}).status_code == 429


def test_token_from_one_workspace_is_useless_in_another(app, owner):
    tok = _token(_invite(owner, "x1@example.com", "setter").json())
    assert _other(app).post(f"{D}/join/check", json={"token": tok}).status_code == 404


def test_invite_validation_and_malformed_input(owner, tenant):
    assert _invite(owner, "not-an-email").status_code == 422
    assert _invite(owner, "a@example.com", "owner").status_code == 422
    assert owner.post(f"{O}/team/invites", content=b"{broken", headers={"content-type": "application/json"}).status_code == 422
    assert owner.post(f"{O}/team/invites", json={"email": "a@example.com", "role": "coach", "admin": True}).status_code == 422
    assert owner.post(f"{O}/team/invites", json={"role": "coach"}).status_code == 422  # no email, no record
    assert owner.post(f"{O}/team/invites", json={"role": "student", "student_id": 999}).status_code == 404
    assert owner.post(f"{O}/team/invites", json={"email": "a@example.com", "role": "setter", "origin": "javascript:alert(1)"}).status_code == 422
    owner.put(f"{O}/settings", json={"key": "blocked_attendees", "value": ["blocked.person@gmail.com"]})
    assert _invite(owner, "Blocked.Person+x@gmail.com", "setter").status_code == 409
    assert _invite(owner, "owner", "setter").status_code == 422
    for bad in ({"token": ""}, {"token": "x" * 400}, {}):
        assert owner.post(f"{O}/join/check", json=bad).status_code == 422
    assert owner.post(f"{O}/join/accept", json={"token": "abc"}).status_code == 422
    with tenant.db() as c:
        assert c.execute("SELECT COUNT(*) FROM invites").fetchone()[0] == 0


def test_existing_account_email_conflict(app, owner):
    r = _invite(owner, "dup@example.com", "setter").json()
    _join(app, _token(r))
    assert _invite(owner, "DUP@example.com", "closer").status_code == 409


# --------------------------------------------------------------------------- RBAC
@pytest.mark.parametrize("role", ["coach", "setter", "closer", "student"])
def test_team_and_setup_are_owner_only(client, role):
    demo_login(client, role)
    for path in ("/team", "/team/students", "/setup", "/setup/preflight", "/team/users/1/export"):
        assert client.get(f"{D}{path}").status_code == 403, path
    assert client.post(f"{D}/team/invites", json={"email": "x@example.invalid", "role": "setter"}).status_code == 403
    assert client.post(f"{D}/team/users/1/deactivate", json={}).status_code == 403
    assert client.post(f"{D}/setup/brand", json={"name": "Mine"}).status_code == 403
    # their own first-run checklist and memory work
    assert client.get(f"{D}/setup/first-run").status_code == 200
    assert client.get(f"{D}/memory").status_code == 200


def test_signed_out_gets_401(client):
    assert client.get(f"{O}/team").status_code == 401
    assert client.get(f"{O}/memory").status_code == 401
    assert client.get(f"{O}/setup/first-run").status_code == 401


# --------------------------------------------------------------------------- people management
def test_role_change_deactivate_kills_sessions_and_reactivate(app, owner, tenant):
    res = _invite(owner, "setter.one@example.com", "setter").json()
    member, r = _join(app, _token(res))
    uid = r.json()["user"]["id"]
    assert member.get(f"{O}/me").status_code == 200
    ch = owner.post(f"{O}/team/users/{uid}/role", json={"role": "closer"})
    assert ch.status_code == 200 and member.get(f"{O}/me").json()["user"]["role"] == "closer"
    off = owner.post(f"{O}/team/users/{uid}/deactivate", json={})
    assert off.status_code == 200 and off.json()["sessions_ended"] >= 1
    assert member.get(f"{O}/me").status_code == 401
    assert _other(app).post(f"{O}/auth/login", json={"username": "setter.one@example.com", "password": STRONG}).status_code == 401
    assert owner.post(f"{O}/team/users/{uid}/reactivate", json={}).status_code == 200
    login(_other(app), username="setter.one@example.com", password=STRONG)
    owner_id = _uid(tenant, "owner")
    assert owner.post(f"{O}/team/users/{owner_id}/deactivate", json={}).status_code == 409  # self
    assert owner.post(f"{O}/team/users/{owner_id}/role", json={"role": "coach"}).status_code == 409
    assert owner.post(f"{O}/team/users/9999/deactivate", json={}).status_code == 404
    assert owner.post(f"{O}/team/users/{uid}/role", json={"role": "owner"}).status_code == 422
    kinds = {a["kind"] for a in audit.recent(tenant, 100)}
    assert {"team.role", "team.deactivate", "team.activate"} <= kinds


def test_other_owner_accounts_cannot_be_managed_here(owner, tenant):
    oid = auth.create_user(tenant, "second-owner", "Another-Owner-Pass-1", "owner")
    assert owner.post(f"{O}/team/users/{oid}/deactivate", json={}).status_code == 403
    assert owner.post(f"{O}/team/users/{oid}/reset-link", json={}).status_code == 403
    assert owner.post(f"{O}/team/users/{oid}/delete", json={"confirm": "second-owner"}).status_code == 403


def test_reset_link_sets_new_password_and_ends_sessions(app, owner, tenant):
    res = _invite(owner, "coach.r@example.com", "coach").json()
    member, r = _join(app, _token(res))
    uid = r.json()["user"]["id"]
    rl = owner.post(f"{O}/team/users/{uid}/reset-link", json={"origin": "http://testserver"})
    assert rl.status_code == 200 and rl.json()["queue_item"]["id"]
    item = queue.get(tenant, rl.json()["queue_item"]["id"])
    assert item["kind"] == "password_reset" and item["status"] == "pending"
    tok = _token(rl.json())
    with tenant.db() as c:
        inv = c.execute("SELECT * FROM invites WHERE token_hash=?", (hashlib.sha256(tok.encode()).hexdigest(),)).fetchone()
    assert inv["kind"] == "reset" and inv["expires"] - inv["created"] == pytest.approx(48 * 3600, abs=5)
    chk = _other(app).post(f"{O}/join/check", json={"token": tok}).json()
    assert chk["kind"] == "reset" and chk["username"] == "coach.r@example.com"
    c2 = _other(app)
    assert c2.post(f"{O}/join/accept", json={"token": tok, "password": "Brand-New-Secret-77"}).status_code == 200
    assert member.get(f"{O}/me").status_code == 401  # old session ended
    assert _other(app).post(f"{O}/auth/login", json={"username": "coach.r@example.com", "password": STRONG}).status_code == 401
    login(_other(app), username="coach.r@example.com", password="Brand-New-Secret-77")
    owner.post(f"{O}/team/users/{uid}/deactivate", json={})
    assert owner.post(f"{O}/team/users/{uid}/reset-link", json={}).status_code == 409


def test_deactivating_revokes_open_reset_link(app, owner):
    res = _invite(owner, "s9@example.com", "setter").json()
    _, r = _join(app, _token(res))
    uid = r.json()["user"]["id"]
    tok = _token(owner.post(f"{O}/team/users/{uid}/reset-link", json={"send_email": False}).json())
    owner.post(f"{O}/team/users/{uid}/deactivate", json={})
    assert _other(app).post(f"{O}/join/accept", json={"token": tok, "password": "Brand-New-Secret-77"}).status_code == 410


def test_forgot_password_is_generic_recorded_and_throttled(app, owner, tenant):
    res = _invite(owner, "forgetful@example.com", "setter", display="Forgetful Setter").json()
    _join(app, _token(res))
    c = _other(app)
    a = c.post(f"{O}/join/forgot", json={"ident": "forgetful@example.com"}).json()
    b = c.post(f"{O}/join/forgot", json={"ident": "nobody@example.com"}).json()
    o = c.post(f"{O}/join/forgot", json={"ident": "owner"}).json()
    assert a == b == o  # never reveals whether an account exists
    reqs = owner.get(f"{O}/team").json()["reset_requests"]
    assert [r["display"] for r in reqs] == ["Forgetful Setter"]
    with tenant.db() as cx:
        assert cx.execute("SELECT COUNT(*) FROM queue WHERE kind='password_reset'").fetchone()[0] == 0  # nothing auto-sent
    for _ in range(team.FORGOT_MAX):
        c.post(f"{O}/join/forgot", json={"ident": "x@example.com"})
    assert c.post(f"{O}/join/forgot", json={"ident": "x@example.com"}).status_code == 429
    rid = reqs[0]["id"]
    assert owner.post(f"{O}/team/reset-requests/{rid}/dismiss", json={}).status_code == 200
    assert owner.post(f"{O}/team/reset-requests/{rid}/dismiss", json={}).status_code == 404


def test_assignments_and_coaches(app, owner, tenant):
    ids = {}
    for email, role in (("m.closer@example.com", "closer"), ("m.setter@example.com", "setter"), ("m.coach@example.com", "coach"),
                        ("m.student@example.com", "student")):
        _, r = _join(app, _token(_invite(owner, email, role).json()))
        ids[role] = r.json()["user"]
    assert owner.post(f"{O}/team/assignments", json={"manager_id": ids["closer"]["id"], "member_id": ids["setter"]["id"]}).status_code == 200
    assert owner.post(f"{O}/team/assignments", json={"manager_id": ids["coach"]["id"], "member_id": ids["student"]["id"]}).status_code == 422
    assert owner.post(f"{O}/team/assignments", json={"manager_id": ids["student"]["id"], "member_id": ids["setter"]["id"]}).status_code == 422
    assert owner.post(f"{O}/team/assignments", json={"manager_id": ids["setter"]["id"], "member_id": ids["setter"]["id"]}).status_code == 422
    assert owner.post(f"{O}/team/assignments", json={"manager_id": 0, "member_id": 1}).status_code == 422
    ov = owner.get(f"{O}/team").json()
    assert ov["assignments"][0]["manager_id"] == ids["closer"]["id"]
    setter = next(u for u in ov["users"] if u["id"] == ids["setter"]["id"])
    assert setter["managers"][0]["id"] == ids["closer"]["id"]
    assert owner.delete(f"{O}/team/assignments/{ids['closer']['id']}/{ids['setter']['id']}").status_code == 200
    assert owner.delete(f"{O}/team/assignments/{ids['closer']['id']}/{ids['setter']['id']}").status_code == 404
    sid = ids["student"]["student_id"]
    assert owner.post(f"{O}/team/students/{sid}/coach", json={"coach_id": ids["coach"]["id"]}).status_code == 200
    assert owner.post(f"{O}/team/students/{sid}/coach", json={"coach_id": ids["setter"]["id"]}).status_code == 422
    assert owner.post(f"{O}/team/students/9999/coach", json={"coach_id": None}).status_code == 404
    with tenant.db() as c:
        assert c.execute("SELECT coach_id FROM users WHERE id=?", (ids["student"]["id"],)).fetchone()[0] == ids["coach"]["id"]
    # changing the coach's role leaves a plain-English warning about their students
    w = owner.post(f"{O}/team/users/{ids['coach']['id']}/role", json={"role": "closer"}).json()
    assert "still assigned" in (w["warning"] or "")


def test_import_students_real_workspace(owner, tenant):
    text = ("Name,Email,Niche\nAva Learner, ava@example.com, physio clinics\nNoah Learner, noah@example.com\n"
            "Ava Again, AVA@example.com\nbroken line\nNo Email, not-an-email\n")
    r = owner.post(f"{O}/team/students/import", json={"text": text, "invite": True, "origin": "http://testserver"})
    assert r.status_code == 200, r.text
    res = r.json()
    assert len(res["created"]) == 2 and len(res["invites"]) == 2 and res["run_id"]
    reasons = {s["reason"] for s in res["skipped"]}
    assert {"Duplicate in this list", "Needs at least a name and an email", "Not a valid email"} <= reasons
    with tenant.db() as c:
        rows = db.rows(c.execute("SELECT name, demo FROM students"))
        assert c.execute("SELECT COUNT(*) FROM queue WHERE kind='invite' AND status='pending'").fetchone()[0] == 2
    assert all(r["demo"] == 0 and "(DEMO)" not in r["name"] for r in rows)
    again = owner.post(f"{O}/team/students/import", json={"text": "Ava Learner, ava@example.com"}).json()
    assert again["created"] == [] and again["skipped"][0]["reason"] == "Already a student"
    assert owner.post(f"{O}/team/students/import", json={"text": "   "}).status_code == 422
    found = owner.get(f"{O}/team/students", params={"q": "noah"}).json()
    assert found["shown"] == 1 and found["items"][0]["account"] == "invited"


def test_import_students_demo_is_labelled_and_fictional(client):
    demo_login(client, "owner")
    r = client.post(f"{D}/team/students/import", json={"text": "Kim Test, kim.test@example.invalid\nReal Person, real@gmail.com"}).json()
    assert r["created"][0]["name"] == "Kim Test (DEMO)"
    assert r["skipped"][0]["reason"].startswith("The pitch demo only")


# --------------------------------------------------------------------------- data rights
def test_export_user_data_no_secrets(app, owner, tenant):
    res = _invite(owner, "exp@example.com", "student", display="Export Me").json()
    member, r = _join(app, _token(res))
    uid = r.json()["user"]["id"]
    member.put(f"{O}/memory", json={"profile": {"offer": "Clinic growth", "price": 3000}})
    member.post(f"{O}/coach/ask", json={"question": "What's the USP formula?"})
    e = owner.get(f"{O}/team/users/{uid}/export")
    assert e.status_code == 200 and "attachment" in e.headers["content-disposition"]
    data = e.json()["data"]
    assert data["account"]["email"] == "exp@example.com" and "pw_hash" not in data["account"]
    assert data["user_memory"] and data["student_record"]["email"] == "exp@example.com"
    text = e.text
    assert "scrypt$" not in text and "token_hash" not in text
    assert owner.get(f"{O}/team/users/9999/export").status_code == 404
    assert "team.export" in {a["kind"] for a in audit.recent(tenant, 30)}


def test_delete_user_soft_deletes_and_scrubs(app, owner, tenant):
    res = _invite(owner, "del@example.com", "student", display="Delete Me").json()
    member, r = _join(app, _token(res))
    u = r.json()["user"]
    member.put(f"{O}/memory", json={"profile": {"niche": "dentists"}, "numbers": [{"label": "DMs", "value": "40"}]})
    member.post(f"{O}/checkin", json={"outreach": 5, "wins": "Booked a call with a named clinic"})
    member.post(f"{O}/coach/ask", json={"question": "How do I pick a niche?"})
    draft = queue.add(tenant, "prospect_dm", "Quick question about your clinic?", channel="instagram_dm",
                      meta={"prospect_facing": True, "owner_user_id": u["id"]})
    assert owner.post(f"{O}/team/users/{u['id']}/delete", json={"confirm": "wrong"}).status_code == 422
    d = owner.post(f"{O}/team/users/{u['id']}/delete", json={"confirm": "del@example.com"})
    assert d.status_code == 200, d.text
    out = d.json()
    assert out["drafts_rejected"] == 1 and out["removed"].get("user_memory") == 2 and out["kept"]
    assert member.get(f"{O}/me").status_code == 401
    assert _other(app).post(f"{O}/auth/login", json={"username": "del@example.com", "password": STRONG}).status_code == 401
    with tenant.db() as c:
        row = dict(c.execute("SELECT * FROM users WHERE id=?", (u["id"],)).fetchone())
        st = dict(c.execute("SELECT * FROM students WHERE id=?", (u["student_id"],)).fetchone())
        ck = dict(c.execute("SELECT * FROM checkins WHERE student_id=?", (u["student_id"],)).fetchone())
        n_threads = c.execute("SELECT COUNT(*) FROM threads WHERE user_id=?", (u["id"],)).fetchone()[0]
        n_mem = c.execute("SELECT COUNT(*) FROM user_memory WHERE user_id=?", (u["id"],)).fetchone()[0]
        inv = dict(c.execute("SELECT * FROM invites WHERE id=?", (res["invite"]["id"],)).fetchone())
    assert row["deleted"] and not row["active"] and row["email"] is None and row["display"] == "Deleted user"
    assert row["username"].startswith("deleted-") and st["name"] == "Deleted student" and st["email"] is None
    assert ck["wins"] == "" and ck["outreach"] == 5  # numbers kept for programme totals, notes gone
    assert n_threads == 0 and n_mem == 0 and inv["email"] is None
    assert queue.get(tenant, draft["id"])["status"] == "rejected"
    assert u["id"] not in {x["id"] for x in owner.get(f"{O}/team").json()["users"]}
    assert owner.post(f"{O}/team/users/{u['id']}/delete", json={"confirm": "x"}).status_code == 404
    assert "team.delete" in {a["kind"] for a in audit.recent(tenant, 50)}


# --------------------------------------------------------------------------- setup checklist
def test_setup_checklist_on_empty_real_workspace(owner, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret-value-xyz123")
    r = owner.get(f"{O}/setup")
    assert r.status_code == 200 and "sk-ant-secret" not in r.text
    data = r.json()
    items = {i["key"]: i for i in data["items"]}
    assert set(items) == {"brand", "material", "playbooks", "ai", "integrations", "live", "team", "students", "mission",
                          "academy", "persona", "backups", "preflight"}
    assert items["material"]["status"] == "warn" and "fictional" in items["material"]["detail"]  # A0
    assert items["material"]["data"]["fixture"] is True and items["material"]["data"]["words"] > 0
    assert items["brand"]["status"] == "todo" and items["team"]["status"] == "todo" and items["students"]["status"] == "todo"
    assert items["ai"]["required"] is False and "tenant_admin.py budget owner ai" in items["ai"]["next"]["command"]
    assert items["ai"]["data"]["key_present"] is True and items["ai"]["data"]["budget"] == 0
    assert all("live" in s or "practice" in s for s in items["live"]["data"]["sentences"])
    assert items["playbooks"]["status"] == "todo" and data["done"] < data["total"]
    for it in data["items"]:
        assert it["why"] and (it["next"] is None or it["next"].get("href") or it["next"].get("command"))
    assert "AI not used" in data["label"]


def test_setup_progresses_as_workspace_fills(app, owner):
    owner.put(f"{O}/settings", json={"key": "third_party_kb", "value": False})
    owner.post(f"{O}/setup/brand", json={"name": "Northstar Growth", "primary": "#112233", "logo": "https://cdn.example.com/l.png"})
    owner.post(f"{O}/team/students/import", json={"text": "Ava L, ava.l@example.com"})
    _invite(owner, "c@example.com", "coach")
    owner.post(f"{O}/setup/decide", json={"item": "persona", "value": "not_now"})
    owner.post(f"{O}/setup/decide", json={"item": "backups", "value": "configured_elsewhere"})
    items = {i["key"]: i for i in owner.get(f"{O}/setup").json()["items"]}
    for k in ("brand", "playbooks", "team", "students", "persona", "backups"):
        assert items[k]["status"] == "done", k
    assert items["playbooks"]["detail"] == "Decided: off"
    assert owner.get(f"{O}/public").json()["brand"]["name"] == "Northstar Growth"


def test_setup_brand_validation(owner, tenant):
    owner.put(f"{O}/settings", json={"key": "banned_names", "value": ["Pat Sample"]})
    for body in ({"primary": "red"}, {"name": ""}, {"name": "Pat Sample Academy"}, {"logo": "http://insecure.example.com/l.png"},
                 {"logo": "javascript:alert(1)"}, {"name": "Ok", "extra": 1}):
        assert owner.post(f"{O}/setup/brand", json=body).status_code == 422, body
    assert owner.post(f"{O}/setup/decide", json={"item": "persona", "value": "maybe"}).status_code == 422
    ok = owner.post(f"{O}/setup/brand", json={"name": "Brightline", "accent": "#00AA11"})
    assert ok.status_code == 200 and ok.json()["brand"]["accent"] == "#00aa11"
    assert any(a["kind"] == "settings" for a in audit.recent(tenant, 10))


def test_preflight_shared_between_screen_and_script(owner, tenant):
    from scripts import preflight_live

    rows = owner.get(f"{O}/setup/preflight").json()["rows"]
    assert [r["name"] for r in rows] == [name for name, _, _ in preflight_live.checks("owner")]
    assert [r["name"] for r in setup.preflight(tenant)] == [r["name"] for r in rows]


def test_speaker_label_detection():
    assert setup.has_speaker_labels(["Coach: hi there", "Student: hello", "Coach: so tell me", "Student: sure"])
    assert setup.has_speaker_labels(["[00:01] Rep: hi", "[00:02] Prospect: yes", "[00:03] Rep: great"])
    assert not setup.has_speaker_labels(["Alright, welcome to module one.", "Everything we build sits on the offer."])
    assert not setup.has_speaker_labels(["Coach: one", "Coach: two", "Coach: three"])  # one speaker is a monologue


# --------------------------------------------------------------------------- first-run
def test_first_run_owner_and_student(client, owner):
    fr = owner.get(f"{O}/setup/first-run").json()
    assert fr["role"] == "owner" and [i["key"] for i in fr["items"]] == ["material", "brand", "team", "students", "question"]
    assert fr["done"] == 0
    assert owner.get(f"{O}/setup/first-run", params={"role": "student"}).status_code == 403
    assert owner.get(f"{O}/setup/first-run", params={"role": "admin"}).status_code == 422
    assert owner.post(f"{O}/setup/first-run/team", json={"done": True}).status_code == 409  # ticks itself
    assert owner.post(f"{O}/setup/first-run/nope", json={"done": True}).status_code == 404
    assert owner.post(f"{O}/setup/first-run/BAD-KEY", json={"done": True}).status_code == 422
    _invite(owner)
    assert owner.get(f"{O}/setup/first-run").json()["done"] == 1
    hide = owner.post(f"{O}/setup/first-run/_dismissed", json={"done": True}).json()
    assert hide["dismissed"] is True

    demo_login(client, "student")
    st = client.get(f"{D}/setup/first-run").json()
    keys = [i["key"] for i in st["items"]]
    assert keys == ["meet_coach", "checkin", "lesson", "roleplay", "prospects", "outreach"]
    items = {i["key"]: i for i in st["items"]}
    assert items["meet_coach"]["done"] and items["checkin"]["done"] and "Coach" in items["meet_coach"]["detail"]
    assert client.post(f"{D}/setup/first-run/meet_coach", json={"done": False}).json()["items"][0]["done"] is False
    assert client.post(f"{D}/setup/first-run/meet_coach", json={"done": True}).json()["items"][0]["done"] is True


def test_first_run_progress_persists(client):
    demo_login(client, "setter")
    fr = client.get(f"{D}/setup/first-run").json()
    assert [i["key"] for i in fr["items"]] == ["memory", "lesson", "drill", "queue"]
    client.put(f"{D}/memory", json={"profile": {"goal": "Book 10 calls (DEMO)"}})
    assert {i["key"]: i["done"] for i in client.get(f"{D}/setup/first-run").json()["items"]}["memory"]
    client.delete(f"{D}/memory")
    assert {i["key"]: i["done"] for i in client.get(f"{D}/setup/first-run").json()["items"]}["memory"]  # never goes backwards


# --------------------------------------------------------------------------- memory
def test_memory_crud_export_delete_own_only(app, owner, tenant):
    a_res = _invite(owner, "stu.a@example.com", "student").json()
    b_res = _invite(owner, "stu.b@example.com", "student").json()
    a, ra = _join(app, _token(a_res))
    b, rb = _join(app, _token(b_res))
    body = {"profile": {"offer": "Patient acquisition", "niche": "physio clinics", "price": 2500, "currency": "GBP",
                        "goal": "First client"},
            "numbers": [{"label": "Openers last week", "value": "60"}, {"label": "openers LAST week", "value": "1"}],
            "decisions": [{"text": "Chose physio over dental"}]}
    p = a.put(f"{O}/memory", json=body)
    assert p.status_code == 200 and p.json()["profile"]["currency"] == "GBP" and len(p.json()["numbers"]) == 1
    assert b.get(f"{O}/memory").json()["empty"] is True  # B never sees A's memory
    assert b.get(f"{O}/memory/user/{ra.json()['user']['id']}").status_code == 404
    assert owner.get(f"{O}/memory/user/{ra.json()['user']['id']}").json()["profile"]["niche"] == "physio clinics"
    ex = a.get(f"{O}/memory/export")
    assert ex.status_code == 200 and "attachment" in ex.headers["content-disposition"]
    assert ex.json()["what_i_know_about_you"]["profile"]["offer"] == "Patient acquisition"
    assert memory.for_user(tenant, ra.json()["user"]["id"])["price"] == 2500
    assert a.delete(f"{O}/memory").json()["deleted"] >= 5
    assert a.get(f"{O}/memory").json()["empty"] is True
    kinds = {x["kind"] for x in audit.recent(tenant, 50)}
    assert {"memory.update", "memory.export", "memory.delete"} <= kinds
    audit_text = " ".join(x["detail"] for x in audit.recent(tenant, 50))
    assert "Patient acquisition" not in audit_text  # values never go in the audit log
    del rb


def test_coach_reads_only_own_students_memory(client, demo_tenant):
    with demo_tenant.db() as c:
        u = c.execute("SELECT id, student_id FROM users WHERE username='demo-student'").fetchone()
        sid, rec = u["id"], u["student_id"]
        coach = c.execute("SELECT id FROM users WHERE username='demo-coach'").fetchone()["id"]
        other = c.execute("SELECT id FROM users WHERE username='demo-coach2'").fetchone()["id"]
        c.execute("UPDATE students SET coach_id=? WHERE id=?", (coach, rec))
    demo_login(client, "coach")
    r = client.get(f"{D}/memory/user/{sid}")
    assert r.status_code == 200 and r.json()["read_only"] and "(DEMO)" in r.json()["profile"]["offer"]
    with demo_tenant.db() as c:
        c.execute("UPDATE students SET coach_id=? WHERE id=?", (other, rec))
    assert client.get(f"{D}/memory/user/{sid}").status_code == 404
    demo_login(client, "setter")
    assert client.get(f"{D}/memory/user/{sid}").status_code == 404


def test_memory_validation(owner):
    bad = [
        {"profile": {}, "extra": 1},
        {"numbers": []},
        {"profile": {"price": -5}},
        {"profile": {"currency": "dollars"}},
        {"profile": {"offer": "x" * 301}},
        {"profile": {}, "numbers": [{"label": "a", "value": "1"}] * 31},
        {"profile": {}, "numbers": [{"label": "", "value": "1"}]},
        {"profile": {}, "decisions": [{"text": ""}]},
        {"profile": {"price": "lots"}},
    ]
    for b in bad:
        assert owner.put(f"{O}/memory", json=b).status_code == 422, b
    assert owner.put(f"{O}/memory", content=b"{nope", headers={"content-type": "application/json"}).status_code == 422
    assert owner.get(f"{O}/memory").json()["empty"] is True


def test_memory_hint_in_router_and_suggestions(demo_tenant):
    from operator_ai.agents import router

    with demo_tenant.db() as c:
        u = dict(c.execute("SELECT * FROM users WHERE username='demo-student'").fetchone())
        owner_u = dict(c.execute("SELECT * FROM users WHERE username='demo-owner'").fetchone())
    user = auth.user_public(u)
    out = router.handle(demo_tenant, "800 DMs, 40 replies, 6 booked, 2 showed, 1 closed", user)
    hint = out["meta"]["remembered"]
    assert hint and any(f["label"] == "Your price" and f["value"] == "$2,500" for f in hint["facts"])
    q = router.handle(demo_tenant, "How do I pick a niche?", auth.user_public(owner_u))
    assert q["meta"]["remembered"] is None  # nothing remembered, nothing shown
    sugg = memory.suggestions(demo_tenant, user)
    assert any(s["source"] == "found:checkin" for s in sugg)
    assert memory.hint(demo_tenant, {"id": "not-a-number"}) is None  # never raises


# --------------------------------------------------------------------------- demo vs real, AI off
def test_demo_team_data_is_fictional_and_real_workspace_is_empty(client, demo_tenant):
    with demo_tenant.db() as c:
        users = db.rows(c.execute("SELECT display, email FROM users"))
        invites = db.rows(c.execute("SELECT display, email FROM invites"))
        mem = db.rows(c.execute("SELECT value FROM user_memory WHERE kind IN ('offer','niche','goal')"))
    assert all(u["display"].endswith("(DEMO)") for u in users)
    assert all((u["email"] or "x@example.invalid").endswith("@example.invalid") for u in users)
    assert invites and all(i["display"].endswith("(DEMO)") and i["email"].endswith("@example.invalid") for i in invites)
    assert mem and all("(DEMO)" in m["value"] for m in mem)
    demo_login(client, "owner")
    ov = client.get(f"{D}/team").json()
    assert ov["counts"]["switched_off"] == 1 and ov["reset_requests"] and ov["assignments"]
    real = tenancy.get("owner")
    with real.db() as c:
        for table in ("invites", "user_memory", "team_assignments", "team_reset_requests", "setup_progress"):
            assert c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0, table


def test_seed_demo_refuses_real_workspace(tenant):
    team.seed_demo(tenant)
    memory.seed_demo(tenant)
    with tenant.db() as c:
        assert c.execute("SELECT COUNT(*) FROM invites").fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM user_memory").fetchone()[0] == 0


def test_everything_runs_with_ai_off(app, owner, tenant):
    before = llm.REQUESTS_SENT
    assert llm.unavailable_reason(tenant)
    res = _invite(owner, "ai.off@example.com", "student").json()
    _join(app, _token(res))
    owner.get(f"{O}/setup")
    owner.get(f"{O}/setup/first-run")
    owner.put(f"{O}/memory", json={"profile": {"offer": "x"}})
    owner.get(f"{O}/memory")
    owner.post(f"{O}/team/students/import", json={"text": "A B, ab@example.com", "invite": True})
    assert llm.REQUESTS_SENT == before
    with tenant.db() as c:
        assert {r["ai_mode"] for r in c.execute("SELECT ai_mode FROM runs WHERE agent='team'")} == {"rules"}


def test_invite_email_is_owner_only_in_the_queue(app, owner):
    qid = _invite(owner, "q@example.com", "setter").json()["queue_item"]["id"]
    res = _invite(owner, "cc@example.com", "coach").json()
    coach, _ = _join(app, _token(res))
    ids = {i["id"] for i in coach.get(f"{O}/queue").json()["items"]}
    assert qid not in ids
    assert coach.post(f"{O}/queue/{qid}/approve").status_code == 403
    assert coach.get(f"{O}/queue/{qid}").status_code == 404


# --------------------------------------------------------------------------- browser: invite -> join -> signed in
@pytest.mark.ui
@pytest.mark.timeout(300)
def test_owner_invites_and_invitee_joins_in_the_browser(tmp_path):
    from playwright.sync_api import expect, sync_playwright

    from tests.server import running_server

    with running_server(tmp_path) as base, sync_playwright() as p:
        browser = p.chromium.launch()
        errors: list[str] = []
        page = browser.new_context(viewport={"width": 1440, "height": 900}).new_page()
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(f"{base}/t/demo/", wait_until="networkidle")
        page.get_by_test_id("pitch-demo").get_by_role("button", name="owner").click()
        page.get_by_test_id("ai-pill").wait_for(timeout=15000)
        page.evaluate("location.hash = '#/team'")
        page.get_by_role("button", name="Invite someone").click()
        page.get_by_label("Email", exact=True).fill("jo.newhire@example.invalid")
        page.get_by_label("Name (optional)").fill("Jo Newhire")
        page.get_by_label("Role").select_option("closer")
        page.get_by_role("button", name="Create invite").click()
        box = page.get_by_test_id("link-once")
        expect(box).to_contain_text("One-time link, shown only now", timeout=15000)
        expect(box).to_contain_text("waiting for your approval")
        link = box.locator("code").inner_text()
        assert "/t/demo/#/join?token=" in link
        page.get_by_role("button", name="Done").click()
        page.get_by_role("tab", name="Students").click()
        expect(page.locator("main")).to_contain_text("Ava Hartley (DEMO)", timeout=15000)
        page.get_by_role("tab", name="People").click()
        page.get_by_role("row", name="Setter Imani").get_by_role("button", name="Manage").click()
        expect(page.get_by_text("Export everything (JSON)")).to_be_visible()
        expect(page.get_by_text("Managed by", exact=True)).to_be_visible()
        page.keyboard.press("Escape")
        page.evaluate("location.hash = '#/setup'")
        page.get_by_role("button", name="Edit brand").click()
        page.get_by_label("Programme name").fill("Brightline (DEMO)")
        page.get_by_role("button", name="Save brand").click()
        expect(page.get_by_text("Brand saved")).to_be_visible(timeout=10000)
        assert page.locator("main h1").count() == 1

        jp = browser.new_context(viewport={"width": 390, "height": 844}).new_page()
        jp.on("pageerror", lambda e: errors.append(str(e)))
        jp.goto(link, wait_until="networkidle")
        expect(jp.get_by_role("heading", level=1)).to_contain_text("Join", timeout=15000)
        assert "token=" not in jp.url  # removed from the address bar straight away
        jp.get_by_label("New password", exact=True).fill("Plum-Garden-Lamp-42")
        jp.get_by_label("Type it again").fill("Plum-Garden-Lamp-42")
        jp.get_by_role("button", name="Create my account").click()
        expect(jp.get_by_test_id("whoami")).to_contain_text("Closer", timeout=15000)
        assert jp.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth") <= 2
        # the same link is dead now
        again = browser.new_context().new_page()
        again.goto(link, wait_until="networkidle")
        expect(again.get_by_role("heading", level=1)).to_contain_text("This link doesn't work", timeout=15000)
        assert errors == []
        browser.close()
