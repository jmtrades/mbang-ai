# Guardrails

All enforced in code, each with positive and negative tests (`tests/test_guardrails.py`, `tests/test_phase2_core.py`
and the phase tests named below).

## The function

`guardrails.check(text, mode, meta=None, banned_names=None) -> [flags]` with modes `outreach`, `client_asset`,
`plain`. A flag is `{code, severity: block|review, message, match}`. It runs when an item is queued, again when it
is approved (after edits), inside self-critique, inside coaching line-fixes, and on builder AI drafts. A `block`
flag holds the item until it is edited clean; a `review` flag marks it "needs a person".

`mode_for(meta)`: prospect-facing → `outreach` (wins even if it is also a client asset); else client asset →
`client_asset`; else `plain`.

## Rules

| Rule | Code | Severity | Notes |
|---|---|---|---|
| Guarantees, money back, risk-free | `guarantee` | block | negation-aware ("we never guarantee" passes) |
| Income claims ("students make $100k a month", "100K+") | `income_claim` | block | the allowed outreach range is judged by the money rule instead |
| Invented testimonials / reviews / case studies / results | `fake_testimonial` | block | |
| Results about a named person | `unverified_result` | block | needs proof + permission |
| Booking shout-outs with names | `fake_shoutout` | block | |
| Planted questions, fake/dummy/second accounts | `planted_question` | block | "never plant questions" passes |
| Invented credentials | `invented_credential` | block | |
| Fake scarcity ("doors closing", "won't last", "last chance") | `fake_scarcity` | block | |
| Deadline / capacity without a stated reason | `deadline_no_reason` | review | with "because / so that / due to…" it passes |
| "Re:" / "Fwd:" subject or "following up" on a first touch | `deceptive_subject` | block | allowed when `prior_contact` |
| Banned names (and persona banned words) | `banned_name` | block | also scrubbed from every API response |
| Never-invite list (Gmail canonicalised) | `never_invite` | block | checked on propose and again at send |
| SMS / phone without opt-in | `not_opted_in` | block | |

### Outreach money rule (prospect-facing only)

- The only dollar figure allowed is "$50k-$100k a month" (dash/case variants accepted).
- It must be hedged ("Let me make [Brand] $50k-$100k a month through …"); definite wording ("will add", "adds")
  blocks (`outreach_definite`), unhedged blocks (`outreach_unhedged`).
- Cold, unqualified or unknown-tier leads may not get the figure at all (`cold_lead_figure`); they get
  "Let me help [Brand] grow through …".
- Any other amount (`outreach_money`) and any price / fee / retainer / investment wording (`outreach_price`) block.

Client assets (VSL, webinar script, pages, that funnel's emails) may state the student's own offer price.

## Other enforced guardrails

- **Approval for everything outgoing.** `queue.approve` re-runs the guardrails, then delivers to a mock transport
  or marks the item "ready to send manually". The only live path is an approved calendar invite with the tenant's
  live calendar switch on (`live/chain.deliver_calendar_invite`).
- **No impersonation.** Banned names are scrubbed (generated text, traces, citations, the UI via a response
  middleware) and block any queue item. Framing answers in a real person's teaching phrasing needs that person's own
  consent, signed on their own login with their password, scoped, expiring and withdrawable (Owner console → Persona;
  the person signs from Business profile). Whenever a persona name is configured, `operator_ai/persona/guards.py`
  blocks `impersonation` ("I'm X", "X here", "As X, I…", sign-offs), `persona_in_outreach` (the name or an approved
  phrase of 3+ words in outreach, client assets or own-prospect items), `persona_autobio` (first-person stories in
  styled output) and `speculative_attribution` ("X would say…", "according to X…" without an [S#]). "Are you X?" and
  questions about "your clients" get "I'm an AI assistant trained on X's material, not X." The old owner-typed consent
  is shown as invalid.
- **Demo labelled and separate.** Fictional records carry "(DEMO)" and `example.invalid`; `data/demo.db` is separate;
  every reset checks the registry flag, the file path and an in-database marker, backs up first, and refuses the real DB.
- **Secrets.** AES-256-GCM vault per tenant (fresh nonce per write, tenant id as AD, key file mode 600); APIs say
  "set / not set" only; `redact()` scrubs sk-…, EAA…, ya29.…, 1//…, access_token=…, Bearer … from errors, logs and audit.
- **Course material is not for resale.** `kb_scope=course` is owner/demo only (`LicenceError`); clients start `empty`
  and may move to `own`. Third-party public material is behind a switch that is off.
- **Ads and CRM.** Mock adapters by default. The Meta adapter needs the live switch, a non-empty allowlist containing
  the account, and an approved action for every write. Creates are forced PAUSED with zero budget and re-read; anything
  else is forced back and the action marked failed. One-click rollback with stored prior state. CRM terminal moves
  need owner approval.
- **Live switches** (`live_meta_enabled`, `meta_allowlist`, `live_calendar_enabled`) and AI budgets are server-only:
  `scripts/tenant_admin.py … --owner-yes "<recorded reason>"`; the settings API returns 403; the yes is audited.
- **KB content policy.** Lines teaching deceptive tactics, drugs/stimulants, ban evasion or stereotypes are dropped at
  index time and listed as "Not used: this passage describes {tactic}"; owner exclusions by file + line or time range.
- **AI budget fails closed.** `ai_token_budget` defaults to 0 → no model request from any thread.
- **Audit log** for logins, queueing, guardrail blocks, approvals, rejections, actions, rollbacks, settings, switch flips,
  vault saves (field names only), KB scope changes, consents, payments, MCP tokens.
