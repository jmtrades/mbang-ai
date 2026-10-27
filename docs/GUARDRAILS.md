# Guardrails

All enforced in code, each with positive and negative tests (`tests/test_guardrails.py`, `tests/test_hardening.py`,
`tests/test_phase2_core.py` and the phase tests named below). `tests/test_guardrails.py` holds a bypass matrix
(50+ strings from the 2026-10-27 audit and our own adversarial pass) that must stay blocked.

## The function

`guardrails.check(text, mode, meta=None, banned_names=None, tenant=None) -> [flags]` with modes `outreach`,
`client_asset`, `plain`. A flag is `{code, severity: block|review, message, match}`. It runs when an item is queued,
again when it is approved (after edits), inside self-critique, inside coaching line-fixes, on builder AI drafts and,
sentence by sentence, on AI-written coach answers. A `block` flag holds the item until it is edited clean; a `review`
flag marks it "needs a person". `check_for(tenant, text, meta)` is the usual entry point: it adds the tenant's banned
names and banned words and lets the plug-in rules see the tenant (for example its proof records).

`mode_for(meta)`: prospect-facing → `outreach` (wins even if it is also a client asset); else client asset →
`client_asset`; else `plain`.

### Normalisation (before any rule runs)

The rules see a normalised copy of the text, so the usual dodges don't work:

- HTML entities are decoded (`&#36;3000` → `$3000`) and the text is NFKC-normalised (full-width and maths letters).
- Invisible characters (Unicode `Cf`: zero-width space and joiner, bidi marks, soft hyphen, BOM) are removed.
- Accents are stripped and look-alike letters from Cyrillic, Greek, Armenian and IPA are folded to Latin
  (`guаrantee` with a Cyrillic а → `guarantee`); dash and quote variants are unified.
- Single letters joined by separators are rejoined (`g u a r a n t e e`, `g.u.a.r.a.n.t.e.e`).
- The deception rules also run on a de-leeted copy (`gu4rant33d` → `guaranteed`) and on a copy with separators
  inside words removed (`Guar-antee`); the guarantee stem tolerates the usual misspellings (`garanteed`,
  `gaurantee`, `garuntee`, `gurantee`, `guarentee`).
- Bidi override and isolate characters (which make text display differently from what it says) are flagged
  (`hidden_characters`: block in outreach and client assets, review elsewhere). Ordinary right-to-left text is fine.
- For the money rule, digits split by spaces are joined (`3 0 0 0`, `3 000`).

### Negation

A negator only counts when it governs the match: in the same clause and within a few words.

- The window stops at a contrast word (`but`, `however`, `yet`, `although`, `except`, `instead`…), a comma, colon,
  semicolon, bracket or dash, and at a new subject (`we`, `I`, `you`, `they`, `our`…). So "No pressure, but this offer
  won't last", "Don't miss out, we guarantee 3x ROI" and "I don't want to guarantee anything but we guarantee
  results" all flag.
- `not only`, `not just`, `no doubt`, `no more`, `no need`, `don't miss`, `don't wait`, `can't lose`, `can't go
  wrong`… are not negations, and neither is a negator that governs an interjection ("Don't worry guaranteed results",
  "Not kidding guaranteed results") or a hyphenated compound ("can't-lose", "never-ending").
- Auxiliary negators (`never`, `not`, `don't`, `can't`…) reach two content words ("never put a guarantee in your USP",
  "don't use fake scarcity like saying doors are closing" pass). Verbs like `avoid` / `stop` / `refuse` and
  determiners like `no` / `without` only cover the words right after them ("no hassle, guaranteed results" and
  "stop guessing, guaranteed results" flag).

## Rules

