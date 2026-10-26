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
queue.add → guardrails → pending | held | needs_human ──(human approves)──► deliver: mock transport
                                                         │                  or "ready to send manually"
                                                         └─ on_approve hooks (action gate, calendar…)
```

## Modules

| Area | Files |
|---|---|
| Config, DB, tenancy | `config.py`, `db.py` (all schemas), `tenancy.py` (registry, LicenceError, demo flag), `settings.py` (BOX_ONLY) |
| Auth | `auth.py` (scrypt passwords, sessions, throttle, ROLE_TABS/ROLE_DECIDE), `deps.py` |
| Knowledge base | `corpus.py` (load, quality flags, bylines, timestamps, chunking, exclusions), `content_policy.py`, `textnorm.py`, `embeddings.py`, `index.py`, `retrieval.py` |
| AI | `llm.py` (provider-pluggable, budget gate, retries, replay store, redaction) |
| Safety | `guardrails.py`, `redact.py`, `audit.py`, `vault.py` |
| Runtime | `runtime.py` (Run/trace), `jobs.py` + `workers.py` (durable queue), `scheduler.py` |
| Agents | `agents/coach.py`, `reviewer.py`, `coaching.py`, `sales.py`, `onboarding.py`, `accountability.py`, `fulfilment.py`, `ads_ops.py`; `diagnostic.py`, `webinar_chain.py`, `studio.py`, `departments.py`, `outreach_loop.py`, `brief.py` |
| Actions | `actions.py` (proposal → owner approval → execute with prior state → rollback), `adapters/ads.py` (Mock), `live/meta.py` (MetaAdsAdapter), `crm.py` |
| Platform | `threads.py`, `uploads.py`, `skills.py`, `call_review.py`, `channels.py`, `mcp_server.py`, `console.py`, `calendar.py` |
| Live layer | `live/net.py` (retries, token bucket), `live/meta.py`, `live/gcal.py`, `live/chain.py` |
| Reporting | `reports.py` (Day One, Command Centre, Morning brief), `scorecard.py` (Go-live), `evals.py` |

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
