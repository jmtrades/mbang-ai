import hashlib
import hmac
import io
import json
import os
import stat
import time

import pytest

from operator_ai import (call_review, channels, console, crm, index, mcp_server, queue, skills, threads, uploads,
                         vault)
from tests.conftest import demo_login


# ---------------------------------------------------------------- threads + projects + memory
def test_threads_title_rename_search_archive_share(client):
    demo_login(client, "student")
    r = client.post("/t/demo/api/coach/ask", json={"question": "What goes in a pre-call brief?"}).json()
    tid = r["thread_id"]
    th = client.get(f"/t/demo/api/threads/{tid}").json()
    assert th["title"].startswith("What goes in a pre-call brief") and len(th["messages"]) == 2
    assert client.patch(f"/t/demo/api/threads/{tid}", json={"title": "Brief notes"}).status_code == 200
    assert any(t["id"] == tid for t in client.get("/t/demo/api/threads", params={"q": "pre-call"}).json()["items"])
    client.patch(f"/t/demo/api/threads/{tid}", json={"archived": True})
    assert not any(t["id"] == tid for t in client.get("/t/demo/api/threads").json()["items"])
    tok = client.post(f"/t/demo/api/threads/{tid}/share").json()["token"]
    demo_login(client, "coach")
    assert client.get(f"/t/demo/api/threads/shared/{tok}").json()["id"] == tid  # internal link works for members
    assert client.get(f"/t/demo/api/threads/{tid}").status_code == 404  # but not someone else's thread directly


def test_projects_scoped_user_deletable_memory(demo_tenant):
    pid = threads.create_project(demo_tenant, 1, "Launch (DEMO)")
    mid = threads.add_memory(demo_tenant, 1, pid, "Prefers short DMs")
    assert [m["content"] for m in threads.memories(demo_tenant, 1, pid)] == ["Prefers short DMs"]
    assert threads.memories(demo_tenant, 1, None) == []  # scoped to the project
    with pytest.raises(PermissionError):
        threads.add_memory(demo_tenant, 2, pid, "not yours")
    assert not threads.delete_memory(demo_tenant, 2, mid)  # another user can't delete it
    assert threads.delete_memory(demo_tenant, 1, mid)


# ---------------------------------------------------------------- uploads + OCR + receipt
@pytest.mark.parametrize("name,data,needle", [
    ("notes.txt", b"Thunder Dome notes", "Thunder Dome"),
    ("notes.md", b"# Title\nbody", "Title"),
    ("data.csv", b"a,b\n1,2\n", "1, 2"),
    ("data.json", b'{"k": "v"}', '"k"'),
    ("cap.srt", b"1\n00:00:01,000 --> 00:00:02,000\nHello there\n\n2\n00:00:02,000 --> 00:00:03,000\nHello there\n", "Hello there"),
    ("cap.vtt", b"WEBVTT\n\n00:00.000 --> 00:01.000\nFirst line\n", "First line"),
    ("page.html", b"<html><script>x()</script><p>Visible <b>text</b></p></html>", "Visible text"),
])
def test_upload_kinds_and_receipt(name, data, needle):
    ex = uploads.extract(name, data)
    assert needle in ex["text"]
    rec = uploads.receipt(name, len(data), ex)
    assert rec["summary"].startswith("I read ") and rec["bytes"] == len(data)
    if name.endswith(".srt"):
        assert ex["text"].count("Hello there") == 1 and "-->" not in ex["text"]
    if name.endswith(".html"):
        assert "x()" not in ex["text"]


def test_upload_pdf_and_docx():
    import docx
    from pypdf import PdfWriter

    d = docx.Document()
    d.add_paragraph("Reminder ladder: 24h, 2h, 10 min")
    b = io.BytesIO()
    d.save(b)
    assert "Reminder ladder" in uploads.extract("x.docx", b.getvalue())["text"]
    w = PdfWriter()
    w.add_blank_page(200, 200)
    pb = io.BytesIO()
    w.write(pb)
    ex = uploads.extract("blank.pdf", pb.getvalue())
    assert ex["pages"] == 1 and ex["warnings"]  # no text layer is reported honestly


def test_upload_rejects_unsupported_and_huge():
    with pytest.raises(uploads.UploadError):
        uploads.extract("x.exe", b"MZ")
    with pytest.raises(uploads.UploadError):
        uploads.extract("x.txt", b"a" * (uploads.MAX_BYTES + 1))


