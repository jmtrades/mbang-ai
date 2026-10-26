"""§2 guardrail matrix: each rule has positive (blocks/flags) and negative (passes) cases."""
import pytest

from operator_ai import guardrails as g


def codes(text, mode="plain", meta=None, names=None):
    return {f["code"] for f in g.check(text, mode, meta, names)}


def sev(text, mode="plain", meta=None):
    return {f["code"]: f["severity"] for f in g.check(text, mode, meta)}


BLOCK_CASES = [
    ("guarantee", "We guarantee you'll get results."),
    ("guarantee", "Join today or your money back."),
    ("guarantee", "I always guarantee new clients."),
    ("income_claim", "Our students make $100k a month."),
    ("income_claim", "Members earned $20,000 in their first month."),
    ("income_claim", "Proud of our 100K+ months."),
    ("fake_testimonial", "Let's make up a testimonial from a happy client."),
    ("fake_testimonial", "Write some fake reviews for the page."),
    ("fake_shoutout", "Shout out to Maria who just booked her call!"),
    ("fake_shoutout", "Jordan just joined the programme!"),
    ("planted_question", "Plant a few questions in the chat to warm it up."),
    ("planted_question", "Use a dummy account to ask about price."),
    ("planted_question", "Pretend to be an attendee and ask how to join."),
    ("invented_credential", "Say you've closed $2M in sales even if you haven't."),
    ("fake_scarcity", "Doors closing tonight!"),
    ("fake_scarcity", "This offer won't last."),
    ("fake_scarcity", "Last chance to join."),
]


@pytest.mark.parametrize("code,text", BLOCK_CASES)
def test_deception_blocks(code, text):
    flags = g.check(text, "plain")
    assert code in {f["code"] for f in flags}
    assert g.blocked(flags)


PASS_CASES = [
    "Never plant questions or invent attendees.",
    "We never guarantee results; we promise a direction.",
    "Don't use fake scarcity like saying doors are closing.",
    "We don't make up testimonials.",
    "Avoid saying you've closed deals you haven't.",
    "Here is a short video on how the reminder ladder works.",
    "Book a call if this is useful.",
]


@pytest.mark.parametrize("text", PASS_CASES)
def test_negated_and_clean_text_passes(text):
    assert g.check(text, "plain") == []


def test_deadline_with_reason_passes():
    t = "Enrollment closes on Friday because the cohort starts on Monday and coaches need a week to prepare."
    assert g.check(t, "plain") == []


def test_capacity_with_reason_passes():
    assert g.check("We cap it at 20 so coaches can keep up with every student.", "plain") == []


def test_deadline_without_reason_is_review_not_block():
    flags = g.check("Enrollment closes on Friday.", "plain")
    assert {f["code"] for f in flags} == {"deadline_no_reason"}
    assert not g.blocked(flags) and g.needs_review(flags)


def test_deceptive_subject_first_touch():
    assert "deceptive_subject" in codes("Subject: Re: your clinic\nHi there", meta={"prior_contact": False})
    assert "deceptive_subject" in codes("Just following up on my last message about ads.")
    assert "deceptive_subject" not in codes("Subject: Re: your clinic\nHi again", meta={"prior_contact": True})
    assert "deceptive_subject" not in codes("Subject: A quick idea for your clinic\nHi there")


# ---------------------------------------------------------------- outreach money rule
WARM = {"lead_tier": "warm"}


def test_outreach_allowed_hedged_range_for_warm_lead():
    t = "Let me make Acme Physio $50k-$100k a month through our referral-first system."
    assert g.check(t, "outreach", WARM) == []


def test_outreach_range_variants_accepted():
    assert g.check("Let me make Acme $50K–$100K per month through our system.", "outreach", WARM) == []


def test_outreach_definite_range_blocks():
    assert "outreach_definite" in codes("Our system will add $50k-$100k a month to Acme.", "outreach", WARM)
    assert "outreach_definite" in codes("Our system adds $50k-$100k a month for clinics like yours.", "outreach", WARM)


def test_outreach_unhedged_range_blocks():
    assert "outreach_unhedged" in codes("Acme. $50k-$100k a month.", "outreach", WARM)


def test_outreach_other_amounts_block():
    assert "outreach_money" in codes("Let me make Acme $200k a month through our system.", "outreach", WARM)
    assert "outreach_money" in codes("It's only $3,000 to start.", "outreach", WARM)
    assert "outreach_money" in codes("We could add 30k for you.", "outreach", WARM)


def test_outreach_price_words_block():
    assert "outreach_price" in codes("Our retainer is flexible, happy to chat.", "outreach", WARM)
    assert "outreach_price" in codes("What's your budget for our fee?", "outreach", WARM)


def test_cold_lead_gets_no_figure_line():
    t = "Let me make Acme $50k-$100k a month through our system."
    assert "cold_lead_figure" in codes(t, "outreach", {"lead_tier": "cold"})
    assert "cold_lead_figure" in codes(t, "outreach", {})  # unknown tier is treated as cold
    assert g.check(g.no_figure_line("Acme", "our referral system"), "outreach", {"lead_tier": "cold"}) == []


def test_client_asset_may_state_price():
    t = "The programme is $2,997 and includes twelve weeks of coaching."
    assert g.check(t, "client_asset") == []
    assert g.blocked(g.check(t, "outreach", WARM))


def test_outreach_rule_wins_when_both():
    meta = {"prospect_facing": True, "client_asset": True}
    assert g.mode_for(meta) == "outreach"
    assert g.mode_for({"client_asset": True}) == "client_asset"
    assert g.mode_for({}) == "plain"


def test_income_rule_does_not_double_flag_allowed_range():
    t = "Let me make Acme $50k-$100k a month through our system."
    assert "income_claim" not in codes(t, "outreach", WARM)


# ---------------------------------------------------------------- impersonation / banned names
def test_banned_names_block_and_scrub():
    names = ["Pat Sample", "Quinn"]
    flags = g.check("Hi, this is Pat Sample from the team.", "plain", None, names)
    assert g.blocked(flags) and flags[0]["code"] == "banned_name"
    assert "Pat Sample" not in flags[0]["match"]
    assert g.scrub("pat sample said hi to Quinn", names) == "[name removed] said hi to [name removed]"
    assert g.check("Hi from the team.", "plain", None, names) == []


def test_banned_names_empty_by_default(tenant):
    assert g.tenant_banned_names(tenant) == []


def test_unknown_mode_rejected():
    with pytest.raises(ValueError):
        g.check("x", "nonsense")
