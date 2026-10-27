# Operator AI

A white-label, agentic AI operating system for a high-ticket coaching / growth-operator programme. Its users are the
course owner, their team (coaches, setters, closers) and their students. It answers from the owner's own material
with citations, and it does the work — diagnoses, drafts, reviews, qualifies, books, follows up and reports — while
putting every outgoing item in front of a human for approval.

> "Your programme's AI. It's trained on your material, it speaks under your brand, and it does the work for you and
> your students. Nothing goes out without your approval."

**Status:** built and tested locally. Nothing is deployed, no live switch has been turned on, and no real message, post,
ad change or calendar invite has ever been sent. See `SELF_SCORE.md` for an honest per-item score and
`docs/LIVE_READINESS.md` for what going live needs.

## Quick start

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.lock   # Python 3.12+ (built on 3.14)
(cd web && npm ci && npm run build)
scripts/run.sh                                                        # http://127.0.0.1:8850
```

- **Pitch demo:** open `http://127.0.0.1:8850/t/demo/` → **Pitch demo** → pick a role. A separate, fictional
  database (`data/demo.db`); everything there is labelled DEMO.
- **Real workspace:** `http://127.0.0.1:8850/t/owner/`. The owner password is written to
  `data/tenants/owner/owner_password.txt` (mode 600) on first start; it is never printed.
- **Course material:** copy the course folders into `inputs/` (read-only; see the layout in `docs/ARCHITECTURE.md`).
  Until then the app uses a small fictional corpus from `tests/fixtures/corpus/`.
- **AI is optional.** With no key, no budget or `OPERATOR_DISABLE_LLM=1`, every feature runs on rules, templates,
  retrieval and labelled replays of saved AI output. The AI budget defaults to 0 and changes only on the server:
  `scripts/tenant_admin.py budget owner ai 200000 --owner-yes "<recorded reason>"`.

## What's inside

| Area | What it does |
|---|---|
| Student Coach | Cited Q&A from the owner's material; honest "not covered"; FR/ES/PT detection; labelled replays |
| Work Reviewer / Coaching mode | Rubric scores tied to SOPs, line-level fixes with citations, guardrail-checked suggestions |
| Diagnostic | Pure-maths bottleneck with money impact, cited benchmarks, Next best action → Studio |
| Sales | Transparent lead scoring, routing, setter script, pre-call brief, money-rule replies, slots, show-up ladder, 500-lead burst, Lead Rescue Desk |
| Students | Onboarding (gaps → cited 30/60/90), accountability over 2,000+ students, nudges, escalations, Retention Star |
| Clients | Fulfilment (money model, launch plan, reviewed hooks, VSL outline, report), webinar execution chain, Ads Ops (PAUSED-only, rollback) |
| Studio | 7 lines of work + Launch kit; AI departments builder (the 90-second demo) |
| Platform | Threads, projects + memory, uploads with local OCR, business profile, skills + workers, call review + drills, mock channels, MCP server, mock CRM, minis + embed, Drive inbox sync, members + payments |
| Owner | Approval queue, runs & traces, audit log, KB admin + provenance, evals, Go-live scorecard, Command Centre, Morning brief |
| Live layer (off) | Signed Meta lead webhook → lead fetch → qualification → booking → Google Calendar event, tested against fakes |
| Console UI | White-label design system (tokens, light/dark/system theme, bundled Inter), role-aware collapsible sidebar, phone tab bar, ⌘K / Ctrl K command palette, Day One home with what-to-do-next, reduced-motion aware transitions |

## Guardrails (summary — details in `docs/GUARDRAILS.md`)

Nothing outbound leaves without approval, and approval only marks items ready or delivers to a mock transport. No
impersonation (banned-names scrub everywhere). No fake proof, fake scarcity or deceptive subject lines (negation-aware).
Prospect outreach may only mention a hedged "$50k-$100k a month" — and never to cold leads. Demo data is labelled
and separate. Secrets live in an AES-256-GCM vault. The course KB belongs to the owner tenant only. Live switches are
off and server-only. AI budget 0 fails closed. Everything is audited.

## Tests

```bash
.venv/bin/pytest                        # everything, offline (AI off, temp data, fakes; stray hosts fail)
.venv/bin/pytest -m "not ui"            # skip the browser tests
scripts/builder_demo_click.py --times 3 # against a running server → CLEAN 3/3
scripts/api_sweep.py                    # every GET no 5xx; bad bodies 422
scripts/load_test.py                    # /health RPS, 2,000-student dashboard p95, 500-lead burst
scripts/eval_retrieval.py               # hit@5 / hit@10 on tests/gold_questions.json
```

## Operations

`/health`, `/ready`, `scripts/watchdog.sh`, `scripts/backup.sh` / `scripts/restore.sh`, `scripts/rotate_logs.sh`,
`scripts/prune_cache.py`, `scripts/preflight_live.py`, `scripts/tenant_create.py`, `scripts/tenant_admin.py`,
`scripts/reset_demo.py`, `scripts/reset_builder_demo.py`. Deploy bundle (built, not deployed): `deploy/` — see
`deploy/README.md`.

## Docs

`docs/ARCHITECTURE.md` · `docs/GUARDRAILS.md` · `docs/LIVE_READINESS.md` · `docs/DEMO_SCRIPT.md` · `DECISIONS.md` ·
`PROGRESS.md` · `SELF_SCORE.md`