def test_image_ocr_local(monkeypatch):
    if uploads.ocr_backend() is None:
        ex = uploads.extract("x.png", b"\x89PNG")
        assert ex["warnings"] and "no local OCR" in ex["warnings"][0]
        return
    from PIL import Image, ImageDraw, ImageFont

    img = Image.new("RGB", (900, 200), "white")
    font = None
    for f in ("/System/Library/Fonts/Supplemental/Arial.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"):
        if os.path.exists(f):
            font = ImageFont.truetype(f, 48)
    ImageDraw.Draw(img).text((20, 60), "Thunder Dome kill rule", fill="black", font=font)
    b = io.BytesIO()
    img.save(b, "PNG")
    ex = uploads.extract("shot.png", b.getvalue())
    assert "Thunder Dome" in ex["text"] and ex["method"].startswith("local OCR")


def test_upload_api(client):
    demo_login(client, "student")
    r = client.post("/t/demo/api/uploads", files={"file": ("plan.txt", b"My 30 day plan", "text/plain")})
    assert r.status_code == 200 and r.json()["words"] == 4
    assert client.get("/t/demo/api/uploads").json()["items"][0]["filename"] == "plan.txt"


def test_owner_upload_to_own_kb_is_searchable(env):
    from operator_ai import retrieval, tenancy

    tenancy.ensure_defaults()
    t, _ = tenancy.create("acme", "Acme Coaching", kb_scope="own")
    assert retrieval.search(t, "zygomorphic referral ladder") == []
    uploads.save(t, 1, "sop.txt", b"Our zygomorphic referral ladder has three rungs.", to_kb=True)
    hits = retrieval.search(t, "zygomorphic referral ladder")
    assert hits and hits[0]["file"] == "kb/sop.txt"


# ---------------------------------------------------------------- business profile + skills + workers
def test_profile_8_sections_read_by_skills(tenant):
    skills.put_profile(tenant, {"audience": {"who": "dental practice owners", "pain": "empty chairs"},
                                "offer": {"outcome": "15 new patients a month", "timeframe": "60 days",
                                          "mechanism": "recall system"}}, "owner")
    p = skills.get_profile(tenant)
    assert set(p) == set(skills.PROFILE_SECTIONS) and len(p) == 8
    r = skills.run_skill(tenant, "usp_builder")
    assert "dental practice owners" in r["draft"] and "recall system" in r["draft"]
    assert queue.get(tenant, r["queue_id"])["kind"] == "usp"


def test_skills_library_size():
    assert len(skills.SKILLS) >= 11


def test_worker_names_missing_login(tenant):
    wid = skills.add_worker(tenant, "Daily post", "instagram_post", "daily")
    r = skills.run_worker(tenant, wid)
    assert r["status"] == "needs_login" and r["missing"] == ["instagram_access_token"]
    vault.put(tenant, {"instagram_access_token": "EAAtest1234567890abc"})
    r2 = skills.run_worker(tenant, wid)
    assert r2["status"] == "drafted" and queue.get(tenant, r2["queue_id"])["status"] in ("pending", "needs_human")


def test_daily_trigger_runs_due_workers(tenant):
    wid = skills.add_worker(tenant, "Nudge", "checkin_nudge", "daily", schedule="00:00")
    n = skills.run_due_workers(time.time())
    assert n >= 1
    assert skills.run_due_workers(time.time()) == 0  # not due again the same day
    assert skills.list_workers(tenant)[0]["last_status"].startswith("drafted")


def test_on_source_change_trigger_drafts_only(tenant):
    skills.add_worker(tenant, "On change", "content_week", "on_source_change")
    assert skills.on_source_change(tenant, "drive:new.txt") == 1
    with tenant.db() as c:
        assert c.execute("SELECT COUNT(*) FROM channel_messages").fetchone()[0] == 0


# ---------------------------------------------------------------- call review (consent first) + drills
TRANSCRIPT = """Rep: Thanks for jumping on. What's your goal for the next 90 days?
Prospect: More patients. Right now we get about ten a month.
Rep: What have you tried so far?
Prospect: Discounts mostly.
Rep: So the gap is between ten and the thirty you want.
Prospect: Honestly it's too expensive for us right now and I need to think about it.
Rep: I can do a discount if you sign today."""


