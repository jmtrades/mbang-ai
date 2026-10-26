"""One chat that does the work: routing, follow-ups, numbers, assets, screenshots, Ads exports."""
import io

import pytest

from operator_ai import ads_import
from operator_ai.agents import coach, router
from tests.conftest import demo_login

ADS_CSV = """Ad name,Amount spent (USD),Results,Impressions,Link clicks,CTR (link click-through rate)
Hook A - stop discounting,"1,240.50",96,48210,1012,2.10%
Hook B - referral nights,410.00,6,15800,95,0.60%
Hook C - the Monday number,22.00,0,850,4,0.47%
Hook D - diary full,600.00,0,9200,40,0.43%
Total,"2,272.50",102,74060,1151,1.55%
"""


@pytest.mark.parametrize("text,intent", [
    ("What are the rules for a Thunder Dome test?", "question"),
    ("800 DMs, 40 replies, 6 booked, 2 showed", "metrics"),
    ("My reply rate is 4% on 500 DMs, what should I fix?", "metrics"),
    ("Review this DM:\nHey! What's your goal? How many clients? Our fee is $3k.", "asset"),
    ("Subject: Re: our chat\nDoors closing tonight, last chance", "asset"),
    (ADS_CSV, "ads_csv"),
])
def test_classify(text, intent):
    assert router.classify(text) == intent


def test_parse_metrics_counts_rates_and_cpl():
    m = router.parse_metrics("800 DMs, 40 replies, 20 conversations, 10 links sent, 6 booked, 2 showed, 1 closed, price $3k")
    assert m == {"dms_sent": 800, "replies": 40, "conversations": 20, "links_sent": 10, "booked": 6, "showed": 2,
                 "closed": 1, "offer_price": 3000}
    assert router.parse_metrics("show rate 45% from 20 booked calls") == {"booked": 20, "showed": 9}
    m = router.parse_metrics("CPL is $40 and we got 50 leads")
    assert m["leads"] == 50 and m["ad_spend"] == 2000


def test_followup_read_in_context(tenant):
    q, used = coach.resolve_followup("How long should it run?", "What are the rules for a Thunder Dome test?")
    assert used and "Thunder Dome" in q
    r = coach.answer(tenant, "How long should it run?", record=False, context="What are the rules for a Thunder Dome test?")
    assert r["covered"] == "yes" and r["citations"][0]["file"] in ("sops/thunderdome.txt", "course/transcripts/vid_j1k2l3.txt")
    assert "seventy-two hours" in r["answer"]
    q2, used2 = coach.resolve_followup("What are the seven segments of a webinar?", "What are the rules for a Thunder Dome test?")
    assert not used2  # a full new question is not rewritten


def test_ads_export_audit_kill_and_winner(tenant):
    a = ads_import.audit(tenant, ADS_CSV, target_cpl=15)
    by = {r["name"].split(" - ")[0]: r for r in a["ads"]}
    assert len(a["ads"]) == 4  # the Total row is skipped
    assert by["Hook A"]["spend"] == 1240.5 and by["Hook A"]["verdict"] == "keep"
    assert by["Hook B"]["kill"] and by["Hook D"]["kill"]  # CPL 68 > 2x target; 600 spent, 0 leads
    assert by["Hook C"]["verdict"].startswith("too early")  # under 1,000 impressions
    assert a["winner"]["name"].startswith("Hook A") and a["writes"] == 0
    assert a["citations"][0]["file"] == "sops/thunderdome.txt"


def test_ads_export_bad_file_explains(tenant):
    with pytest.raises(ads_import.ImportError_, match="spend and impressions"):
        ads_import.audit(tenant, "name,foo\nx,1\n")


def test_chat_api_routes_each_intent_into_one_thread(client):
    demo_login(client, "student")
    r1 = client.post("/t/demo/api/chat", json={"text": "What are the rules for a Thunder Dome test?"}).json()
    tid = r1["thread_id"]
    assert r1["intent"] == "question" and r1["meta"]["citations"]
    r2 = client.post("/t/demo/api/chat", json={"text": "How long should it run?", "thread_id": tid}).json()
    assert r2["meta"]["followup"] is True and r2["meta"]["covered"] == "yes"
    r3 = client.post("/t/demo/api/chat", json={"text": "800 DMs, 40 replies, 20 conversations, 10 links sent, 6 booked, "
                                                        "2 showed, 1 closed, price $3k", "thread_id": tid}).json()
    assert r3["intent"] == "metrics" and r3["meta"]["bottleneck"]["key"] == "reply" and r3["meta"]["next_best_action"]
    r4 = client.post("/t/demo/api/chat", json={"text": "Review this DM:\nHey! What's your goal? How many clients? "
                                                        "Our fee is $3k a month.", "thread_id": tid}).json()
    assert r4["intent"] == "asset" and r4["meta"]["review"]["asset_type"] == "dm_script"
    assert any(f.get("guardrail") in ("outreach_money", "outreach_price") for f in r4["meta"]["review"]["fixes"])
    th = client.get(f"/t/demo/api/threads/{tid}").json()
    assert len(th["messages"]) == 8 and [m["meta"].get("intent") for m in th["messages"] if m["role"] == "assistant"] == \
        ["question", "question", "metrics", "asset"]


def test_chat_upload_ads_csv_and_screenshot(client):
    demo_login(client, "owner")
    r = client.post("/t/demo/api/chat/upload", files={"file": ("export.csv", ADS_CSV.encode(), "text/csv")}).json()
    assert r["intent"] == "ads_csv" and r["meta"]["ads_audit"]["winner"]["name"].startswith("Hook A")
    from operator_ai import uploads

    if uploads.ocr_backend() is None:
        return
    from PIL import Image, ImageDraw, ImageFont
    import os

    img = Image.new("RGB", (1100, 160), "white")
    font = None
    for f in ("/System/Library/Fonts/Supplemental/Arial.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"):
        if os.path.exists(f):
            font = ImageFont.truetype(f, 40)
    ImageDraw.Draw(img).text((20, 50), "800 DMs, 40 replies, 6 booked, 2 showed", fill="black", font=font)
    b = io.BytesIO()
    img.save(b, "PNG")
    r2 = client.post("/t/demo/api/chat/upload", files={"file": ("dash.png", b.getvalue(), "image/png")}).json()
    assert r2["intent"] == "metrics" and r2["receipt"]["method"].startswith("local OCR")


def test_chat_never_sends(client):
    demo_login(client, "owner")
    client.post("/t/demo/api/chat", json={"text": "Review this DM:\nWe guarantee results!"})
    from operator_ai import tenancy

    with tenancy.get("demo").db() as c:
        assert c.execute("SELECT COUNT(*) FROM channel_messages WHERE direction='out'").fetchone()[0] == 0
