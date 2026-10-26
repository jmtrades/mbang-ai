"""§11 builder demo, headless, AI off, three times in a row."""
import pytest

from operator_ai import departments as dp
from operator_ai import queue

pytestmark = pytest.mark.ui


def test_generate_is_rules_and_fast():
    gen = dp.generate(dp.PRESETS["physio"])
    assert gen["counts"] == {"departments": 5, "agents": 10}
    assert gen["generated_by"] == "rules, no AI call" and gen["ms"] < 1000
    for d in gen["departments"]:
        assert d["kpi"] and len(d["agents"]) == 2
        for a in d["agents"]:
            assert a["guardrails"] == dp.GUARDS and a["instructions"] and a["inputs"] and a["trigger"] and a["kpi"]
    setter = next(a for d in gen["departments"] for a in d["agents"] if a["key"] == "dm_setter")
    assert len(setter["inputs"]) == 3 and all("(DEMO)" in i["from"] for i in setter["inputs"])


def test_dm_setter_run_ai_off(demo_tenant):
    gen = dp.generate(dp.PRESETS["physio"])
    bid = dp.save_build(demo_tenant, "physio", gen["business"], gen)
    r = dp.run_agent(demo_tenant, bid, "dm_setter", background=False)
    assert r["headline"] == "3 drafts · 3 pass review · Sent: 0"
    for c in r["cards"]:
        assert c["source"] == "Rules" and c["label"].startswith("Rules draft (AI not used): ")
        it = queue.get(demo_tenant, c["queue_id"])
        assert it["status"] == "pending" and it["title"].startswith("Reply to ") and it["title"].endswith("(DEMO)")


def test_ai_draft_that_trips_a_rule_falls_back(demo_tenant, monkeypatch):
    monkeypatch.setattr(dp, "ai_draft", lambda *a, **k: ("We guarantee results. It's $2,500.", None))
    gen = dp.generate(dp.PRESETS["physio"])
    bid = dp.save_build(demo_tenant, "physio", gen["business"], gen)
    r = dp.run_agent(demo_tenant, bid, "dm_setter", background=False)
    assert all(c["source"] == "Rules" and "tripped a guardrail" in c["label"] for c in r["cards"])
    assert r["summary"]["pass_review"] == 3


def test_ai_draft_used_when_clean(demo_tenant, monkeypatch):
    monkeypatch.setattr(dp, "ai_draft", lambda *a, **k: ("Hi! Great question. What's your main goal this quarter?", None))
    gen = dp.generate(dp.PRESETS["physio"])
    bid = dp.save_build(demo_tenant, "physio", gen["business"], gen)
    r = dp.run_agent(demo_tenant, bid, "dm_setter", background=False)
    assert all(c["source"] == "AI" for c in r["cards"])


def test_builder_daily_cap_and_switch(demo_tenant, monkeypatch):
    monkeypatch.setenv("OPERATOR_BUILDER_AI", "0")
    assert dp.ai_reason(demo_tenant) == "builder AI off (OPERATOR_BUILDER_AI=0)"
    monkeypatch.setenv("OPERATOR_BUILDER_AI", "1")
    from operator_ai import settings

    settings.set_box(demo_tenant, "ai_token_budget", 1000, "test")
    monkeypatch.setenv("OPERATOR_DISABLE_LLM", "0")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-1234567890")
    demo_tenant.set(dp._cap_key(), 200)
    assert dp.ai_reason(demo_tenant).startswith("daily cap of 200")


def test_support_escalates_refund_and_legal(demo_tenant):
    gen = dp.generate(dp.PRESETS["bookkeeping"])
    bid = dp.save_build(demo_tenant, "bookkeeping", gen["business"], gen)
    r = dp.run_agent(demo_tenant, bid, "support_agent", background=False)
    esc = [c for c in r["cards"] if c["escalated"]]
    assert len(esc) == 2 and all(queue.get(demo_tenant, c["queue_id"])["status"] == "needs_human" for c in esc)


def test_analyst_labels_every_dollar_demo(demo_tenant):
    import re

    gen = dp.generate(dp.PRESETS["gym"])
    bid = dp.save_build(demo_tenant, "gym", gen["business"], gen)
    r = dp.run_agent(demo_tenant, bid, "ad_performance_analyst", background=False)
    for c in r["cards"]:
        for m in re.finditer(r"\$[\d,.]+", c["draft"]):
            assert c["draft"][m.end():m.end() + 7] == " (DEMO)"


def test_start_over_clears_undecided_demo_drafts(demo_tenant):
    gen = dp.generate(dp.PRESETS["physio"])
    bid = dp.save_build(demo_tenant, "physio", gen["business"], gen)
    r = dp.run_agent(demo_tenant, bid, "dm_setter", background=False)
    out = dp.start_over(demo_tenant, bid)
    assert out["cleared_drafts"] == 3 and all(queue.get(demo_tenant, q) is None for q in r["queue_ids"])


def test_reset_builder_refused_on_real(tenant):
    with pytest.raises(PermissionError):
        dp.reset_all(tenant)


def test_headless_click_through_three_times(tmp_path):
    from playwright.sync_api import sync_playwright

    from scripts.builder_demo_click import click_through
    from tests.server import running_server

    with running_server(tmp_path) as base, sync_playwright() as p:
        browser = p.chromium.launch()
        results = []
        for _ in range(3):
            ctx = browser.new_context(viewport={"width": 1440, "height": 900})
            results.append(click_through(ctx.new_page(), base, bonus=True))
            ctx.close()
        browser.close()
    assert results == [[], [], []], results