def test_call_review_requires_every_party_consent(tenant):
    cid = call_review.create_call(tenant, "Discovery (DEMO)", "Abel", ["Abel", "Prospect"], TRANSCRIPT)
    with pytest.raises(call_review.ConsentMissing):
        call_review.review(tenant, cid)
    call_review.record_consent(tenant, cid, "Abel", "verbal", "owner")
    with pytest.raises(call_review.ConsentMissing):
        call_review.review(tenant, cid)
    call_review.record_consent(tenant, cid, "Prospect", "verbal on recording", "owner")
    r = call_review.review(tenant, cid)
    assert {s["key"] for s in r["stages"]} == {"rapport", "discovery", "gap", "offer", "next_step"}
    assert all(s["citation"] for s in r["stages"])
    assert r["biggest_leak"]["score"] == min(s["score"] for s in r["stages"])
    assert {o["key"] for o in r["objections"]} == {"price", "think"} and r["discounted"]
    assert r["drills"] and call_review.rep_trends(tenant)[0]["rep"] == "Abel"


def test_drills_spaced_repetition(tenant):
    cards = call_review.due_cards(tenant, 1)
    assert len(cards) >= 3 and all(c["is_due"] for c in cards)
    g = call_review.grade(tenant, 1, cards[0]["card_id"], 5)
    assert g["interval_days"] == 1.0 and g["reps"] == 1
    g = call_review.grade(tenant, 1, cards[0]["card_id"], 4)
    assert g["interval_days"] == 6.0
    g = call_review.grade(tenant, 1, cards[0]["card_id"], 1)
    assert g["interval_days"] == 1.0 and g["reps"] == 0
    assert call_review.sm2(2.5, 6, 2, 5)[1] == 15.0


# ---------------------------------------------------------------- voice-note deck + web voice
def test_voice_note_to_cited_deck(tenant):
    d = channels.voice_note_deck(tenant, "Today I want to cover the Thunder Dome. Three to five creatives. Kill the losers. "
                                         "Then reminders before calls. Twenty four hours. Two hours.")
    assert d["slides"] and all(s["citation"] for s in d["slides"])


def test_web_voice_consent_quota_transcript_recap(demo_tenant):
    with pytest.raises(channels.ChannelError):
        channels.voice_start(demo_tenant, 1, False)
    s = channels.voice_start(demo_tenant, 1, True)
    channels.voice_chunk(demo_tenant, 1, s["session_id"], "What are the rules for a Thunder Dome test?", 30)
    demo_tenant.set("voice_minutes_quota", 1)
    with pytest.raises(channels.ChannelError) as ei:
        channels.voice_chunk(demo_tenant, 1, s["session_id"], "more", 60)
    assert ei.value.status == 429
    out = channels.voice_end(demo_tenant, 1, s["session_id"])
    assert out["recap"]["questions"] == 1 and out["recap"]["items"][0]["covered"] == "yes"


# ---------------------------------------------------------------- telegram / slack / sms / email
def test_telegram_link_ask_checkin_group(client):
    from operator_ai import tenancy

    demo_login(client, "student")
    code = client.post("/t/demo/api/channels/link-code").json()["code"]
    t = tenancy.get("demo")
    secret = vault.ensure_practice_secret(t, "telegram_webhook_secret")
    H = {"x-telegram-bot-api-secret-token": secret}
    send = lambda text, chat_type="private": client.post("/t/demo/hooks/telegram", headers=H, json={
        "message": {"chat": {"id": 4242, "type": chat_type}, "text": text}})
    assert client.post("/t/demo/hooks/telegram", headers={"x-telegram-bot-api-secret-token": "wrong"},
                       json={}).status_code == 401
    assert send(f"/link {code}").json()["linked"] is True
    r = send("/ask What are the rules for a Thunder Dome test?").json()
    item = queue.get(t, r["queued"])
    assert item["kind"] == "channel_reply" and item["channel"] == "telegram" and item["status"] == "pending"
    assert send("/checkin 40 3 2 4").json()["checkin"] is True
    assert send("hello everyone", "group").json()["ignored"].startswith("group message")
    assert queue.approve(t, item["id"], "owner(owner)")["delivered"] == "mock:telegram"


