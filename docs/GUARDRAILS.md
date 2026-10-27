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
| SMS / phone without opt-in | `not_opted_in` | block | any number formatting; the latest START/STOP wins |
| Contact marked do-not-contact | `do_not_contact` | block | prospects and leads; a scheduled send is cancelled instead |
| Address on the suppression list (unsubscribed, asked to stop) | `unsubscribed` | block | emails, @handles, phone numbers |
| Live channel on but no usable address | `no_address` | block | e.g. a Telegram nudge with no linked chat and no email |
| Prospect email without an unsubscribe line | `missing_unsubscribe` | block | `delivery.guard_rules`; the footer is added automatically |
| Prospect email without the business postal address | `missing_postal_address` | block | Business profile, Identity |

### Outreach money rule (prospect-facing only)

- The only dollar figure allowed is "$50k-$100k a month" (dash/case variants accepted).
- It must be hedged ("Let me make [Brand] $50k-$100k a month through …"); definite wording ("will add", "adds")
  blocks (`outreach_definite`), unhedged blocks (`outreach_unhedged`).
- Cold, unqualified or unknown-tier leads may not get the figure at all (`cold_lead_figure`); they get
  "Let me help [Brand] grow through …".
- Any other amount (`outreach_money`) and any price / fee / retainer / investment wording (`outreach_price`) block.

Client assets (VSL, webinar script, pages, that funnel's emails) may state the student's own offer price.

## Other enforced guardrails

- **Approval for everything outgoing, and delivery truth.** `queue.approve` claims the item atomically (one approval
  ever wins), re-runs the guardrails, then: holds it as `scheduled` until `meta.send_after`; hands it to a person as
  `awaiting_manual_send` (Instagram/LinkedIn DMs and phone always; any channel without a live sender in a real
  workspace) until they confirm "I sent it"; or sends it through a live sender behind the server-only switch
  (`sent` / `send_failed`). Mock transports run only in the demo workspace. The scheduled dispatcher re-runs the
  guardrails and the never-invite check and evaluates `meta.cancel_if` before anything goes. Stage moves, caps and
  stats count `sent`, never `approved`. Prospect email carries a postal-address footer, an unsubscribe link and
  List-Unsubscribe headers; `/u/{token}` (guessing is throttled, a real link always works, applied once) sets
  do-not-contact and suppresses the address.
  In a real workspace a prospect email cannot be approved (live or copy-and-send) until `OPERATOR_PUBLIC_URL` is set,
  so the link never points at localhost. A send interrupted mid-way becomes `send_failed` after 15 minutes and is
  never retried by itself.
- **Your drafts stay yours.** A queue item carrying `meta.owner_user_id` (outreach a person drafted for their own
  prospects/clients) is visible to and decided by that person and the owner only, never by other staff through
  their role; queue counts cover only what the viewer may see. Only the owner can record SMS/phone opt-in collected
  outside the app (audited as consent); everyone else's contacts opt in by replying START.
- **No impersonation.** Banned names are scrubbed (generated text, traces, citations, the UI via a response
  middleware) and block any queue item. A named persona needs recorded consent (Owner console → Personality).
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
