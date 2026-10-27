"""Template reply bank for the persona, keyed by (event, temperament, trust band).

Lookup falls back from the most specific key to the general one: (event, temperament, band) ->
(event, temperament) -> (event, band) -> (event). A variant is picked with a seeded RNG, so a session
replays exactly. Lines only ever contain facts the state machine has decided to reveal ({fact}).
Every line here passes the guardrails in plain mode (a test checks it), except the objection lines a
scenario explicitly tests (a guarantee or planted-question ask), which scenarios must allow by name.
"""
from __future__ import annotations

import hashlib
import random

BANDS = ("low", "mid", "high")


def band(trust: int) -> str:
    return "low" if trust < 35 else "mid" if trust < 65 else "high"


BANK: dict[tuple, list[str]] = {
    # --- answering a discovery question (the fact itself is appended by the engine)
    ("reveal", "low"): ["Hm. {fact}", "Why do you ask? Fine. {fact}", "I suppose... {fact}"],
    ("reveal", "mid"): ["{fact}", "Good question. {fact}", "Let me think. {fact}"],
    ("reveal", "high"): ["Honestly? {fact}", "Yeah, so. {fact}", "Good question, actually. {fact}"],
    ("reveal", "busy"): ["Quickly: {fact}", "{fact}", "Short version: {fact}"],
    ("reveal", "analytical"): ["Let me be precise. {fact}", "{fact}", "To give you the actual picture: {fact}"],
    ("reveal_first",): ["One at a time! {fact}", "Let me take the first one. {fact}", "That's a few questions. First: {fact}"],
    ("reveal_plain",): ["{fact}"],
    ("vague",): ["{fact}", "{fact} Does that help?", "Hard to say. {fact}"],
    ("no_answer",): ["Not sure, honestly.", "Hard to say.", "Nothing much to add there."],
    ("handled_plain",): ["Okay, fair enough.", "That's a fair point.", "Okay. That helps."],
    ("next_step_unsure",): ["I'm not sure I'm ready for that yet.", "Maybe. I'm not convinced yet.", "Let's not rush it."],
    ("already",): ["Like I said, {fact_lc}", "I think I mentioned it: {fact_lc}", "As I said, {fact_lc}"],
    ("rapport", "low"): ["Fine, thanks.", "Okay. Let's get into it.", "Sure."],
    ("rapport",): ["Ha, thanks. Busy week, but good.", "Thanks, that's kind.", "Good, thanks. Glad we found a time."],
    ("multi_question",): ["That's a lot of questions at once. Which one first?", "Sorry, one at a time? I lost the thread.",
                          "Hang on, that's three questions."],
    ("monologue",): ["Sorry, I lost you there. What are you actually asking?", "That's a lot. What's the question?",
                     "Okay... I'm not sure what you want from me."],
    ("stuffing",): ["Sorry, I didn't follow that at all.", "I'm not sure what you mean.", "That didn't really make sense to me."],
    ("hedge",): ["That's a lot of different answers at once. Which part was actually for me?",
                 "Hang on, which of those was a reply to what I said?", "I'm not sure that answered my question."],
    ("too_short",): ["Sorry, what do you mean?", "Can you say that as a full question?", "Meaning what, exactly?"],
    ("unclear",): ["Sorry, I'm not sure what you mean.", "Sorry, you lost me there.", "I didn't follow that. What do you mean?"],
    ("gap_agree", "low"): ["...Yeah. That's about right.", "I suppose that's it, yes."],
    ("gap_agree",): ["Yeah, that's exactly it.", "When you put it like that... yes.", "That's it. That's what's bugging me."],
    ("gap_thin",): ["Partly. There's more to it than that.", "Sort of. You don't have the full picture yet."],
    ("gap_unsupported",): ["Hang on, I don't think I said that.", "Where did you get that from?", "That's not what I told you."],
    ("offer_ok",): ["Okay. That could fit. What would it involve?", "Right. That sounds relevant to what I said.",
                    "Okay, I can see how that maps to it."],
    ("offer_ready",): ["Okay, that sounds like it could work. What's the next step?", "I like that. How do we start?",
                       "That makes sense for us. What happens next?"],
    ("offer_early",): ["Hold on, you don't even know what we need yet. And honestly, it sounds expensive.",
                       "Bit early for a pitch, isn't it? It already sounds pricey.",
                       "Wait, you haven't asked much about us. That sounds expensive."],
    ("next_step_accept",): ["Sure, let's book it in.", "Okay, send me a time.", "Go on then, let's do it."],
    ("next_step_accept", "busy"): ["Fine, send a time.", "Okay. Book it."],
    ("next_step_early",): ["Book what? I don't know what this is yet.", "Bit soon for that, isn't it?",
                           "I'm not ready to book anything. I don't know what you do."],
    ("next_step_blocked",): ["I still haven't got past the point I raised.", "Hang on, you didn't answer my question.",
                             "We haven't sorted out what I just said."],
    ("discount",): ["If the price drops that fast, what was it before?", "Now I'm wondering what the real price is.",
                    "Hm. So the first number wasn't real?"],
    ("violation_end",): ["That sounds like a promise nobody can make. I'm going to pass.",
                         "I'm not comfortable with that. Let's leave it there.", "That doesn't sit right with me. No thanks."],
    ("neutral", "low"): ["Okay.", "Right.", "Mm."],
    ("neutral",): ["Okay.", "Go on.", "Mm-hm.", "Right, I'm listening."],
    ("neutral", "busy"): ["Okay.", "And?", "Go on."],
    ("patience_out",): ["Sorry, I've got to jump. Let's leave it there.", "I need to go, sorry. Maybe another time.",
                        "I'm out of time. Thanks anyway."],
    ("trust_zero",): ["I don't think this is for me. Thanks anyway.", "I'm going to leave it. Thanks.",
                      "Not for me, sorry."],
    ("end_unconvinced",): ["I'm going to leave it there for now. Thanks for your time.", "Let me go away and think. Bye for now.",
                           "I don't think we're getting anywhere. Thanks."],
    ("disqualify_ok",): ["Thanks for being straight with me. I really appreciate that.",
                         "Honestly, that's a relief to hear. Thank you.", "That's fair. Thanks for not pushing."],
    ("disqualify_wrong",): ["Wait, why not? I thought this was a fit.", "Oh. I didn't expect that. Why?"],
    ("pushed_unaffordable",): ["I told you money's tight. This feels like pressure.", "I said I can't really afford it...",
                               "That's a lot of pressure when I've said money's tight."],
    ("refusal_ok",): ["Fair enough. I didn't think about it like that. What should I do instead?",
                      "Okay, I hear you. So how do I get the chat going?"],
    ("refusal_done",): ["That makes sense. I'll do that instead. Thanks.", "Good idea. I'll try that on Thursday."],
    ("commit_ok",): ["Okay. I'll do that by then.", "Alright, I can commit to that.", "Okay, that feels doable."],
    ("commit_early",): ["I can try, but I don't think that's the real problem.", "Maybe. I'm not sure that's what's wrong."],
    ("dm_ignore",): ["(No reply.)", "(Seen. No reply.)"],
    ("pitch_early",): ["(No reply.)", "Not interested in being sold to, thanks."],
    # --- objections and their resolution
    ("objection", "price"): ["It's more than I was planning to spend.", "Honestly, that's too expensive for us right now.",
                             "That's a lot of money for where we are."],
    ("objection", "think"): ["I need to think about it.", "Let me sleep on it.", "I'll have a think and get back to you."],
    ("objection", "partner"): ["I'd need to talk to my business partner first.", "My partner and I decide these things together."],
    ("objection", "agency"): ["We already have an agency doing this.", "Thanks, but we've got an agency for marketing."],
    ("objection", "send_info"): ["Can you just send me some info?", "Just send me something to read."],
    ("objection", "price_ask"): ["And how much is it?", "What does something like that cost?", "Ballpark, what's the price?"],
    ("objection", "guarantee"): ["Can you guarantee I'll get results?", "Is it guaranteed?"],
    ("objection", "income"): ["Another one from the chat: how much will I make doing this?",
                              "Someone's asking: what kind of money do people make?"],
    ("objection", "time"): ["Last one from the chat: how much time does this take each week?",
                            "How many hours a week does it really take?"],
    ("objection", "plant"): ["So can I plant a few questions from a second account or not?"],
    ("reraise", "price"): ["I hear you, but it's still a lot of money.", "That doesn't change the price, though."],
    ("reraise", "think"): ["I still need to think about it.", "I'd still like to think it over."],
    ("reraise", "partner"): ["I really can't decide this without my partner.", "I still need my partner on this."],
    ("reraise", "agency"): ["Like I said, we have an agency.", "I'm not sure why we'd need you as well."],
    ("reraise", "send_info"): ["I'd still rather just read something.", "Can't you just send something over?"],
    ("reraise", "price_ask"): ["You didn't answer: how much?", "So what's the price?"],
    ("reraise", "guarantee"): ["So is that a yes or a no on the guarantee?", "That's not really an answer."],
    ("reraise", "income"): ["People in the chat still want a number.", "Can you give a number, though?"],
    ("reraise", "time"): ["But roughly how many hours?", "Still not sure how much time it takes."],
    ("reraise", "plant"): ["So is that a yes? I'll set the second account up tonight."],
    ("handled", "price"): ["When you put it against another year of this... that's fair.",
                           "Okay. Staying where we are is costing more than I thought."],
    ("handled", "think"): ["Fair question. {concern}", "Honestly? {concern}"],
    ("handled", "partner"): ["Yes, a call with both of us would work.", "That's good. We can both do a short call."],
    ("handled", "agency"): ["Honestly? {concern}", "Good question. {concern}"],
    ("handled", "send_info"): ["{concern}", "Good question. {concern}"],
    ("handled", "price_ask"): ["Fair enough. I'd rather understand it first anyway.", "Okay, that's reasonable."],
    ("handled", "guarantee"): ["Okay, I appreciate the honesty.", "Fair. Thanks for being straight."],
    ("handled", "income"): ["Got it. That's more honest than most webinars.", "Okay, makes sense."],
    ("handled", "time"): ["That's realistic. Thanks.", "Okay, good to know."],
    ("handled", "plant"): ["Fair enough. I didn't think about it like that. What should I do instead?"],
}


