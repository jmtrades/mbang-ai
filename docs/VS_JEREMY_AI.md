# Operator AI vs Jeremy AI — critical comparison

Sources for Jeremy AI are its own marketing pages (jeremyhaynesai.com and the Utari walkthrough on jeremyhaynes.com),
read on 2026-10-26. I could not use the product, so its claims are taken at face value and marked as such. "Better"
below means better for a coaching / growth-operator programme owner who wants the AI to do the work safely under
their own brand.

## What Jeremy AI says it does

- A clone trained on 11,400+ hours of Zoom calls, 85,000+ DMs/emails, 400+ SOPs, 1,200+ ad account audits.
- One chat on Telegram, web or SMS: paste numbers, screenshots, scripts or ad copy and get an answer.
- A phone number that answers in a cloned voice with low latency; team seats and group chats.
- A Google Drive folder that syncs daily; agentic "workers" with triggers (e.g. audit a call when a transcript lands).
- Integrations (CRMs, Slack, Gmail, Stripe, ad platforms). $1,000/month, by application; 400,000+ messages handled.

## Head to head

| Capability | Jeremy AI (claimed) | Operator AI (built, tested) | Verdict |
|---|---|---|---|
| Size of the brain | 11,400 h + 85k DMs + 400 SOPs | Whatever the owner loads; benchmarked at 577k words (3,114 passages, ~11 ms search) | **Theirs today** — depends entirely on the material you load |
| Answer quality | LLM in the creator's voice | With an AI key: grounded LLM answers; without: cited bullet-point extracts | **Theirs** until an AI key + budget are set; ours never needs a key to work |
| Grounding & honesty | Not documented | Every claim cited to file + line, source opens at the line, invented citations stripped, honest "not covered" (12/12) | **Ours** |
| One chat for questions / numbers / scripts / screenshots | Yes | Yes — routes to coach, diagnostic, reviewer or Ads audit; screenshots read by local OCR; Ads Manager CSV audited | **Even**, ours shows its working (trace, citations, money impact) |
| Follow-up questions | Presumably | Follow-ups read in the context of the previous question | Even |
| Numbers → diagnosis | Yes | Pure-maths bottleneck with money cost per stage, cited benchmarks, next best action | **Ours** (transparent maths) |
| Ad account audits | 1,200+ audits in training | Kill/winner rules on a real Ads Manager export (read-only); mock account for practice; real Meta client built, off | Even; theirs has more history, ours shows the rule behind each verdict |
| Doing the work | Advice | Drafts DMs, follow-ups, invites, reminders, webinar chain, 7 Studio lines, builder agents — all into an approval queue | **Ours** |
| Safety | Not documented | Approval queue, guardrails (fake proof, income claims, scarcity, money rule), never-invite list, PAUSED-only ads, rollback, audit log | **Ours** |
| Voice | Phone number, cloned voice | Browser dictation + read-aloud; a phone Q&A line (Twilio-style) in a neutral voice, off by default | **Theirs** on voice cloning — deliberately: ours never clones a real person's voice |
| Channels | Telegram, web, SMS | Web, Telegram, Slack, SMS, phone, email (real senders built, off until the owner switches them on) | Ours wider; neither verified live here |
| Team mode | Seats + group chat | 5 roles with an RBAC matrix, per-role tabs, coach/setter/closer queues, Telegram groups | **Ours** |
| Students | Not offered | Onboarding, check-ins, risk scores over 2,000+ students, nudges, Retention Star | **Ours** |
| Daily knowledge sync | Google Drive daily | Daily sync of a Drive-for-desktop folder, content-policy screen, staged for the owner's OK | Even (ours adds screening + approval) |
| White-label / resale | Single creator | Multi-tenant, client workspaces, course KB licence-locked to the owner | **Ours** |
| Works with AI off | No | Yes, every feature, labelled | **Ours** |
| Price | $1,000/month | Self-hosted; AI cost only if you switch it on | **Ours** |

## Where we are honestly behind, and what closes it

1. **The brain.** Jeremy AI is trained on ten years of material. Ours is only as deep as `inputs/`. Copy the full course,
   SOPs and (with consent) DM and call archives into `inputs/` — the index scales (577k words benchmarked).
2. **Conversational quality.** Without an AI key our answers are cited extracts. Add `ANTHROPIC_API_KEY` and an
   `ai_token_budget` (server CLI) and the same pipeline answers in full sentences, still grounded and cited.
3. **Cloned voice.** Not built on purpose (no impersonation). A consented persona voice could be added later.
4. **Real-world proof.** They report 400,000+ messages. Ours has never been switched on against real channels.

"A billion times better" can't be verified. What can be verified: more of the work is done, every answer can be
checked against its source, nothing goes out without approval, and every item above has a passing test.
