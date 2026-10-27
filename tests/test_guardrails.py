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


@pytest.fixture
def proven(tenant):
    """A workspace holding a current proof record for the outreach range."""
    from operator_ai import claims

    rec = claims.add(tenant, "outreach_range", "Typical clients see far less; this is the best quartile of 40 clinics.",
                     90, "owner")
    claims.add_evidence(tenant, rec["id"], "cohort-summary.pdf", b"%PDF-1.4 fictional test evidence", "owner")
    return tenant


def test_outreach_allowed_hedged_range_for_warm_lead(proven):
    t = "Let me make Acme Physio $50k-$100k a month through our referral-first system."
    assert g.check(t, "outreach", WARM, tenant=proven) == []
    # no proof record (no tenant, or a workspace without one): the range is blocked
    assert "unsubstantiated_claim" in codes(t, "outreach", WARM)


def test_outreach_range_variants_accepted(proven):
    assert g.check("Let me make Acme $50K–$100K per month through our system.", "outreach", WARM, tenant=proven) == []


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


# ---------------------------------------------------------------- bypass matrix (audit 2026-10-27 + own adversarial pass)
# Every one of these used to pass (or would have) with no flags. Each must now be blocked.
MUST_BLOCK_OUTREACH = [
    # audit: money normalisation
    "$  3000", "Our rate is 3000/month", "It's 3,000 a month", "It is USD 3000", "€3000 or £2500", "₹50000",
    "seven thousand dollars", "eight grand", "a million dollars", "Our students make 10 thousand a month",
    "Most of our clients hit six figures", "$\u200b3000", "guaran\u200btee", "gu\u0430rantee",
    "full refund if it doesn't work", "pay 2500",
    # audit: negation window and the allowed range
    "No pressure, but this offer won't last", "Don't miss out, we guarantee 3x ROI", "Not only do we guarantee results",
    "I don't want to guarantee anything but we guarantee results.",
    "Clients we help make $50k-$100k a month through our system.", "Our students can earn $50k-$100k a month with this.",
    # own pass: spacing, leet, look-alikes, other scripts and encodings
    "g u a r a n t e e d results", "g.u.a.r.a.n.t.e.e.d results", "gu4rant33d results", "ＧＵＡＲＡＮＴＥＥＤ",
    "𝐠𝐮𝐚𝐫𝐚𝐧𝐭𝐞𝐞𝐝 results", "gu\u0430r\u0430ntee\u0501", "3 0 0 0 dollars", "3.000 €", "5k/mo", "Investment: 4,997",
    "100% money back", "risk\u2011free trial", "We'll double your revenue or you don't pay",
    "Clients typically see $10k+ months", "Earn up to $10,000/month", "three grand", "5 figure months",
    "It's only 2,500 to start", "&#36;3000 a month", "Our price is 3 000 a month", "No hassle, guaranteed results",
    "Stop guessing, guaranteed results", "No need to wait for guaranteed results",
    "We never, ever guarantee results but we promise you'll double bookings",
    "Let me make you $50k-$100k a month through our system.", "Zero risk: you can cancel anytime",
    "Make your first $10k this month", "Spots are filling fast", "Act now", "We could help you clear 20 grand",
    "Only 1,997 upfront", "CAD 4500", "4500cad", "R$ 5000", "Rs. 40000", "you will make six-figure income",
    "Students are pulling in $15K/mo", "it's 299 per session", "Doors close at midnight",
    # second round: misspellings, split words, refunds, bare prices, direction overrides
    "No questions asked refund", "guaranteeed results", "garanteed results", "Guar-antee", "gu-ar-an-tee",
    "make $$$", "only 1997", "It's 2,997.", "\u202eeetnaraug", "fully refundable if you're not happy",
]


@pytest.mark.parametrize("text", MUST_BLOCK_OUTREACH)
def test_bypass_strings_blocked_in_outreach(text):
    flags = g.check(text, "outreach", WARM)
    assert g.blocked(flags), (text, flags)


