"""2026 Playbooks: loader + validation, separate index and search, the third_party_kb switch, coach
integration (external [X#] list, honest not-covered first, invented [X#] stripped, guardrails), router
meta, demo seed, API routes and RBAC. All offline, AI off unless a test fakes the model."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from operator_ai import audit, auth, config, guardrails, llm, playbooks, runtime, settings, tenancy, threads, translate
from operator_ai.agents import coach, router
from tests.conftest import demo_login, login
from tests.fakes import FakeModel

NOT_COVERED_EN = translate.NOT_COVERED["en"]
X_HEAD = re.compile(r"^External best practice \(researched [0-9]{4}-[0-9]{2}(?: to [0-9]{4}-[0-9]{2})?, not the course material\):$")
# questions the fixture course does not cover but the playbooks do
PLAYBOOK_ONLY = ["How should I structure a revenue share deal with a creator?",
                 "What does the FTC say about earnings claims?"]
UNRELATED = ["How do I train a puppy to stop barking?", "What is the best keto meal plan?",
             "How do I write a Shopify theme in Liquid?", "What's the best way to learn Mandarin grammar?",
             "How do I fix a leaking kitchen tap?", "What are the visa rules for moving to Portugal?"]


@pytest.fixture
def on(tenant):
    tenant.set("third_party_kb", True)
    return tenant


@pytest.fixture
def ai_on(tenant, monkeypatch):
    fm = FakeModel()
    llm.set_transport(fm.transport)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-123456789")
    monkeypatch.setenv("OPERATOR_DISABLE_LLM", "0")
    settings.set_box(tenant, "ai_token_budget", 100000, "test budget")
    yield fm
    llm.set_transport(None)


def _bullets(answer: str) -> list[str]:
    return [ln for ln in answer.splitlines() if ln.startswith("• ")]


# ---------------------------------------------------------------- loader + validation
def _shipped() -> tuple[int, int]:
    files = sorted((Path(playbooks.__file__).parent / "knowledge" / "playbooks").glob("*.json"))
    return len(files), sum(len(json.loads(f.read_text())["entries"]) for f in files)


N_DOMAINS, N_ENTRIES = _shipped()


def test_library_loads_every_domain_and_entry():
    lib = playbooks.load()
    assert N_DOMAINS >= 9 and N_ENTRIES >= 200
    assert len(lib.domains) == N_DOMAINS
    assert len(lib.entries) == N_ENTRIES
    assert len(set(lib.entries)) == len(lib.order) == N_ENTRIES
    for e in lib.entries.values():
        assert e.title and e.guidance and e.category
        assert re.match(r"^\d{4}-\d{2}", e.as_of)
        assert e.confidence in ("high", "medium", "low")
        assert e.sources and all(s["url"].startswith(("https://", "http://")) for s in e.sources)
    assert lib.problems == []  # the shipped data is clean


def test_repeated_id_is_qualified_with_its_domain_and_reported(tmp_path):
    entry = {"id": "same-id", "title": "T", "category": "c", "guidance": "G.", "as_of": "2026-10", "confidence": "high",
             "sources": [{"title": "S", "url": "https://example.org/a"}]}
    for dom in ("alpha", "beta"):
        (tmp_path / f"{dom}.json").write_text(json.dumps({"domain": dom, "title": dom.title(), "researched": "2026-10-27",
                                                          "entries": [entry]}))
    lib = playbooks.load(tmp_path)
    assert "same-id" in lib.entries and "beta--same-id" in lib.entries
    assert any("already used" in p for p in lib.problems)


def test_validate_entry_rejects_unsafe_or_incomplete():
    problems: list[str] = []
    good = {"id": "ok-entry", "title": "T", "category": "c", "guidance": "G.", "as_of": "2026-10", "confidence": "High",
            "sources": [{"title": "S", "url": "https://example.org/a"}, {"title": "Bad", "url": "javascript:alert(1)"}]}
    e = playbooks.validate_entry(good, "d", "D", "2026-10-27", "f.json#1", problems)
    assert e is not None and e.confidence == "high" and len(e.sources) == 1  # the javascript: link is dropped
    assert e.steps == () and e.benchmarks == ()  # optional lists default to empty
    assert any("non-http(s) link" in p for p in problems)
    for bad in ({**good, "id": "Bad Id"}, {**good, "guidance": ""}, {**good, "as_of": "Oct 2026"},
                {**good, "confidence": "certain"}, {**good, "sources": [{"title": "x", "url": "ftp://x"}]},
                {**good, "sources": []}, "not an object"):
        before = len(problems)
        assert playbooks.validate_entry(bad, "d", "D", "2026-10-27", "f.json#2", problems) is None
        assert len(problems) > before


def test_load_from_a_directory_skips_bad_files_and_entries(tmp_path):
    entry = {"id": "a-one", "title": "Alpha one", "category": "cat", "guidance": "Do the alpha thing well.",
             "as_of": "2026-09", "confidence": "medium", "steps": ["Step one."], "do": ["Do it"], "dont": ["Skip it"],
             "benchmarks": [{"metric": "Rate", "value": "10%", "context": "ctx"}, {"metric": ""}],
             "sources": [{"title": "Src", "url": "https://example.org", "publisher": "P", "date": "2026"}]}
    (tmp_path / "alpha.json").write_text(json.dumps({
        "domain": "alpha", "title": "Alpha", "researched": "2026-10-01", "note": "N", "open_questions": ["Q?"],
        "entries": [entry, {**entry, "id": "a-two", "confidence": "unknown"}, {**entry, "id": "a-one"}]}))
    (tmp_path / "broken.json").write_text("{not json")
    (tmp_path / "beta.json").write_text(json.dumps({"domain": "Beta!", "title": "B", "researched": "2026-10-01", "entries": []}))
    (tmp_path / "gamma.json").write_text(json.dumps({"domain": "alpha", "title": "Dup", "researched": "2026-10-01", "entries": []}))
    lib = playbooks.load(tmp_path)
    assert list(lib.domains) == ["alpha"]
    assert set(lib.entries) == {"a-one", "alpha--a-one"}
    e = lib.entries["a-one"]
    assert e.benchmarks == ({"metric": "Rate", "value": "10%", "context": "ctx"},)
    text = "\n".join(lib.problems)
    for needle in ("broken.json: unreadable JSON", "beta.json: 'domain' must be", "gamma.json: domain 'alpha' already loaded",
                   "'confidence' must be", "benchmark has no metric", "already used"):
        assert needle in text, needle
    empty = tmp_path / "empty"
    empty.mkdir()
    assert playbooks.load(empty).entries == {} and playbooks.load(empty).problems


def test_domains_get_and_domain_detail():
    ds = playbooks.domains()
    assert len(ds) == N_DOMAINS and sum(d["count"] for d in ds) == N_ENTRIES
    assert all(re.match(r"^\d{4}-\d{2}-\d{2}$", d["researched"]) for d in ds)
    full = playbooks.get("speed-to-lead-five-minute-routing")
    assert full["steps"] and full["do"] and full["dont"] and full["benchmarks"] and full["sources"]
    assert full["external"] is True and "Not the programme's course material" in full["disclaimer"]
    assert playbooks.get("no-such-entry") is None and playbooks.get("") is None
    dd = playbooks.domain_detail("meta-ads-2026")
    assert dd["count"] == len(dd["entries"]) == 22 and dd["open_questions"]
    assert playbooks.domain_detail("nope") is None


# ---------------------------------------------------------------- index + search
def test_search_returns_x_citations_with_metadata(on):
    items = playbooks.search(on, "warm up a new email domain", k=5)
    assert [c["id"] for c in items] == [f"X{i}" for i in range(1, len(items) + 1)]
    assert "new-domain-and-mailbox-warmup" in [c["entry_id"] for c in items[:3]]
    c = items[0]
    for k in ("entry_id", "domain", "title", "as_of", "confidence", "sources", "snippet"):
        assert c[k]
    assert c["kind"] == "external" and c["file"].startswith("playbooks/") and c["library"] == "2026 Playbooks"
    assert all(s["url"].startswith("http") for s in c["sources"])


def test_index_is_separate_from_the_course(on):
    from operator_ai import index, retrieval

    lib = playbooks.load()
    course = index.for_tenant(on)
    assert lib.index is not course and lib.index.N == N_ENTRIES
    assert all(ch.kind == "playbook" and ch.corpus_id == "playbooks" for ch in lib.index.chunks)
    assert not any(f.startswith("playbooks/") for f in course.docs)
    assert all(not h["file"].startswith("playbooks/") for h in retrieval.search(on, "speed to lead routing"))


def test_search_respects_the_switch(tenant):
    assert playbooks.enabled(tenant) is False  # real workspace: off by default
    assert playbooks.search(tenant, "speed to lead") == []
    assert playbooks.relevant(tenant, ["How should I structure a revenue share deal with a creator?"]) == []
    assert playbooks.search(tenant, "speed to lead", respect_switch=False)  # owner preview
    tenant.set("third_party_kb", True)
    assert playbooks.search(tenant, "speed to lead")
    assert playbooks.search(None, "speed to lead") == []


@pytest.mark.parametrize("value,expected", [(True, True), (False, False), ("true", True), ("on", True), ("false", False),
                                            ("0", False), (0, False), (1, True), ([], False), ({"x": 1}, False)])
def test_switch_values(tenant, value, expected):
    tenant.set("third_party_kb", value)
    assert playbooks.enabled(tenant) is expected


def test_switch_server_default(tenant, monkeypatch):
    assert config.get().third_party_kb is False and playbooks.enabled(tenant) is False
    monkeypatch.setenv("OPERATOR_THIRD_PARTY_KB", "1")
    assert playbooks.enabled(tenant) is True  # unset tenant setting falls back to the server default
    tenant.set("third_party_kb", False)
    assert playbooks.enabled(tenant) is False  # an explicit owner "off" always wins


def test_search_edge_cases(on):
    assert playbooks.search(on, "") == [] and playbooks.search(on, "   ") == []
    assert playbooks.search(on, "zzqx qqvv") == []  # no lexical match -> nothing, not dense noise
    assert len(playbooks.search(on, "show rate", k=100)) <= 30
    only = playbooks.search(on, "creative testing", k=10, domain="meta-ads-2026")
    assert only and all(c["domain"] == "meta-ads-2026" for c in only)


@pytest.mark.parametrize("q", UNRELATED)
def test_relevance_gate_rejects_unrelated_questions(on, q):
    assert playbooks.relevant(on, [q]) == []


@pytest.mark.parametrize("q,entry_id", [
    ("How should I structure a revenue share deal with a creator?", "deal-structure-selection"),
    ("How do I warm up a new email domain?", "new-domain-and-mailbox-warmup"),
    ("How do I handle 'I need to think about it'?", "objection-handling-pause-question-isolate"),
    ("How long should a webinar be?", "webinar-length-and-offer-moment"),
])
def test_relevance_gate_finds_the_right_entry(on, q, entry_id):
    rel = playbooks.relevant(on, [q], k=4)
    assert 1 <= len(rel) <= 4 and entry_id in [c["entry_id"] for c in rel]
    assert [c["id"] for c in rel] == [f"X{i}" for i in range(1, len(rel) + 1)]


def test_search_and_get_scrub_banned_names(on):
    on.set("banned_names", ["Instagram"])
    items = playbooks.search(on, "instagram dm outreach limits", k=5)
    assert items and all("instagram" not in json.dumps(c).lower() for c in items)
    assert "Instagram" not in json.dumps(playbooks.get("instagram-dm-outreach-norms-and-limits", on))


# ---------------------------------------------------------------- coach integration
def test_coach_switch_off_keeps_the_honest_answer_only(tenant):
    r = coach.answer(tenant, PLAYBOOK_ONLY[0], record=False)
    assert r["covered"] == "no" and r["answer"] == NOT_COVERED_EN
    assert r["citations"] == [] and r["external"] == []
    steps = [s["tool"] for s in runtime.get(tenant, r["run_id"])["steps"]]
    assert "external" not in steps and "external_answer" not in steps


@pytest.mark.parametrize("q", PLAYBOOK_ONLY)
def test_coach_not_covered_adds_cited_external_best_practice(on, q):
    r = coach.answer(on, q, record=False)
    assert r["covered"] == "no" and r["citations"] == []
    lines = r["answer"].splitlines()
    assert lines[0] == NOT_COVERED_EN  # the honest sentence always comes first
    assert X_HEAD.match(lines[2]), lines[2]
    bullets = _bullets(r["answer"])
    assert 2 <= len(bullets) <= 4
    ext_ids = {c["id"] for c in r["external"]}
    assert ext_ids and all(c["kind"] == "external" and c["id"].startswith("X") for c in r["external"])
    for b in bullets:
        ids = re.findall(r"\[(X\d+)\]", b)
        assert ids and set(ids) <= ext_ids
        assert not guardrails.blocked(guardrails.check_for(on, b, {}))
    assert not re.search(r"\[S\d+\]", r["answer"])  # never presented as course material
    assert "course material" in r["answer"].split("\n", 2)[2].lower()  # the header says "not the course material"
    assert r["mode"] == "rules" and r["external_mode"] == "rules" and "external best practice added" in r["label"]
    run = runtime.get(on, r["run_id"])
    assert {"external", "external_answer"} <= {s["tool"] for s in run["steps"]} and run["ai_mode"] == "rules"


def test_coach_covered_answer_gets_a_separate_external_list(on):
    r = coach.answer(on, "When should reminders go out before a call?", record=False)
    assert r["covered"] == "yes"
    assert r["citations"] and all(c["id"].startswith("S") and not c["file"].startswith("playbooks/") for c in r["citations"])
    assert r["external"] and all(c["id"].startswith("X") and c["kind"] == "external" for c in r["external"])
    assert "reminder-cadence-and-no-show-recovery" in [c["entry_id"] for c in r["external"]]
    assert not re.search(r"\[X\d+\]", r["answer"]) and "External best practice" not in r["answer"]
    assert r["label"].startswith("Extractive")  # the course answer's label is unchanged


def test_coach_unrelated_question_stays_honest_with_switch_on(on):
    for q in UNRELATED[:3]:
        r = coach.answer(on, q, record=False)
        assert r["answer"] == NOT_COVERED_EN and r["external"] == [] and r["covered"] == "no"


def test_coach_course_covered_without_playbook_match_has_no_external(on):
    r = coach.answer(on, "What are the rules for a Thunder Dome test?", record=False)
    assert r["covered"] == "yes" and r["external"] == [] and r["citations"][0]["file"] == "sops/thunderdome.txt"


def test_coach_translated_question_keeps_language_and_honesty(on):
    r = coach.answer(on, "¿Cómo configuro una tienda de TikTok?", record=False)
    assert r["covered"] == "no" and r["answer"].startswith(translate.NOT_COVERED["es"])


def test_external_bullets_skip_guardrail_blocked_text(on):
    # every candidate the library offers for refunds/guarantees mentions "guarantee"; none may be shown blocked
    ext = playbooks.relevant(on, ["Can I offer a money-back guarantee?"], k=4)
    assert ext
    for b in coach.external_bullets(on, "Can I offer a money-back guarantee?", ext):
        assert not guardrails.blocked(guardrails.check_for(on, b, {}))
        assert re.search(r"\[X\d+\]$", b)


def test_verify_external_strips_invented_markers():
    clean, kept, stripped = coach.verify_external("Do this [X1] and that [X9] [X2].", {"X1", "X2"})
    assert clean == "Do this [X1] and that [X2]." and kept == ["X1", "X2"] and stripped == ["X9"]


def test_ai_external_bullets_verified_stripped_and_guardrailed(on, ai_on):
    ai_on.reply = json.dumps({"bullets": [
        "Pure revenue share fits a creator with a proven offer [X1]",
        "Take equity only with real governance rights [X2] [X99]",
        "Totally invented advice [X42]",
        "We guarantee you will make $10k a month [X1]",
        "Course passage smuggled in [S3] [X3]",
    ]})
    r = coach.answer(on, PLAYBOOK_ONLY[0], record=False)
    bullets = _bullets(r["answer"])
    assert r["answer"].startswith(NOT_COVERED_EN) and 2 <= len(bullets) <= 4
    assert {"X99", "X42"} <= set(r["stripped_citations"])
    assert "[X99]" not in r["answer"] and "[X42]" not in r["answer"] and "Totally invented" not in r["answer"]
    assert "guarantee" not in r["answer"].lower() and "[S3]" not in r["answer"]
    assert r["external_mode"] == "live" and r["mode"] == "live" and r["citations"] == []
    ids = {c["id"] for c in r["external"]}
    assert set(re.findall(r"\[(X\d+)\]", r["answer"])) <= ids
    assert ai_on.calls and "not course material" in ai_on.calls[-1]["body"]["messages"][0]["content"]


def test_ai_external_too_few_safe_bullets_falls_back_to_rules(on, ai_on):
    ai_on.reply = json.dumps({"bullets": ["We guarantee results [X1]", "Invented [X77]"]})
    r = coach.answer(on, PLAYBOOK_ONLY[0], record=False)
    assert r["external_mode"] == "rules" and 2 <= len(_bullets(r["answer"])) <= 4
    assert "X77" in r["stripped_citations"] and "guarantee results" not in r["answer"]


def test_ai_external_then_labelled_replay_when_ai_off(on, ai_on):
    ai_on.reply = json.dumps({"bullets": ["Revenue share suits proven offers [X1]", "Equity needs governance rights [X2]"]})
    first = coach.answer(on, PLAYBOOK_ONLY[0], record=False)
    assert first["external_mode"] == "live"
    settings.set_box(on, "ai_token_budget", 0, "")
    n_calls = len(ai_on.calls)
    again = coach.answer(on, PLAYBOOK_ONLY[0], record=False)
    assert len(ai_on.calls) == n_calls  # no model request with the budget at 0
    assert again["external_mode"] == "replay" and again["external_label"].startswith("Replayed AI answer")
    assert "• Revenue share suits proven offers. [X1]" in again["answer"]


def test_invented_x_marker_in_course_answer_is_stripped(on, ai_on):
    ai_on.reply = json.dumps({"covered": "yes", "answer": "Run it for 72 hours [S1], see also [X3].", "citations": ["S1"]})
    r = coach.answer(on, "How long should a Thunder Dome test run before I judge it?", record=False)
    assert "[X3]" not in r["answer"] and "X3" in r["stripped_citations"]
    assert [c["id"] for c in r["citations"]] == ["S1"]


def test_coach_thread_and_router_carry_external_separately(on):
    from operator_ai import auth as _auth

    uid = _auth.create_user(on, "stu1", "pw-for-tests-123", "student")
    user = {"id": uid, "username": "stu1", "role": "student"}
    res = router.handle(on, PLAYBOOK_ONLY[0], user)
    assert res["intent"] == "question"
    assert res["meta"]["external"] and res["meta"]["citations"] == []
    assert res["answer"].startswith(NOT_COVERED_EN)
    t = threads.get(on, res["thread_id"], uid)
    meta = t["messages"][-1]["meta"]
    assert meta["external"] == res["meta"]["external"] and meta["citations"] == []
    r2 = coach.answer(on, PLAYBOOK_ONLY[1], user=user, record=True, thread=True)
    t2 = threads.get(on, r2["thread_id"], uid)
    assert t2["messages"][-1]["meta"]["external"]


# ---------------------------------------------------------------- demo seed + workspaces
def test_demo_seed_turns_the_switch_on_and_real_workspaces_stay_off(demo_tenant):
    assert demo_tenant.get("third_party_kb") is True and playbooks.enabled(demo_tenant)
    owner = tenancy.get("owner")
    assert owner.get("third_party_kb") is None and not playbooks.enabled(owner)
    client, _ = tenancy.create("pbclient", "Client Co")
    assert not playbooks.enabled(client)
    # seeded demo threads show external chips for a question the course doesn't cover
    with demo_tenant.db() as c:
        rows = [json.loads(r["meta"] or "{}") for r in c.execute("SELECT meta FROM messages WHERE role='assistant'")]
    assert any("external" in m for m in rows)


def test_demo_ensure_seeded_backfills_only_an_unset_switch(demo_tenant):
    from operator_ai import demo

    with demo_tenant.db() as c:
        c.execute("DELETE FROM settings WHERE key='third_party_kb'")
    demo.ensure_seeded(demo_tenant)
    assert demo_tenant.get("third_party_kb") is True
    demo_tenant.set("third_party_kb", False)  # an owner's explicit "off" in the demo is kept
    demo.ensure_seeded(demo_tenant)
    assert demo_tenant.get("third_party_kb") is False


# ---------------------------------------------------------------- API + RBAC
@pytest.mark.parametrize("role", auth.ROLES)
def test_overview_every_role(client, role):
    demo_login(client, role)
    r = client.get("/t/demo/api/playbooks")
    assert r.status_code == 200
    d = r.json()
    assert d["enabled"] is True and d["can_browse"] is True and d["total"] == N_ENTRIES and len(d["domains"]) == N_DOMAINS
    assert d["banner"] == playbooks.BANNER and d["latest_researched"] and d["switch"] == "third_party_kb"
    assert d["can_toggle"] is (role == "owner")
    if role != "owner":
        assert d["problems"] == [] and d["toggle"] is None
    else:
        assert isinstance(d["problems"], list) and d["toggle"]["key"] == "third_party_kb"


def test_search_entry_and_domain_endpoints(client):
    demo_login(client, "student")
    r = client.get("/t/demo/api/playbooks/search", params={"q": "warm up a new email domain"})
    assert r.status_code == 200 and r.json()["items"][0]["id"] == "X1"
    assert r.json()["external"] is True
    assert client.get("/t/demo/api/playbooks/search").json()["items"] == []
    assert client.get("/t/demo/api/playbooks/search", params={"q": "x"}).json()["hint"]
    assert client.get("/t/demo/api/playbooks/search", params={"q": "a" * 301}).status_code == 422
    assert client.get("/t/demo/api/playbooks/search", params={"q": "leads", "k": 0}).status_code == 422
    e = client.get("/t/demo/api/playbooks/entry/speed-to-lead-five-minute-routing")
    assert e.status_code == 200 and e.json()["steps"] and e.json()["sources"][0]["url"].startswith("http")
    assert client.get("/t/demo/api/playbooks/entry/does-not-exist").status_code == 404
    assert client.get("/t/demo/api/playbooks/entry/..%2F..%2Fetc").status_code == 404
    dd = client.get("/t/demo/api/playbooks/domain/compliance-2026")
    assert dd.status_code == 200 and dd.json()["entries"]
    assert client.get("/t/demo/api/playbooks/domain/nope").status_code == 404


def test_unauthenticated_requests_are_refused(client):
    tenancy.ensure_defaults()
    assert client.get("/t/owner/api/playbooks").status_code == 401
    assert client.get("/t/owner/api/playbooks/search", params={"q": "leads"}).status_code == 401
    assert client.get("/t/owner/api/playbooks/entry/speed-to-lead-five-minute-routing").status_code == 401


def test_switch_off_gates_non_owners_and_owner_toggles_via_settings(client):
    login(client)  # real owner workspace, zero demo data
    t = tenancy.get("owner")
    auth.create_user(t, "coach1", "coach-pw-123456", "coach")
    ov = client.get("/t/owner/api/playbooks").json()
    assert ov["enabled"] is False and ov["can_browse"] is True and ov["off_message"]  # owner may preview
    assert client.get("/t/owner/api/playbooks/search", params={"q": "speed to lead"}).json()["items"]
    login(client, username="coach1", password="coach-pw-123456")
    ov = client.get("/t/owner/api/playbooks").json()
    assert ov["can_browse"] is False and ov["can_toggle"] is False and "switched off" in ov["off_message"]
    assert client.get("/t/owner/api/playbooks/search", params={"q": "speed to lead"}).status_code == 403
    assert client.get("/t/owner/api/playbooks/entry/speed-to-lead-five-minute-routing").status_code == 403
    assert client.get("/t/owner/api/playbooks/domain/meta-ads-2026").status_code == 403
    assert client.put("/t/owner/api/settings", json={"key": "third_party_kb", "value": True}).status_code == 403
    login(client)
    assert client.put("/t/owner/api/settings", json={"key": "third_party_kb", "value": True}).status_code == 200
    assert any(a["kind"] == "settings" and json.loads(a["detail"] or "{}").get("key") == "third_party_kb"
               for a in audit.recent(t, 20))
    login(client, username="coach1", password="coach-pw-123456")
    assert client.get("/t/owner/api/playbooks").json()["can_browse"] is True
    assert client.get("/t/owner/api/playbooks/search", params={"q": "speed to lead"}).status_code == 200


def test_coach_api_returns_external_for_demo_student(client):
    demo_login(client, "student")
    r = client.post("/t/demo/api/coach/ask", json={"question": PLAYBOOK_ONLY[0]})
    assert r.status_code == 200
    d = r.json()
    assert d["answer"].startswith(NOT_COVERED_EN) and d["external"] and d["citations"] == []
    chat = client.post("/t/demo/api/chat", json={"text": PLAYBOOK_ONLY[1]})
    assert chat.status_code == 200 and chat.json()["meta"]["external"]


# ---------------------------------------------------------------- review fixes: relevance focus
# questions whose words appear in the library but which no entry is ABOUT: no external bullets/chips
OFF_TOPIC_BUT_WORDY = ["How do I fire a client?", "How much should I charge?", "How do I make money online?",
                       "What's the best CRM?"]


@pytest.mark.parametrize("q", OFF_TOPIC_BUT_WORDY)
def test_relevance_gate_needs_the_entry_to_be_about_the_question(on, q):
    assert playbooks.relevant(on, [q], k=4) == []
    j = playbooks.judge(q)
    assert j["covered"] is False


def test_focus_explains_why_an_entry_is_rejected():
    lib = playbooks.load()
    idx = lib.index
    e = lib.entries["signal-ladder-optimization-event"]  # a pixel "fires" events; not about firing clients
    f = playbooks.focus("How do I fire a client?", e, idx.tfs[lib.order.index(e.id)], idx)
    assert f["ok"] is False and f["in_title"] == []
    e2 = lib.entries["webinar-length-and-offer-moment"]
    f2 = playbooks.focus("How long should a webinar be?", e2, idx.tfs[lib.order.index(e2.id)], idx)
    assert f2["ok"] is True and "webinar" in f2["in_title"]


def test_refund_question_ranks_refund_entries_not_pipeline_hygiene(on):
    rel = playbooks.relevant(on, ["How do I deal with a refund request?"], k=4)
    assert rel and "refund" in rel[0]["title"].lower()
    assert "pipeline-stages-and-crm-hygiene" not in [c["entry_id"] for c in rel]


def test_coach_vague_question_stays_honest_with_switch_on(on):
    r = coach.answer(on, "How much should I charge?", record=False)
    if r["covered"] == "no":
        assert r["answer"] == NOT_COVERED_EN
    assert r["external"] == [] and "[X" not in r["answer"]


# ---------------------------------------------------------------- review fixes: surfaces outside the app
def test_lead_magnet_mini_never_adds_external_best_practice(on):
    from operator_ai import console

    console.create_mini(on, "lm-pb", "Lead magnet", ["sops/thunderdome.txt"], 5, "I agree to share my email")
    s = console.mini_start(on, "lm-pb", "visitor@example.invalid", True)
    r = console.mini_ask(on, "lm-pb", s["token"], PLAYBOOK_ONLY[0])
    assert r["covered"] == "no" and r["citations"] == []
    assert "External best practice" not in r["answer"] and not re.search(r"\[X\d+\]", r["answer"])
    # same question in the app (switch on) does get the external section: only the public mini is fenced off
    assert coach.answer(on, PLAYBOOK_ONLY[0], record=False)["external"]


def test_coach_external_false_overrides_the_switch(on):
    r = coach.answer(on, PLAYBOOK_ONLY[0], record=False, external=False)
    assert r["answer"] == NOT_COVERED_EN and r["external"] == []
    r2 = coach.answer(on, "When should reminders go out before a call?", record=False, external=False)
    assert r2["covered"] == "yes" and r2["external"] == []
    steps = {s["tool"] for s in runtime.get(on, r2["run_id"])["steps"]}
    assert "external" not in steps


def _linked_student(t, channel: str, ext_id: str, username: str = "chat-student") -> int:
    """Channel replies go through inbound.route: only a sender linked to a student/member gets the cited Coach
    (a stranger gets a holding reply), so these tests link the chat first, as a student would with a link code."""
    uid = auth.create_user(t, username, "x" * 16, "student")
    with t.db() as c:
        sid = c.execute("INSERT INTO students(name,email) VALUES('Chat Student','chat.student@example.invalid')").lastrowid
        c.execute("UPDATE users SET student_id=? WHERE id=?", (sid, uid))
        c.execute("INSERT INTO channel_links(channel,ext_id,user_id,opted_in,status,created) VALUES(?,?,?,1,'linked',1)",
                  (channel, ext_id, uid))
    return uid


def test_channel_reply_draft_resolves_every_x_marker_and_waits_for_approval(on):
    from operator_ai import inbound

    _linked_student(on, "telegram", "chat-42")
    out = inbound.route(on, "telegram", "chat-42", PLAYBOOK_ONLY[0], {})
    assert out["status"] == "pending"  # queued, never sent without approval
    with on.db() as c:
        row = c.execute("SELECT body, status FROM queue WHERE id=?", (out["queued"],)).fetchone()
    body = row["body"]
    assert row["status"] == "pending" and body.startswith(NOT_COVERED_EN)
    used = set(re.findall(r"\[(X\d+)\]", body.split("External, not the course material")[0]))
    assert used
    key = body.split("External, not the course material", 1)[1]
    for x in used:
        assert re.search(rf"^\[{x}\] .+ \(researched \d{{4}}-\d{{2}}.*\) https?://", key, re.M), x


def test_external_refs_only_lists_markers_the_answer_uses():
    ext = [{"id": "X1", "title": "A", "as_of": "2026-10", "confidence": "high", "url": "https://example.org/a"},
           {"id": "X2", "title": "B", "as_of": "2026-09", "confidence": "low", "url": "https://example.org/b"}]
    refs = coach.external_refs("Do this [X2].", ext)
    assert "[X2] B (researched 2026-09, low confidence) https://example.org/b" in refs and "[X1]" not in refs
    assert coach.external_refs("No markers here.", ext) == "" and coach.external_refs("[X1]", []) == ""


def test_mcp_ask_coach_keeps_external_separate(on):
    from operator_ai import mcp_server

    r = mcp_server._call(on, "ask_coach", {"question": PLAYBOOK_ONLY[0]})
    assert r["citations"] == [] and r["external"]
    assert set(re.findall(r"\[(X\d+)\]", r["answer"])) <= {c["id"] for c in r["external"]}


def test_phone_line_never_speaks_x_markers(client):
    from urllib.parse import urlencode

    from operator_ai import channels, vault

    t = tenancy.get("owner")
    t.set("third_party_kb", True)
    vault.put(t, {"sms_auth_token": "voicetok"})
    settings.set_box(t, "live_channels_enabled", True, "test")
    _linked_student(t, "phone", "+1-555-0199", "caller-student")  # only a known caller hears course answers
    params = {"From": "+1-555-0199", "SpeechResult": PLAYBOOK_ONLY[0]}
    url = "http://testserver/t/owner/hooks/voice"
    r = client.post("/t/owner/hooks/voice", content=urlencode(params),
                    headers={"content-type": "application/x-www-form-urlencoded",
                             "x-twilio-signature": channels.twilio_signature("voicetok", url, params)})
    assert r.status_code == 200 and "doesn" in r.text and "External best practice" in r.text
    assert not re.search(r"\[[SX]\d+\]", r.text)


# ---------------------------------------------------------------- review fixes: AI external bullets
def test_ai_external_bullet_claiming_to_be_course_material_is_dropped(on, ai_on):
    ai_on.reply = json.dumps({"bullets": [
        "The course says to always take equity [X1]",
        "Per the course material, revenue share is best [X2]",
        "Pure revenue share fits a creator with a proven offer [X1]",
        "Equity needs real governance rights [X2] [S2]",
    ]})
    r = coach.answer(on, PLAYBOOK_ONLY[0], record=False)
    assert r["external_mode"] == "live"
    assert "course says" not in r["answer"].lower() and "per the course material" not in r["answer"].lower()
    assert "S2" in r["stripped_citations"] and "[S2]" not in r["answer"]
    assert len(_bullets(r["answer"])) == 2


def test_ai_external_non_text_bullets_are_ignored(on, ai_on):
    ai_on.reply = json.dumps({"bullets": [{"text": "Object bullet [X1]"}, 42, None,
                                          "Pure revenue share fits a proven offer [X1]", "Equity needs governance [X2]"]})
    r = coach.answer(on, PLAYBOOK_ONLY[0], record=False)
    assert "{'" not in r["answer"] and "Object bullet" not in r["answer"] and "42" not in r["answer"]
    assert r["external_mode"] == "live" and len(_bullets(r["answer"])) == 2
    ai_on.reply = json.dumps({"bullets": {"not": "a list"}})
    r2 = coach.answer(on, PLAYBOOK_ONLY[1], record=False)
    assert r2["external_mode"] in ("rules", None) and r2["answer"].startswith(NOT_COVERED_EN)


# ---------------------------------------------------------------- review fixes: switch off + audit
def test_switch_off_overview_carries_no_library_content_for_non_owners(client):
    login(client)
    t = tenancy.get("owner")
    auth.create_user(t, "setter1", "setter-pw-123456", "setter")
    owner_view = client.get("/t/owner/api/playbooks").json()
    assert owner_view["total"] == N_ENTRIES and owner_view["domains"]  # the owner previews to decide
    login(client, username="setter1", password="setter-pw-123456")
    r = client.get("/t/owner/api/playbooks")
    assert r.status_code == 200
    d = r.json()
    assert d["can_browse"] is False and d["domains"] == [] and d["total"] == 0 and d["problems"] == []
    assert "compliance" not in json.dumps(d).lower()


def test_switch_audit_records_which_way_it_was_turned(client):
    login(client)
    t = tenancy.get("owner")
    for v in (True, False):
        assert client.put("/t/owner/api/settings", json={"key": "third_party_kb", "value": v}).status_code == 200
    rows = [json.loads(a["detail"] or "{}") for a in audit.recent(t, 20) if a["kind"] == "settings"]
    turns = [r.get("to") for r in rows if r.get("key") == "third_party_kb"]
    assert True in turns and False in turns


# ---------------------------------------------------------------- review fixes: index cache + warm-up
def _mini_library(root):
    entry = {"id": "c-one", "title": "Cache test entry", "category": "cat", "guidance": "Warm the inbox slowly.",
             "as_of": "2026-09", "confidence": "high", "sources": [{"title": "Src", "url": "https://example.org"}]}
    (root / "cachetest.json").write_text(json.dumps({"domain": "cachetest", "title": "Cache", "researched": "2026-10-01",
                                                     "entries": [entry, {**entry, "id": "c-two", "title": "Second"}]}))


def test_corrupt_or_stale_vector_cache_is_rebuilt(env, tmp_path, monkeypatch):
    root = tmp_path / "pbcache"
    root.mkdir()
    _mini_library(root)
    lib = playbooks.load(root)
    assert lib.index.vectors.shape[0] == 2
    caches = [p for p in config.get().index_dir.glob("playbooks-*") if (p / "vectors.npy").exists()]
    mine = [p for p in caches if lib.key.rsplit("|", 1)[-1] in p.name]
    assert mine
    for p in mine:
        (p / "vectors.npy").write_bytes(b"\x93NUMPY garbage")  # a half-written file from a crashed worker
    playbooks.clear_cache()
    lib2 = playbooks.load(root)
    assert lib2.index.vectors.shape[0] == 2 and lib2.index.search("warm inbox", k=2)
    # a change in what gets indexed (same files) must not reuse the old vectors
    monkeypatch.setattr(playbooks.Entry, "index_text", lambda self: self.title)
    playbooks.clear_cache()
    lib3 = playbooks.load(root)
    assert lib3.index.vectors.shape[0] == 2
    names = {p.name for p in config.get().index_dir.glob("playbooks-*")}
    assert len([n for n in names if lib.key.rsplit("|", 1)[-1] in n]) == 2


def test_startup_warm_up_also_builds_the_playbooks_index(env, monkeypatch):
    import threading

    from operator_ai import index

    class Now:
        def __init__(self, target=None, **kw):
            self.target = target

        def start(self):
            self.target()

    monkeypatch.setattr(threading, "Thread", Now)
    playbooks.clear_cache()
    index.warm_async()
    assert playbooks.load()._index is not None


def test_external_refs_never_put_a_blocked_title_into_a_queued_draft(on):

    ext = [{"id": "X1", "title": "Refund and guarantee design: objective conditions", "domain_title": "Compliance 2026",
            "as_of": "2026-10", "confidence": "high", "url": "https://example.org/r"}]
    refs = coach.external_refs("Publish a refund SLA. [X1]", ext, on)
    assert "guarantee" not in refs.lower() and "[X1] Compliance 2026 entry (researched 2026-10" in refs
    from operator_ai import inbound

    _linked_student(on, "telegram", "chat-7")
    out = inbound.route(on, "telegram", "chat-7", "How do I deal with a refund request?", {})
    with on.db() as c:
        row = c.execute("SELECT body, status, flags FROM queue WHERE id=?", (out["queued"],)).fetchone()
    assert row["status"] in ("pending", "needs_human"), (row["status"], row["flags"])  # never "held"
    assert not guardrails.blocked(json.loads(row["flags"] or "[]"))