def test_slack_signature(demo_tenant, client):
    secret = vault.ensure_practice_secret(demo_tenant, "slack_signing_secret")
    body = json.dumps({"type": "event_callback", "event": {"type": "app_mention", "text": "<@U1> what's the USP formula?",
                                                           "channel": "C1", "user": "U2"}}).encode()
    ts = str(int(time.time()))
    good = {"x-slack-request-timestamp": ts, "x-slack-signature": channels.slack_sign(secret, ts, body),
            "content-type": "application/json"}
    r = client.post("/t/demo/hooks/slack", content=body, headers=good)
    assert r.status_code == 200 and queue.get(demo_tenant, r.json()["queued"])["channel"] == "slack"
    bad = {**good, "x-slack-signature": "v0=deadbeef"}
    assert client.post("/t/demo/hooks/slack", content=body, headers=bad).status_code == 401
    old = {**good, "x-slack-request-timestamp": str(int(time.time()) - 3600)}
    assert client.post("/t/demo/hooks/slack", content=body, headers=old).status_code == 401
    ch = json.dumps({"type": "url_verification", "challenge": "abc"}).encode()
    ok = {"x-slack-request-timestamp": ts, "x-slack-signature": channels.slack_sign(secret, ts, ch)}
    assert client.post("/t/demo/hooks/slack", content=ch, headers=ok).json() == {"challenge": "abc"}


def _signed(secret, payload):
    body = json.dumps(payload).encode()
    return body, {"x-signature": channels.sign_body(secret, body), "content-type": "application/json"}


def test_sms_opt_in_first_and_fictional_numbers(demo_tenant, client):
    secret = vault.ensure_practice_secret(demo_tenant, "sms_webhook_secret")
    body, h = _signed(secret, {"from": "+1-555-0142", "body": "What's the USP formula?"})
    assert client.post("/t/demo/hooks/sms", content=body, headers=h).json()["ignored"].startswith("not opted in")
    body, h = _signed(secret, {"from": "+1-555-0142", "body": "START"})
    assert client.post("/t/demo/hooks/sms", content=body, headers=h).json()["opted_in"] is True
    body, h = _signed(secret, {"from": "+1-555-0142", "body": "What's the USP formula?"})
    q = client.post("/t/demo/hooks/sms", content=body, headers=h).json()["queued"]
    # an unknown number never gets course material: a neutral holding reply for a person (inbound.route)
    item = queue.get(demo_tenant, q)
    assert item["status"] == "needs_human" and item["meta"]["holding"] and "Sources:" not in item["body"]
    body, h = _signed(secret, {"from": "+44 7700 900123", "body": "hi"})
    assert client.post("/t/demo/hooks/sms", content=body, headers=h).status_code == 422  # not a fictional number
    # outbound SMS to someone who never opted in is held
    held = queue.add(demo_tenant, "reminder", "See you soon", channel="sms", recipient_ref="+1-555-0199")
    assert held["status"] == "held" and held["flags"][0]["code"] == "not_opted_in"
    body, h = _signed(secret, {"from": "+1-555-0142", "body": "STOP"})
    client.post("/t/demo/hooks/sms", content=body, headers=h)
    assert not channels.opted_in(demo_tenant, "sms", "+1-555-0142")


def test_email_inbound_parse_quote_strip_drafts_only(demo_tenant, client):
    secret = vault.ensure_practice_secret(demo_tenant, "email_webhook_secret")
    with demo_tenant.db() as c:  # a known student keeps the cited Coach (unknown senders get a holding reply)
        c.execute("INSERT INTO students(name,email,demo) VALUES('Lee (DEMO)','lee@example.invalid',1)")
    raw = (b"From: Lee <lee@example.invalid>\nSubject: Show rates\nContent-Type: text/plain\n\n"
           b"What show rate should I aim for on booked calls?\n\nOn Mon, someone wrote:\n> old quoted text\n")
    r = client.post("/t/demo/hooks/email", content=raw, headers={"x-signature": channels.sign_body(secret, raw)})
    d = r.json()
    assert "old quoted text" not in d["parsed"]["body"] and d["parsed"]["subject"] == "Show rates"
    item = queue.get(demo_tenant, d["queued"])
    assert item["kind"] == "email_reply" and item["meta"]["subject"] == "Re: Show rates" and item["status"] == "pending"
    assert client.post("/t/demo/hooks/email", content=raw, headers={"x-signature": "bad"}).status_code == 401


