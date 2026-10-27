# Architecture

One Python process (FastAPI + uvicorn) serves the API and the built React app. SQLite in WAL mode: one database
per tenant, a registry database, and a separate demo database. Everything below runs offline; the only outbound
network code lives in `operator_ai/llm.py` (behind the AI budget gate) and `operator_ai/live/` (behind per-tenant
live switches).

```
browser (React, hash routes /t/<tid>/#/page)
   │  cookie op_s, Path=/t/<tid>, HttpOnly (Secure when hosted)
   ▼
FastAPI  /t/<tid>/api/…   (deps.get_ctx → tenant DB session → RBAC by tab)
   │       /t/<tid>/hooks/…  (signed webhooks: Meta, Telegram, Slack, SMS, phone, email, payments)
   │       /mcp, /m/<tid>/<slug>, /embed.js, /health, /ready
   ▼
agents ──► runtime.Run (plan → steps → result, stored as it runs, polled by the UI)
   │          ├─ retrieval (BM25 + local dense + RRF, quality/kind weights, owner priority)
   │          ├─ llm (budget gate, replay store) — optional
   │          ├─ guardrails.check(text, mode) — pure functions
   │          └─ critique / rubrics (SOP-tied rules)
   ▼
queue.add → guardrails → pending | held | needs_human ──(human approves: atomic claim)──► sending
                                                         │   ├─ scheduled (send_after) ── delivery.dispatch_due ──┐
                                                         │   ├─ awaiting_manual_send ── "I sent it" ──────────────┤
                                                         │   ├─ live sender (switch on) / mock (demo only) ───────┴─► sent
                                                         │   └─ send_failed | cancelled (cancel_if, DNC, unsubscribed)
                                                         ├─ on_approve hooks (action gate, calendar…)
                                                         └─ on_sent hooks + item_sent (stage moves, caps, cadences)
```

## Modules

| Area | Files |
|---|---|
| Config, DB, tenancy | `config.py`, `db.py` (all schemas), `tenancy.py` (registry, LicenceError, demo flag), `settings.py` (BOX_ONLY) |
| Auth | `auth.py` (scrypt passwords, sessions, throttle, ROLE_TABS/ROLE_DECIDE), `deps.py` |
| Knowledge base | `corpus.py` (load, quality flags, bylines, timestamps, chunking, exclusions), `content_policy.py`, `textnorm.py`, `embeddings.py`, `index.py`, `retrieval.py` |
| AI | `llm.py` (provider-pluggable, budget gate, retries, replay store, redaction) |
| Safety | `guardrails.py`, `claims.py` (proof records for result figures), `ratelimit.py` (public endpoints), `errors.py` (input errors → 4xx), `redact.py`, `audit.py`, `vault.py`; see `docs/SECURITY.md` |
| Runtime | `runtime.py` (Run/trace), `jobs.py` + `workers.py` (durable queue), `scheduler.py` |
| Agents | `agents/coach.py`, `reviewer.py`, `coaching.py`, `sales.py`, `onboarding.py`, `accountability.py`, `fulfilment.py`, `ads_ops.py`; `diagnostic.py`, `webinar_chain.py`, `studio.py`, `departments.py`, `outreach_loop.py`, `brief.py` |
| Actions | `actions.py` (proposal → owner approval → execute with prior state → rollback), `adapters/ads.py` (Mock), `live/meta.py` (MetaAdsAdapter), `crm.py` |
| Platform | `threads.py`, `uploads.py`, `skills.py`, `call_review.py`, `channels.py`, `mcp_server.py`, `console.py`, `calendar.py` |
| Live layer | `live/net.py` (retries, token bucket), `live/meta.py`, `live/gcal.py`, `live/chain.py` |
| Reporting | `reports.py` (Day One, Command Centre, Morning brief), `scorecard.py` (Go-live), `evals.py` |
| First client | `prospects.py` (ICP, lawful imports, claims, research, guarded drafts, caps, booking, win), `proposals.py` (public page `/t/<tid>/p/<token>`), `missions/` (goal-driven plans over typed steps that call the real feature functions) |
| Delivery and replies | `delivery.py` (scheduled sends, manual-send truth, cadences, compliance footer, `/u/<token>`), `inbound.py` (reply routing, classification, STOP and bounces → do-not-contact), `escalations.py` (SLAs, owner alerts) |
| Training | `academy/` (cited lessons, questions, spaced repetition, certificates, the own-outreach gate), `roleplay/` (turn-aware scoring, rubric rules as data, exam attempts, AI phrasing caps), `persona/` (subject-signed consent, framing on consented surfaces) |
| After the win | `client_ops.py` (onboarding checklist, intake `/c/<tid>/<token>`, weekly ops), `webinars.py` (events, `/w/<tid>/<slug>`, fan-out held by delivery) |
| Team and setup | `team.py` (invites, `#/join`, resets, data rights), `setup.py` (owner checklist and first-run lists from every feature's real state), `memory.py` ("What I know about you", read by the Coach) |

Features talk through `events.py` (item_sent, reply_received, prospect_stage, client_won, booking_made,
certification_awarded, dnc_set ...), `queue.on_approve / on_sent / on_reject / on_cancel`, `meta.send_after /
send_before / cancel_if` (the delivery dispatcher), `scheduler.TENANT_TICKS`, `demo.FEATURE_SEEDERS` and
`guardrails.RULE_PLUGINS`. A queue item's life: pending | held | needs_human → approved → scheduled |
awaiting_manual_send → sending → sent | send_failed | needs_attention (interrupted: a person checks) | cancelled.

## Data

Per-tenant DB (`data/tenants/<id>/tenant.db`): settings, users, sessions, audit, runs, queue, jobs, actions,
students, checkins, coach_tasks, leads, clients, threads, messages, projects, memories, uploads, profiles,
workers, eval_runs, calendar_events, chain_* tables, outreach tables, builder tables, mock_ads, CRM tables,
calls/consents, drills, channel tables, mcp_tokens, minis, payment_events, members, drive_staged, voice_sessions,
rehearsals, ai_replays, queries, kb_notes. Registry: `data/tenants/registry.db` (id, name, kb_scope, kind).
Demo: `data/demo.db`. Vault: `data/tenants/<id>/vault.bin` + `vault.key` (mode 600).

## Retrieval

BM25 over stemmed tokens with programme compounds folded (`thunderdome`, `noshow`, `optin`, `showrate`…) and
synonym expansion at 0.4 weight; dense vectors from local bge-small-en-v1.5 (when cached) or a deterministic
hashed embedding; reciprocal-rank fusion (k=60). Quality weights (garbled ×0.5, partial ×0.85) and kind weights
(SOP ×1.1) scale raw scores before ranking, so they move passages without swamping relevance. The coverage judge
is lexical: the idf-weighted share of the question's terms the corpus knows, plus the strength of the top match.

## Jobs

`jobs` table with UNIQUE idempotency keys, `next_at` for exponential backoff (max 3 retries), `running` rows re-queued
on start. Worker threads poll every watched tenant. The live inbox has its own 8-attempt policy.
