# Demo script

Start: `scripts/run.sh`, open `http://127.0.0.1:8850/t/demo/`. Everything in the pitch demo is fictional and labelled DEMO.

## The 90-second builder demo (§11)

| # | Click | On screen |
|---|---|---|
| 1 | Sign-in → **Pitch demo** → **Owner** | Sidebar; bottom-left "Owner (DEMO) · PITCH DEMO" |
| 2 | Sidebar → **AI departments** (`/#/builder`) | "Build a business's AI team"; 3 fictional businesses marked DEMO + "Your own business" |
| 3 | **Kinetic Physio Coaching** → **Generate departments** | "5 departments · 10 agents" + "rules, no AI call"; five cards each with a KPI and 2 agents (< 1 s) |
| 4 | Sales follow-up & DM setter → **DM setter** | Produces, trigger, KPI, guardrails (No price in DMs, One question per message, Everything goes to approval), instructions, 3 DEMO inbound DMs |
| 5 | **Run on demo data** | 5 steps tick (load input → rules draft → AI pass → review → queue); "3 drafts · 3 pass review · Sent: 0"; three cards with AI/Rules badge, score, Queue # |
| 6 | **Review in the queue** | The "Reply to … (DEMO)" drafts, pending approval |
| 7 | Browser back → **Start over** | Back to the business picker |
| Bonus | LedgerLeap → **Support agent** · IronHaus → **Ad performance analyst** | Refund and legal tickets "Escalated to a person"; every $ figure labelled DEMO |

Headless check: `scripts/builder_demo_click.py --times 3` → `CLEAN 3/3`.

## A 5-minute tour

1. **Day One**: six outcome cards with live counts.
2. **Student Coach**: ask "What are the rules for a Thunder Dome test?" → cited answer; click a citation chip →
   the source opens at the cited lines. Ask "How do I set up TikTok Shop?" → "The course material doesn't cover this."
3. **Approval queue**: a held draft ("we guarantee results") can't be approved; edit it clean → approve → "mock:instagram".
4. **Bottleneck diagnosis** → Sample → Diagnose → bottleneck with money impact and cited benchmarks → **Next best action** opens the Studio line.
5. **Ads Ops** → Practice go-live: build PAUSED test → propose go-live → approve → roll back.
6. **Go-live scorecard**: every acceptance row with its honest status and the key it's waiting for.
7. **Settings** → Reset demo to clean story (backs up first; refused on the real workspace).