# ---------------------------------------------------------------- MCP server
def test_mcp_read_only_token_auth(owner):
    from operator_ai import tenancy

    t = tenancy.get("owner")
    tok = mcp_server.mint(t, "laptop", "owner")
    H = {"authorization": f"Bearer {tok}"}
    rpc = lambda m, p=None, i=1: owner.post("/mcp", headers=H, json={"jsonrpc": "2.0", "id": i, "method": m, "params": p or {}})
    assert rpc("initialize").json()["result"]["serverInfo"]["name"] == "operator-ai"
    names = {x["name"] for x in rpc("tools/list").json()["result"]["tools"]}
    assert names == {"ask_coach", "search_library", "list_skills", "run_skill", "get_business_profile"}
    out = json.loads(rpc("tools/call", {"name": "ask_coach", "arguments": {"question": "What are the rules for a Thunder Dome test?"}})
                     .json()["result"]["content"][0]["text"])
    assert out["covered"] == "yes" and out["citations"]
    before = queue.counts(t)
    out = json.loads(rpc("tools/call", {"name": "run_skill", "arguments": {"skill": "dm_opener"}}).json()["result"]["content"][0]["text"])
    assert out["queued"] is False and queue.counts(t) == before  # read-only
    assert rpc("nope").json()["error"]["code"] == -32601
    assert owner.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).status_code == 401
    assert owner.post("/mcp", headers={"authorization": "Bearer opmcp.owner.wrong"}, json={}).status_code == 401
    with t.db() as c:
        token_id = c.execute("SELECT id FROM mcp_tokens").fetchone()[0]
    mcp_server.revoke(t, token_id, "owner")
    assert rpc("tools/list").status_code == 401


# ---------------------------------------------------------------- CRM
def test_crm_terminal_moves_need_approval(demo_tenant):
    cid = crm.add_contact(demo_tenant, "Pat Lane (DEMO)", "pat@example.invalid")
    assert crm.move(demo_tenant, cid, "booked", "owner")["status"] == "moved"
    r = crm.move(demo_tenant, cid, "closed_won", "setter")
    assert r["status"] == "needs_approval"
    with demo_tenant.db() as c:
        assert c.execute("SELECT stage FROM crm_contacts WHERE id=?", (cid,)).fetchone()[0] == "booked"
    with pytest.raises(queue.QueueError):
        queue.approve(demo_tenant, r["queue_id"], "setter", role="setter")
    queue.approve(demo_tenant, r["queue_id"], "owner(owner)")
    with demo_tenant.db() as c:
        assert c.execute("SELECT stage FROM crm_contacts WHERE id=?", (cid,)).fetchone()[0] == "closed_won"
    from operator_ai import actions

    actions.rollback(demo_tenant, r["action_id"], "owner")
    with demo_tenant.db() as c:
        assert c.execute("SELECT stage FROM crm_contacts WHERE id=?", (cid,)).fetchone()[0] == "booked"


# ---------------------------------------------------------------- integrations + vault
def test_vault_aes_gcm_tenant_bound(env):
    from operator_ai import tenancy

    tenancy.ensure_defaults()
    a, _ = tenancy.create("ten-a", "A")
    b, _ = tenancy.create("ten-b", "B")
    vault.put(a, {"meta_access_token": "EAAsecretvalue123456"})
    raw1 = (a.dir / "vault.bin").read_bytes()
    vault.put(a, {"meta_app_id": "123"})
    raw2 = (a.dir / "vault.bin").read_bytes()
    assert raw1[4:16] != raw2[4:16]  # fresh nonce every write
    assert b"EAAsecret" not in raw2
    assert stat.S_IMODE(os.stat(a.dir / "vault.key").st_mode) == 0o600
    assert vault.get(a, "meta_access_token") == "EAAsecretvalue123456"
    # tenant id is the associated data: B's id can't open A's blob even with A's key
    (b.dir / "vault.bin").write_bytes(raw2)
    (b.dir / "vault.key").write_bytes((a.dir / "vault.key").read_bytes())
    with pytest.raises(vault.VaultError):
        vault.get(b, "meta_app_id")