| Rule | Code | Severity | Notes |
|---|---|---|---|
| Guarantees, money back, risk-free, "no risk", "full refund if it doesn't work", "no questions asked", "refundable", "or you don't pay", "promise you'll get…", "we promise results", "100% success rate", "results warranty" | `guarantee` | block | negation-aware as above; "non-refundable" passes |
| Income claims ("students make $100k a month", "most of our clients hit six figures", "100K+", "$30k months", "earn up to $10k a month", "our clients made 40,000 last month") | `income_claim` | block | any currency symbol or code, k / thousand / grand / figures, spelled-out amounts, and a bare amount after an earning verb (grouped thousands or 4+ digits, not a year, followed by money words like "last month" / "in sales", never a count like "5000 cold calls"). Runs on the unaltered text in every mode: the $50k-$100k range is never exempt |
| Invented testimonials / reviews / case studies / results | `fake_testimonial` | block | |
| Results about a named person | `unverified_result` | block | needs proof + permission |
| Booking shout-outs with names | `fake_shoutout` | block | |
| Planted questions, fake/dummy/second accounts | `planted_question` | block | "never plant questions" passes |
| Invented credentials | `invented_credential` | block | |
| Fake scarcity ("doors closing", "won't last", "last chance", "act now", "spots filling fast") | `fake_scarcity` | block | |
| Deadline / capacity without a stated reason | `deadline_no_reason` | review | with "because / so that / due to…" it passes |
| "Re:" / "Fwd:" subject or "following up" on a first touch | `deceptive_subject` | block | allowed when `prior_contact` |
| Banned names (and persona banned words) | `banned_name` | block | names are also scrubbed from every API response (see below) |
| Never-invite list (Gmail canonicalised) | `never_invite` | block | checked on propose and again at send |
| SMS / phone without opt-in | `not_opted_in` | block | |
| Outreach range without a current proof record | `unsubstantiated_claim` | block | plug-in `operator_ai.claims:guard_rules` |

### Outreach money rule (prospect-facing only)

- No amount of any kind (`outreach_money`): any currency symbol (every Unicode `Sc` character: `$ € £ ₹ ¥ ₦ R$`…)
  or ISO code (`USD`, `EUR`, `CAD`…) next to a number with any spacing, `30k`, `10 thousand`, `eight grand`,
  `a million dollars`, `six figures`, `3000/month`, `3,000 a month`, `299 per session`, `pay 2500`, `2,500 to start`,
  `only 1997`, a bare `2,997`, `$$$`, bare prices ("the program is 2997", "it's 997.", "join for 1997 today",
  "1997 gets you in"), bare earnings ("made 40000 in a month", "40,000 last month"). ("10k followers", "5000 cold
  calls", "120 calls last month" and years like "2026" pass.)
- No price, fee, retainer or investment wording (`outreach_price`).
- The one figure that can be allowed is the hedged "$50k-$100k a month", and only when **all** of these hold:
  - the workspace has a **current proof record** for it (otherwise `unsubstantiated_claim`; see below);
  - the lead is warm or hot (cold, unqualified, nurture or unknown tier → `cold_lead_figure`);
  - it is hedged, never definite ("will add", "adds" → `outreach_definite`; no hedge → `outreach_unhedged`);
  - it has the range line's shape, about a business's revenue: "Let me (help) make [Brand] $50k-$100k a month
    through …" (anything else, or a person anywhere in the [Brand] slot — "make *you* …", "Acme *owners*",
    "your *clients*" — → `outreach_range_shape`).
- The drafting helpers follow the same rule: `guardrails.range_line(brand, mechanism, tenant)`,
  `agents.sales.reply_line(lead, tier, tenant)` and the Studio call offer write the range only while a current proof
  record exists, else the no-figure line "Let me help [Brand] grow through …".

Client assets (VSL, webinar script, pages, that funnel's emails) may state the student's own offer price.

### Proof records (claims substantiation)

`operator_ai/claims.py`, table `claims_substantiation`. A record backs a figure while it is **current**: status active,
not past its expiry date (1-366 days), at least one evidence file (pdf, png, jpg, webp, csv, txt, md; 5 MB each, 10
max), and a typicality note of at least 20 characters saying what typical results look like.

- Owner only: Owner console → Proof records (`GET/POST /claims`, `POST /claims/{id}/evidence`,
  `GET /claims/{id}/evidence/{eid}`, `POST /claims/{id}/expire`). Every change is audited.
- New workspaces start with **no record**, so the range is blocked and drafts use the no-figure line.
- The demo is seeded with one fictional record labelled "(DEMO)" with a fictional evidence file, so its story still
  shows the range. Proof records are read-only in the demo. A real workspace is never seeded.

### Coach answers

- An AI-written answer (live or a labelled replay) is served as course-grounded only claim by claim: a sentence
  without a valid `[S#]` is dropped, then every remaining sentence is checked (plain mode) and blocked ones are
  dropped. If nothing cited survives, the answer falls back to the extractive answer from the same passages
  (labelled "the AI answer had no valid citations"), or to "not covered".
- Extractive answers quote the owner's own indexed material verbatim (already screened by the content policy at
  index time), so they are not rewritten.
