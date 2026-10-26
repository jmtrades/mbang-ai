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