def test_vault_api_set_not_set_only(owner):
    r = owner.put("/t/owner/api/vault", json={"fields": {"slack_bot_token": "xoxb-should-never-come-back"}})
    assert r.status_code == 200 and r.json()["status"] == {"slack_bot_token": "set"}
    blob = json.dumps(owner.get("/t/owner/api/vault").json()) + json.dumps(owner.get("/t/owner/api/integrations").json())
    assert "xoxb" not in blob and '"slack_bot_token": "set"' in blob
    assert owner.put("/t/owner/api/vault", json={"fields": {"evil": "x"}}).status_code == 422
    from operator_ai import audit, tenancy

    a = audit.recent(tenancy.get("owner"), 5, "vault")[0]
    assert "slack_bot_token" in a["detail"] and "xoxb" not in a["detail"]


def test_integrations_list(tenant):
    items = console.integrations(tenant)
    assert {i["key"] for i in items} >= {"meta", "google_calendar", "telegram", "slack", "sms", "email", "payments", "crm"}
    assert all(i["status"] in ("REAL_OK", "NEEDS_LOGIN", "NEEDS_AI_BUDGET") for i in items)


# ---------------------------------------------------------------- members + payments
def test_signed_payment_webhook_grants_and_revokes_access(demo_tenant, client):
    secret = vault.ensure_practice_secret(demo_tenant, console.PAYMENT_SECRET)
    ev = {"id": "evt_1", "type": "payment.succeeded", "data": {"email": "newmember@example.invalid", "plan": "core"}}
    body = json.dumps(ev).encode()
    h = {"x-signature": console.sign_payment(secret, body)}
    r = client.post("/t/demo/hooks/payments", content=body, headers=h).json()
    assert r["access"] is True and r["invite"]
    assert client.post("/t/demo/hooks/payments", content=body, headers=h).json()["duplicate"] is True
    assert client.post("/t/demo/hooks/payments", content=body, headers={"x-signature": "0" * 64}).status_code == 401
    ev2 = {"id": "evt_2", "type": "payment.refunded", "data": {"email": "newmember@example.invalid"}}
    b2 = json.dumps(ev2).encode()
    assert client.post("/t/demo/hooks/payments", content=b2, headers={"x-signature": console.sign_payment(secret, b2)}).json()[
        "access"] is False
    with demo_tenant.db() as c:
        assert c.execute("SELECT active FROM users WHERE username='newmember@example.invalid'").fetchone()[0] == 0
    big = b"x" * (256 * 1024 + 1)
    assert client.post("/t/demo/hooks/payments", content=big, headers={"x-signature": "x"}).status_code == 413


# ---------------------------------------------------------------- minis + embed
def test_mini_scope_limit_consent_embed(demo_tenant, client):
    console.create_mini(demo_tenant, "ads-helper", "Ads helper", ["sops/thunderdome.txt"], 2, "I agree (DEMO)")
    assert client.get("/t/x/api/public").status_code == 404
    page = client.get("/m/demo/ads-helper")
    assert page.status_code == 200 and "Ads helper" in page.text
    assert client.post("/m/demo/ads-helper/start", json={"email": "a@example.invalid", "consent": False}).status_code == 422
    tok = client.post("/m/demo/ads-helper/start", json={"email": "a@example.invalid", "consent": True}).json()["token"]
    r = client.post("/m/demo/ads-helper/ask", json={"token": tok, "question": "What are the rules for a Thunder Dome test?"}).json()
    assert r["covered"] == "yes" and all(c["file"] == "sops/thunderdome.txt" for c in r["citations"])
    r2 = client.post("/m/demo/ads-helper/ask", json={"token": tok, "question": "What's the USP formula?"}).json()
    assert r2["covered"] == "no"  # out of the mini's scope
    assert client.post("/m/demo/ads-helper/ask", json={"token": tok, "question": "x?"}).status_code in (422, 429)
    assert client.post("/m/demo/ads-helper/ask", json={"token": tok, "question": "Another one?"}).status_code == 429
    js = client.get("/embed.js")
    assert js.status_code == 200 and "iframe" in js.text


