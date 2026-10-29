"""One-click webinar launch: one brief -> chain + event + ONE approval -> page live, reminders on, deck downloadable."""
from datetime import datetime, timedelta

from tests.conftest import demo_login

from operator_ai import webinar_launch


def _when(days=10):
    return (datetime.now() + timedelta(days=days)).replace(hour=19, minute=0, second=0, microsecond=0).isoformat()


def test_launch_one_approval_publishes_page_and_turns_on_emails(owner):
    c = owner
    r = c.post("/t/owner/api/launches", json={"starts_at": _when(), "tz": "America/New_York",
                                              "niche": "trading coaches", "offer": "trading mentorship", "price": 5000})
    assert r.status_code == 200, r.text
    s = r.json()
    assert s["pack"]["status"] == "pending" and not s["live"] and s["emails"]["on"] == 0
    assert len(s["assets"]) == 10 and any(a["step"] == "slide_script" for a in s["assets"])
    slug = s["page_url"].rstrip("/").split("/")[-1]
    assert c.get(f"/w/owner/{slug}").status_code == 404  # nothing public before the approval

    ok = c.post(f"/t/owner/api/queue/{s['pack']['id']}/approve")
    assert ok.status_code == 200, ok.text
    after = c.get(f"/t/owner/api/launches/{s['webinar_id']}").json()
    assert after["live"] and after["emails"]["on"] == after["emails"]["total"] > 0
    assert c.get(f"/w/owner/{slug}").status_code == 200

    reg = c.post(f"/w/owner/{slug}/register", json={"name": "Ana Test", "email": "ana@x.example.com", "consent": True})
    assert reg.status_code == 200, reg.text
    assert c.get(f"/t/owner/api/launches/{s['webinar_id']}").json()["registrants"] == 1

    deck = c.get(f"/t/owner/api/launches/{s['webinar_id']}/deck.pptx")
    assert deck.status_code == 200 and deck.content[:2] == b"PK" and len(deck.content) > 20_000


def test_chat_launch_routes_and_parses_the_date(owner):
    r = owner.post("/t/owner/api/chat", json={"text": "Launch a webinar for my trading education client on "
                                                             "November 12 at 7pm ET"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["intent"] == "launch" and d["meta"]["launch"]["pack"]["status"] == "pending"
    assert "12 November" in d["meta"]["launch"]["when"] and "19:00" in d["meta"]["launch"]["when"]
    no_date = owner.post("/t/owner/api/chat", json={"text": "Can you launch a webinar for me?"}).json()
    assert no_date["intent"] == "goal"  # no date: the mission intake asks for it


def test_parse_request():
    now = datetime(2026, 10, 28, 12, 0)
    p = webinar_launch.parse_request("run a webinar on Nov 3rd at 6:30pm pt for my fitness client", now)
    assert p["starts_at"] == "2026-11-03T18:30" and p["tz"] == "America/Los_Angeles" and p["client_hint"] == "fitness"
    assert webinar_launch.parse_request("launch a webinar on Jan 5", now)["starts_at"].startswith("2027-01-05T19:00")


def test_student_launches_and_approves_their_own_and_sees_only_theirs(client, env):
    demo_login(client, "student")
    r = client.post("/t/demo/api/launches", json={"starts_at": _when(12), "niche": "dating coaches"})
    assert r.status_code == 200, r.text
    s = r.json()
    ok = client.post(f"/t/demo/api/queue/{s['pack']['id']}/approve")
    assert ok.status_code == 200, ok.text
    assert client.get(f"/t/demo/api/launches/{s['webinar_id']}").json()["live"]
    assert client.get(f"/t/demo/api/launches/{s['webinar_id']}/deck.pptx").status_code == 200
    mine = {x["webinar_id"] for x in client.get("/t/demo/api/launches").json()["items"]}
    assert s["webinar_id"] in mine

    demo_login(client, "owner")
    other = client.post("/t/demo/api/launches", json={"starts_at": _when(14), "niche": "real estate"}).json()
    demo_login(client, "student")
    assert client.get(f"/t/demo/api/launches/{other['webinar_id']}").status_code == 404
    assert other["webinar_id"] not in {x["webinar_id"] for x in client.get("/t/demo/api/launches").json()["items"]}


def test_webinar_is_about_each_clients_own_topic_and_audience(owner):
    """A growth operator's client webinar teaches that client's customers, in the client's own words."""
    def launch_for(name, niche, offer, brief=None):
        cid = owner.post("/t/owner/api/clients", json={"name": name, "niche": niche, "offer": offer}).json()["id"]
        if brief:
            assert owner.put(f"/t/owner/api/clients/{cid}", json={"brief": brief}).status_code == 200
        return owner.post("/t/owner/api/launches", json={"starts_at": _when(), "client_id": cid}).json()

    s8 = launch_for("Keys To Cashflow", "Section 8 real estate", "Section 8 investor mentorship")
    assert "Section 8 real estate" in s8["title"] and "first-time investors" in s8["title"]
    usp = next(a["body"] for a in s8["assets"] if a["step"] == "offer")
    assert "business owners" not in usp and "enquiries" not in usp

    fx = launch_for("Pip Masters", "forex trading education", "forex mentorship")
    assert fx["title"].startswith("How new forex traders get")

    own = launch_for("Rent Ready", "Section 8 real estate", "mentorship",
                     {"audience": "nurses", "outcome": "$2,000 a month in rent", "pain": "quitting their job"})
    assert own["title"] == "How nurses get $2,000 a month in rent without quitting their job"
    showup = next(a["body"] for a in own["assets"] if a["step"] == "showup_sequence")
    assert "next Thursday" not in showup and own["when"].split(",")[0] in showup  # the real date, not a placeholder