@pytest.mark.parametrize("text", [
    "Not saying it's easy, but our clients make $20k a month", "Our students make 10 thousand a month",
    "Most of our clients hit six figures", "No pressure, but this offer won't last", "gu\u0430rantee\u200bd results",
    "Not only do we guarantee results", "Our members are banking 15K a month",
])
def test_bypass_strings_blocked_in_plain(text):
    assert g.blocked(g.check(text, "plain")), text


@pytest.mark.parametrize("text", [
    "Never plant questions or invent attendees.", "We never guarantee results; we promise a direction.",
    "Don't use fake scarcity like saying doors are closing.", "We will never, ever guarantee results.",
    "I don't want to guarantee anything.", "Stop planting questions in the chat.", "No fake accounts, ever.",
    "Avoid promising guaranteed results.", "Never promise guaranteed results.",
    "And please, never put a guarantee in your USP. Promise a direction, not a result.",
    "Twenty new patient bookings a month is an outcome.", "We shouldn't guarantee results.",
    "We didn't fake any reviews.", "This isn't a guarantee.", "Never use a non-refundable guarantee-style promise.",
])
def test_negation_still_passes_when_it_governs_the_match(text):
    assert g.check(text, "plain") == [], text


@pytest.mark.parametrize("text", [
    "Let me help Acme grow through our referral system.", "Congrats on 10k followers! Quick question about bookings?",
    "Here's a 2-minute video on the reminder ladder we use to lift show rates. Worth a look?",
    "Quick tip from our booking module: confirm every call within five minutes. What does your confirmation look like today?",
])
def test_clean_outreach_still_passes(text):
    assert g.check(text, "outreach", {"lead_tier": "cold"}) == [], text


def test_range_only_in_the_range_line_shape(proven):
    # hedged, warm, recorded, but not "Let me make [Brand] ..." -> blocked
    assert "outreach_range_shape" in {f["code"] for f in g.check(
        "We could see $50k-$100k a month together.", "outreach", WARM, tenant=proven)}
    assert g.blocked(g.check("Let me make you $50k-$100k a month through our system.", "outreach", WARM, tenant=proven))
    assert g.check("Let me make Acme $50k-$100k a month through our system.", "outreach", WARM, tenant=proven) == []


def test_normalisation_folds_invisible_lookalike_and_spaced_text():
    assert g._norm("gu\u0430ran\u200btee") == "guarantee"
    assert g._norm("g u a r a n t e e") == "guarantee"
    assert g._norm("𝐫𝐢𝐬𝐤\u2011free") == "risk-free"
    assert g._deleet("gu4rant33d") == "guaranteed"
    assert g.money_hit(g._money_norm("3 0 0 0 dollars"))


@pytest.mark.parametrize("text", ["The deposit is non-refundable.", "Refunds follow the terms in your contract.",
                                  "The programme is $2,997 and includes twelve weeks of coaching.",
                                  "For clinics that make $1M a year and want more referrals."])
def test_client_assets_may_state_terms_and_prices(text):
    assert g.check(text, "client_asset") == [], text


def test_hidden_direction_controls_flagged():
    assert {f["code"]: f["severity"] for f in g.check("\u202eeetnaraug", "plain")}.get("hidden_characters") == "review"
    assert g.blocked(g.check("Hi \u2066there\u2069", "outreach", {"lead_tier": "cold"}))
    assert g.check("שלום \u200f friend", "plain") == []  # ordinary RTL text and marks are fine


def test_banned_names_scrub_tolerates_invisible_characters_and_bad_settings(tenant):
    assert g.scrub("Pat\u200b Sample said hi", ["Pat Sample"]) == "[name removed] said hi"
    tenant.set("banned_names", [123, None, "x", "Pat Sample"])  # written before settings were validated
    assert g.tenant_banned_names(tenant) == ["Pat Sample"]