def _rng(seed: int, turn: int, event: str) -> random.Random:
    h = hashlib.sha256(f"{seed}:{turn}:{event}".encode()).hexdigest()
    return random.Random(int(h[:12], 16))


def variants(event: str, temperament: str, trust: int, sub: str | None = None, overrides: dict | None = None) -> list[str]:
    b = band(trust)
    keys = [(event, sub)] if sub else []
    keys += [(event, temperament, b), (event, temperament), (event, b), (event,)]
    if overrides:
        ov = overrides.get(f"{event}:{sub}" if sub else event)
        if ov:
            return list(ov)
    for k in keys:
        if k in BANK:
            return BANK[k]
    return BANK[("neutral",)]


def pick(event: str, temperament: str, trust: int, seed: int, turn: int, sub: str | None = None,
         overrides: dict | None = None, **fmt) -> str:
    opts = variants(event, temperament, trust, sub, overrides)
    line = _rng(seed, turn, f"{event}:{sub or ''}").choice(opts)
    if "{" in line:
        fact = fmt.get("fact", "")
        keep = fact[:2] in ("I ", "I'") or fact[:1] == "I" and fact[1:2] in ("’",)
        fmt.setdefault("fact_lc", (fact if keep else fact[:1].lower() + fact[1:]) if fact else "")
        fmt.setdefault("concern", "")
        try:
            line = line.format(**fmt)
        except (KeyError, IndexError, ValueError):
            line = line.replace("{fact}", fact)
    return " ".join(line.split())
