"""Fictional, speaker-labelled group-call transcripts for the pitch demo only (labelled DEMO, never indexed, never
written to the real workspace). They give the demo persona something real to mine: one teacher with consistent
phrasing, and students who talk differently."""
from __future__ import annotations

import random

from .. import corpus

DEMO_NAME = "Avery Lumen (DEMO)"
DEMO_ALIASES = ["Avery Lumen", "Lumen"]
SPEAKER = "Avery Lumen (DEMO)"
STUDENTS = ["Student Maya (DEMO)", "Student Theo (DEMO)", "Student Ines (DEMO)", "Student Rowan (DEMO)",
            "Student Hana (DEMO)", "Student Dev (DEMO)"]

OPENERS = ["Right, let's keep this simple.", "Okay, here's the honest bit.", "Good question, so let's slow down.",
           "Right, let's keep this simple.", "Okay, here's the honest bit."]
CLOSERS = ["Small reps, every day.", "Fair?", "Go do the reps.", "Small reps, every day.", "Fair?"]
TRANSITIONS = ["Here's the honest bit:", "And this is the part people skip:", "So what does that mean for you?"]
SIGNATURE = ["keep it boring and repeatable", "that's the whole game", "you earn the right to ask"]
TEACH = [
    "The goal of the first message is a real conversation, not a sale.",
    "Ask one clear question and then actually listen to the answer.",
    "If the opener could be sent to anyone, it isn't good enough yet.",
    "Write down what they told you before you reply, so your reply proves you listened.",
    "Your follow-up has to add something useful, a tip or a short resource.",
    "Qualify before you send the booking link, every single time.",
    "A booked call that doesn't show is just a nice feeling, it isn't progress.",
    "Confirm the booking quickly so they know a real person is on the other side.",
    "Send reminders that give value, never a reminder that only says don't forget.",
    "When someone goes quiet, be kind and offer them a simple next step.",
    "On the call, let them describe the gap in their own words before you present anything.",
    "If they say they need to think about it, ask what specifically they want to think through.",
    "Never discount on the call to get a yes, because it teaches them your price isn't real.",
    "Track every message you send, otherwise you are guessing about what works.",
    "Pick a niche you can actually reach and that already spends money on the problem.",
    "Your offer should say who you help, what changes for them and how you do it.",
    "Results you share must be real and you need permission to share them.",
    "Don't plant comments or pretend to be someone else, you lose trust you can't buy back.",
    "Consistency beats intensity, so plan the work you can repeat on a bad week.",
    "When the numbers are bad, find the one stage that leaks the most and fix that first.",
    "Read your message out loud before you send it, and cut anything you wouldn't say to a friend.",
    "A clear no is useful, it frees you up for the people who are a real fit.",
    "Show up to the coaching call with your numbers, not just your feelings about the week.",
    "If a reply rate drops, change one thing at a time so you know what moved it.",
    "Spend the first part of the week on outreach before you touch anything else.",
    "Be specific in your follow-up, mention the thing they actually said.",
    "Nobody owes you a reply, so make the message worth replying to.",
    "Keep your calendar honest and protect the time you blocked for outreach.",
]
STUDENT_LINES = [
    "I was wondering how many messages I should be sending each day?",
    "What do you think about sending a voice note instead of text?",
    "I'm not sure my offer is clear enough, can you look at it?",
    "My reply rate dropped this week and I don't really know why.",
    "Should I follow up if they've seen the message but not replied?",
    "I feel like I'm doing everything and nothing is working, honestly.",
    "How do I handle someone who says they need to ask their partner?",
    "Is it okay to send the booking link in the first reply?",
    "I had two no-shows this week, what should I have done differently?",
    "Can I talk about results from my old job in my outreach?",
]


def _turn(rng: random.Random) -> str:
    parts = []
    if rng.random() < 0.7:
        parts.append(rng.choice(OPENERS))
    body = rng.sample(TEACH, k=rng.randint(2, 4))
    if rng.random() < 0.45:
        body.insert(rng.randint(1, len(body)), rng.choice(TRANSITIONS))
    if rng.random() < 0.4:
        sig = rng.choice(SIGNATURE)
        body.append(sig[0].upper() + sig[1:] + ".")
    parts += body
    if rng.random() < 0.6:
        parts.append(rng.choice(CLOSERS))
    return " ".join(parts)


CO_HOST = "Co-host Sam (DEMO)"
CO_OPENERS = ["Let me add one thing.", "To build on that.", "One more point on this.", "Good point."]
CO_CLOSERS = ["Hope that helps.", "Any questions on that?", "That's my take."]


def _co_turn(rng: random.Random) -> str:
    """The co-host teaches the same topics in a different voice: the topic-matched background for mining."""
    parts = [rng.choice(CO_OPENERS)] if rng.random() < 0.6 else []
    parts += rng.sample(TEACH, k=rng.randint(2, 4))
    if rng.random() < 0.5:
        parts.append(rng.choice(CO_CLOSERS))
    return " ".join(parts)


def transcript(n: int) -> list[str]:
    rng = random.Random(1000 + n)
    lines = [f"[{SPEAKER}] 00:00:0{n % 10}", "Welcome back everyone, this is our group call (DEMO).", ""]
    for i in range(18):
        st = rng.choice(STUDENTS)
        lines += [f"{st}: {rng.choice(STUDENT_LINES)}", f"{SPEAKER}: {_turn(rng)}"]
        if rng.random() < 0.7:
            lines.append(f"{CO_HOST}: {_co_turn(rng)}")
        if i % 6 == 5:
            lines.append("")
    return lines


def docs(n_calls: int = 14) -> list[corpus.Doc]:
    out = []
    for n in range(1, n_calls + 1):
        lines = transcript(n)
        out.append(corpus.Doc(file=f"demo/persona/group_call_{n:02d}.txt", kind="transcript",
                              title=f"Group call {n} (DEMO)", lines=lines, ts=[None] * len(lines), quality="ok",
                              licence_status="course", provenance="owner_confirmed", corpus_id="demo-persona"))
    return out