- A short question is read as a follow-up to the previous one only when it really refers back: no content words of
  its own, a pronoun standing in for the earlier subject, or "what about / and …" with at most a couple of its own
  content words. "Is there a kill rule for ads?" after a USP question is a new question.

### Public lead-magnet minis

A mini scoped to chosen files answers **only** from passages in those files: retrieval is filtered before the
answer is built (extractive, guardrail-checked, traced as agent `mini`). Limits: the question limit counts per email
address per day (canonical form, so `a+1@…` is the same address), 3 sessions per email per day, 10 session starts and
60 questions per network per hour (`OPERATOR_LIMIT_MINI_START`, `OPERATOR_LIMIT_MINI_ASK`).

## Other enforced guardrails

- **Approval for everything outgoing.** `queue.approve` re-runs the guardrails, then delivers to a mock transport
  or marks the item "ready to send manually". The only live path is an approved calendar invite with the tenant's
  live calendar switch on (`live/chain.deliver_calendar_invite`).
- **No impersonation.** Banned names are scrubbed (generated text, traces, citations, and every tenant API response
  via a middleware) and block any queue item. The response scrub walks the decoded JSON and rewrites string values
  only, so a banned word equal to a key or literal (`status`, `false`, `id`) can't corrupt a response; it skips the
  settings and personality endpoints so the lists round-trip unchanged; persona style words are enforced on
  generated text but not scrubbed from responses. A named persona needs recorded consent (Owner console → Personality).
- **Settings have shapes.** Every API-editable setting is validated (`settings.validate`): banned names are a list of
  2-80 character strings, colours are `#rrggbb`, switches are true/false, numbers are finite and in range, anything
  else is plain JSON under 20 KB. A wrong shape is a 422, never stored.
- **Demo labelled and separate.** Fictional records carry "(DEMO)" and `example.invalid`; `data/demo.db` is separate;
  every reset checks the registry flag, the file path and an in-database marker, backs up first (newest 5 copies
  kept), and refuses the real DB. See `docs/SECURITY.md` for the demo's budget, rate limits and corpus.
- **Secrets.** AES-256-GCM vault per tenant (fresh nonce per write, tenant id as AD, key file mode 600); APIs say
  "set / not set" only; `redact()` scrubs sk-…, EAA…, ya29.…, 1//…, access_token=…, Bearer … from errors, logs and
  audit. Backups keep the vault keys out of the data archive (`docs/SECURITY.md`).
- **Course material is not for resale.** `kb_scope=course` is owner/demo only (`LicenceError`); clients start `empty`
  and may move to `own`. Third-party public material is behind a switch that is off. The public demo reads the
  fictional fixture corpus, never `inputs/`, unless the server-only `demo_course_material` switch is on.
- **KB admin is per workspace.** Owner exclusions (file + line or time range) live in the tenant's own folder and are
  part of that tenant's index cache key; they never change another tenant's answers. KB-admin and proof-record writes
  are owner-only and refused in the demo.
- **Ads and CRM.** Mock adapters by default. The Meta adapter needs the live switch, a non-empty allowlist containing
  the account, and an approved action for every write. Creates are forced PAUSED with zero budget and re-read; anything
  else is forced back and the action marked failed. One-click rollback with stored prior state. CRM terminal moves
  need owner approval.
- **Live switches** (`live_meta_enabled`, `meta_allowlist`, `live_calendar_enabled`, `live_channels_enabled`,
  `demo_course_material`) and AI budgets are server-only: `scripts/tenant_admin.py … --owner-yes "<recorded reason>"`;
  the settings API returns 403; the yes is audited.
- **KB content policy.** Lines teaching deceptive tactics, drugs/stimulants, ban evasion or stereotypes are dropped at
  index time and listed as "Not used: this passage describes {tactic}"; owner exclusions by file + line or time range.
- **AI budget fails closed.** `ai_token_budget` defaults to 0 → no model request from any thread. The demo has its own
  small budget that a reset never refills.
- **Audit log** for logins, queueing, guardrail blocks, approvals, rejections, actions, rollbacks, settings, switch flips,
  vault saves (field names only), KB scope changes, consents, payments, MCP tokens, proof records.

## Known limits

Pattern rules can't understand meaning. A determined writer can still phrase a claim the rules don't recognise
(for example a long run-on sentence where a negator sits two words before an unrelated guarantee, or an amount
described only in words the lists don't contain). That is why every outgoing item also needs a human approval, and
why the bypass matrix in `tests/test_guardrails.py` grows with every miss we find.
