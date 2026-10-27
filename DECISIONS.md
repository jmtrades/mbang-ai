# Decisions

One line each: decision — reason — how to change it.

- Python 3.14 venv (not 3.15) — 3.15 is too new for some wheels; 3.14 has wheels for every pinned dependency — recreate `.venv` with another interpreter and `pip install -r requirements.lock`.
- FastAPI lifespan instead of `on_event` — Starlette 1.x removed `on_event` — n/a.
- Separate demo DB at `data/demo.db`, registered as tenant `demo` (kind=demo) — the spec wants a separate demo database with one-click roles; reusing tenant routing keeps one code path — demo path is `Config.demo_db_path`.
- Owner/real workspace is tenant `owner` (kind=owner, kb_scope=course) — the course KB may only belong to the owner tenant — set `OPERATOR_DEFAULT_TENANT` to rename.
- Permissions are UI tab names (`ROLE_TABS` in `auth.py`) — one source of truth for the RBAC matrix, the sidebar and API checks — edit `ROLE_TABS` / `ROLE_DECIDE`.
- A session cookie from another tenant gets 404 (not 401) — cross-tenant access must not reveal anything — `deps.get_ctx`.
- Embeddings: `OPERATOR_EMBEDDINGS=auto` uses fastembed bge-small-en-v1.5 only when the model is already in the local cache; otherwise a deterministic local hashed n-gram embedding (384-d). Tests force `hash` — tests must run offline and the model download is a network call — run `scripts/fetch_embeddings.py` once (network) then restart.
- AI token budget is cumulative per tenant (tokens used vs `ai_token_budget`) — simplest fail-closed meter — `scripts/tenant_admin.py set-budget`.
- Model default `claude-sonnet-5-5` via `OPERATOR_LLM_PROVIDER=anthropic` — current Claude family; any provider works through `llm.py` — set `OPERATOR_LLM_PROVIDER` / `OPERATOR_LLM_MODEL`.
- No `inputs/` present in this build environment, so the fictional fixture corpus in `tests/fixtures/corpus/` is used for tests and the demo — `OPERATOR_CORPUS_DIR` switches automatically once `inputs/` has files.
- "Budget 0" is reported before "AI disabled" — the budget is the product-level gate the owner sees — `llm.unavailable_reason`.
- Rubric pass needs score ≥ 70, no guardrail block and no heavy criterion (weight ≥ 2) under 5 — a weighted average alone let a 3-question DM pass — `critique.critique`.
- Unknown lead tier is treated as cold for the outreach money rule — safest reading of "cold or unqualified leads get a no-figure line" — pass `lead_tier` in item meta.
- Setter cap = 5 new opener conversations per sender per day (the lesson's "five thoughtful conversations a day per sender"); the goal calculator uses the same cap and reports how many senders a goal needs rather than raising one sender's volume — `outreach_loop.DAILY_CAP`, tenant setting `outreach_daily_cap`.
- Studio/webinar prospect-facing drafts use the placeholder "[their brand]" for the prospect's brand — the programme owner's brand is not the prospect's — edit the draft in the queue.
- Banned names are also scrubbed by a response middleware on every `/t/<tid>/api` JSON response (except `/api/settings`, where the owner manages the list) — the name-leak sweep found a Studio response echoing a banned name from the brief; a single safety net beats chasing every echo — `app.scrub_banned_names`.
- Reminders in the show-up ladder go on the conversation's own channel (Instagram DM), not SMS — SMS needs an opt-in first and would be held otherwise — change `channel` in `agents/sales.py` once SMS opt-in is collected.
- Live leads: with `live_meta_enabled` off, the webhook still verifies the signature and stores the lead, but qualification uses the webhook's own fields (practice) and makes no Graph call — receiving is safe, fetching uses the tenant's token only when the owner switched it on — `live/chain._qualify`.
- The real Meta Ads adapter forces `status=PAUSED` and zero budget on every create regardless of the caller; the action gate still re-reads — defence in depth — `live/meta.MetaAdsAdapter.create`.
- Students are linked to their student record by `users.student_id` — `routes/students.py`.
- Playwright tests use the latest headless Chromium (downloaded once with `playwright install chromium`) — dev tooling only.
- The dense model (bge-small-en-v1.5) was downloaded once with `scripts/fetch_embeddings.py` for the eval comparison; tests always use the hashed embedding so they stay offline — `OPERATOR_EMBEDDINGS`.
- UI theme is token-based (`web/src/styles.css`): every colour is a CSS variable with a light and a `[data-theme=dark]` value; the brand (`--brand`) drives derived tones (`--brand-solid`, `--brand-fg`, `--brand-soft`) via `color-mix`, and `applyBrand` picks dark or white button text from the brand colour's luminance — any tenant colour stays readable — change tokens in `styles.css`.
- Dark mode follows the system by default; the choice (System/Light/Dark) is saved per device in `localStorage["op.theme"]` and applied before first paint by an inline script in `index.html` — no flash, no server round trip — `web/src/theme.ts`.
- Tailwind light tints used directly by screens (`bg-green-50`, `text-amber-900` …) are remapped to dark-safe values under `[data-theme=dark]`, so a screen written without tokens is still legible in dark mode — new screens should use the tokens (`bg-good-soft`, `text-warn-ink` …).
- Inter Variable (optical sizes) is bundled from `@fontsource-variable/inter`; no font or image is fetched from another host at runtime — swap the import at the top of `styles.css`.
- Sidebar order is role-aware (`ROLE_GROUPS` / `ROLE_PRIORITY` in `pages/registry.tsx`), groups are collapsible and remembered per device (`op.nav.collapsed`), and phones get a four-page tab bar per role (`ROLE_TABBAR`) plus "More" — the full menu stays one tap away — edit the registry.
- The command palette (⌘K / Ctrl K) is built in (`components/CommandPalette.tsx`) with a small subsequence matcher instead of a fuzzy-search dependency; recent pages are kept per device in `op.recent`.
- Motion uses `motion/react` with `LazyMotion` + `MotionConfig reducedMotion="user"`, and a CSS reduced-motion override, so every transition switches off for people who ask the OS for less motion.