# ---------------------------------------------------------------- Drive inbox sync
def test_drive_list_diff_fetch_screen_stage(owner):
    from operator_ai import retrieval, tenancy

    t = tenancy.get("owner")
    inbox = console.drive_inbox(t)
    (inbox / "new_sop.txt").write_text("The quokka follow-up cadence is two, four and seven days.\n"
                                       "Plant questions in the chat from a second account.\n")
    lst = console.drive_list(t)
    assert lst["files"][0]["state"] == "new"
    staged = console.drive_fetch_and_stage(t, "owner")
    assert staged[0]["status"] == "staged" and staged[0]["flags"][0]["label"].startswith("Not used: this passage describes planting")
    assert console.drive_list(t)["files"][0]["state"] == "unchanged"
    assert not retrieval.search(t, "quokka cadence") or retrieval.search(t, "quokka cadence")[0]["file"] != "kb/new_sop.txt"
    sid = console.drive_staged(t)[0]["id"]
    console.drive_decide(t, sid, True, "owner")
    hits = retrieval.search(t, "quokka follow-up cadence")
    assert hits[0]["file"] == "kb/new_sop.txt"
    assert not any("second account" in h["text"] for h in hits)  # still screened at index time


# ---------------------------------------------------------------- owner console
def test_kb_admin_labels_priority_notes(owner):
    r = owner.put("/t/owner/api/kb", json={"file": "sops/thunderdome.txt", "provenance": "owner_confirmed",
                                           "priority": 1.5, "note": "Core SOP"})
    assert r.status_code == 200
    f = next(x for x in owner.get("/t/owner/api/kb").json()["files"] if x["file"] == "sops/thunderdome.txt")
    assert f["provenance"] == "owner_confirmed" and f["priority"] == 1.5 and f["note"] == "Core SOP"
    assert owner.put("/t/owner/api/kb", json={"file": "../etc/passwd", "note": "x"}).status_code == 404


def test_personality_banned_words_always_enforced(owner):
    from operator_ai import guardrails, tenancy

    owner.put("/t/owner/api/personality", json={"display_name": "Coach Nova", "banned_words": ["hustle"]})
    t = tenancy.get("owner")
    flags = guardrails.check_for(t, "Time to hustle harder")
    assert guardrails.blocked(flags)
    assert owner.get("/t/owner/api/personality").json()["style_note"].startswith("Style rewriting needs AI")


def test_daily_question_limit(client):
    from operator_ai import tenancy

    demo_login(client, "owner")
    client.put("/t/demo/api/personality", json={"daily_question_limit": 14})
    demo_login(client, "student")  # the seed already asked 13 questions today
    assert client.post("/t/demo/api/coach/ask", json={"question": "What is the USP formula?"}).status_code == 200
    assert client.post("/t/demo/api/coach/ask", json={"question": "What is the USP formula?"}).status_code == 429


def test_recent_and_unanswered_queries(client):
    demo_login(client, "owner")
    allq = client.get("/t/demo/api/queries").json()["items"]
    un = client.get("/t/demo/api/queries", params={"unanswered": True}).json()["items"]
    assert len(allq) >= 13 and un and all(q["covered"] == "no" for q in un)


def test_engagement_alerts(demo_tenant):
    alerts = console.engagement_alerts(demo_tenant)
    assert any("held by guardrails" in a["text"] for a in alerts)


def test_provenance_panel(tenant):
    p = console.provenance_panel(tenant)
    assert p["by_provenance"].get("source_unconfirmed", 0) >= 1 and p["files"]


# ---------------------------------------------------------------- PWA
def test_pwa_manifest_and_service_worker(client):
    from tests.server import ensure_web_built

    ensure_web_built()
    m = client.get("/manifest.webmanifest")
    assert m.status_code == 200
    man = json.loads(m.text)
    assert man["display"] == "standalone" and any(i["sizes"] == "512x512" for i in man["icons"])
    sw = client.get("/sw.js")
    assert sw.status_code == 200 and "offline" in sw.text and "/api/" in sw.text
    assert client.get("/offline.html").status_code == 200


def test_drive_daily_auto_sync_and_synced_folder(env, tmp_path, monkeypatch):
    from operator_ai import tenancy as _t

    _t.ensure_defaults()
    t = _t.get("owner")
    synced = tmp_path / "Google Drive" / "Course updates"
    monkeypatch.setenv("OPERATOR_DRIVE_INBOX_OWNER", str(synced))
    synced.mkdir(parents=True)
    (synced / "new_sop.txt").write_text("Our new follow-up cadence is two, four and seven days.")
    assert console.drive_list(t)["inbox"] == "server-configured synced folder"
    assert console.drive_auto_sync(t, now=time.time()) == 1
    assert console.drive_auto_sync(t, now=time.time()) == 0  # once a day
    assert console.drive_staged(t)[0]["status"] == "staged"  # still needs the owner's OK
