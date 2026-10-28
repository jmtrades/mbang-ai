# Operator AI — Rebuild plan

How the code gets from commit `d6c998b` to every requirement in `docs/ENTERPRISE_STANDARD.md`. Each phase lists the
requirement ids it closes, the work, and its exit check. A phase is done when its exit check passes in CI, never when
the code is written. Phases run in this order because each one makes the next measurable or safe.

The product invariants INV-1..INV-5 hold in every phase. No phase may weaken approval-before-send, the impersonation
and voice-cloning bans, the claims rules or mode labelling, even temporarily on a branch that ships.

## Starting point (measured 2026-10-28)

| Measure | Standard | At `d6c998b` |
|---|---|---|
| Python modules / lines | layered kernel · domain · api | 147 modules, 44,187 lines, one flat package |
| Modules over 800 lines | 0 | 10 (largest `prospects.py`, 2,285) |
| TSX files over 600 lines | 0 | 6 (largest `pages/Prospects.tsx`, 1,014) |
| `any` in `web/src` | 0 | 517 |
| Tests | tiered suites, gated in CI | 1,006 tests in one folder, no CI |
| Toolchain | one pinned Python minor everywhere | venv 3.14, Dockerfile 3.12, no `pyproject.toml` |
| Lint / types | ruff + mypy at zero | not configured |

## Ratchets

Many gates cannot pass on day one without a rewrite. Each such gate starts as a **ratchet**: a committed baseline file
lists today's violations, the gate fails on any new violation and on any baseline entry that has been fixed but not
removed, so the baseline can only shrink. A ratchet becomes a hard gate when its baseline is empty. Baselines live in
`tests/arch_baselines/` with one file per gate.

## Phases

### Phase 0 — Foundations (QE-1..4, QE-10 partial, ARCH-3 ratchet, FE-2 ratchet)

Work: `pyproject.toml` (project metadata, ruff, mypy, pytest config), `.python-version`, one Python minor in the
Dockerfile, `CHANGELOG.md`, `operator_ai.__version__` on `/health`; `tests/test_arch.py` with ratchets for module
size, function size, TSX size, `any` count and raw status writes, plus hard gates that already pass (no voice-clone
dependencies, interpreter pins agree); `.github/workflows/ci.yml` running lint, types (ratcheted), the suite, the
arch gates and the web typecheck/build; `.pre-commit-config.yaml`; `Makefile` with `make ci-local`.

Exit: CI green on `master`; tag `v0.2.0`.

### Phase 1 — Kernel (ARCH-1, 2, 4, 5, 13, 14, 15, 16; DATA-1, 2, 4; REL-1, 2; API-2)

Work: carve `operator_ai/kernel/` out of `db.py`, `config.py`, `tenancy.py`, `audit.py`, `guardrails.py` and the
duplicated helpers; validated settings object and `check-config`; structured JSON logging with request ids; the
problem+json error envelope; versioned migrations replacing `COLUMN_MIGRATIONS`; unit of work; `kernel.fsm` with the
queue as the first entity; import-linter contracts.

Exit: `kernel/` imports nothing outside it; the queue's only status writer is `fsm.transition`; migration tests pass
against every released schema.

### Phase 2 — Durable work (ARCH-6..11, REL-5, 9, 14; DATA-5)

Work: transactional outbox, leased jobs with retry policy and dead-letter, leased scheduler, generation-based
recovery, crash-injection markers, retention, tenant fairness.

Exit: the recovery matrix passes; kill -9 at every marker produces at most one send and no silent loss.

### Phase 3 — Security (SEC-1..20, DATA-6..10)

Work: Argon2id with re-hash, no predictable owner account, MFA (TOTP, WebAuthn, recovery codes), session list and
revoke, CSRF tokens, policy rate limiter, object-level authorization matrix, sandboxed upload parsing, SSRF pinning,
hashed time-boxed public tokens, tamper-evident audit chain, KEK-wrapped vault, subject export and erasure, CSP nonces.

Exit: `tests/security/` and `tests/test_authz_matrix.py` green; threat model in `docs/SECURITY.md`.

### Phase 4 — API v1 contract (API-1, 3..16)

Work: `/t/{tid}/api/v1` with schemas on every route, keyset pagination, idempotency keys, If-Match on decisions,
operations for long work, SSE streaming for coach and chat, scoped tokens and outbound webhooks.

Exit: `tests/contract/` green, OpenAPI snapshot committed, schemathesis finds no 5xx.

### Phase 5 — Frontend (FE-1..20)

Work: generated API types, TanStack Query, typed router, zero `any`, capability gating, live updates over SSE, form
library, i18n foundation, accessibility to WCAG 2.2 AA, size budgets, service worker rules, per-tenant identity; split
every TSX file over 600 lines. The design system in `design-system/` is the visual reference.

Exit: web CI (typecheck, lint, vitest, size-limit, Playwright with axe) green; the `any` ratchet baseline is empty.

### Phase 6 — Operations (REL-3, 4, 6, 7, 8, 10..13, 15, 16; DATA-11..14)

Work: OpenTelemetry metrics and traces, health tiers, blue/green deploy, ops alerting, SLOs with nightly load tests,
bounded index cache, connection pooling, runbooks and drills, owner ops page, continuous backups with restore drills,
Postgres parity for the kernel.

Exit: nightly perf, restore drill and kernel-postgres jobs green; `docs/SLO.md` and `docs/CAPACITY.md` generated.

### Phase 7 — AI runtime (AI-1..12)

Work: grounding gate per sentence, budget reservation, model pinning and prompt versioning, bounded executor, output
gates on every agent, injection red-team per agent, turn-aware role-play scoring, corrections memory, per-agent eval
harness in CI.

Exit: `tests/ai/` green; eval thresholds committed and enforced.

### Phase 8 — Parity and beyond (PAR-1..14)

Work: ingestion at scale (the course importer in `scripts/import_course.py` is the first piece of PAR-1), long input
and files, live-verified channels, group chat, triggered workers, integrations and MCP tools, corrections and memory,
consented persona, PWA with push, analytics, onboarding under 30 minutes, and `docs/VS_JEREMY_AI.md` regenerated from
the parity tests.

Exit: `tests/parity/` green; the comparison doc is generated, every row linked to its proving test.

## Decisions

- **Ratchets before rewrites.** A gate lands first as a ratchet so CI is green from Phase 0 and every later change is
  measured. Reason: a standard that fails on day one gets switched off; one that only blocks regressions gets kept.
- **Python 3.14 is the pinned minor.** The development venv, the lock file and every wheel already work on 3.14; the
  Dockerfile moves from 3.12 to match. Reason: QE-3 needs one interpreter, and 3.14 is the one the suite runs on.
- **Monolith, not services.** Three process roles over one database (ARCH-7) give the isolation the standard needs
  without a network hop per call. Reason: a single-box deployment is the product's main mode.
- **SQLite per tenant stays the self-hosted default**; Postgres arrives through the dialect seam for hosted
  deployments (DATA-13). Reason: tenant isolation by file is simple to back up, restore and delete.
- **Course material never enters the repository.** `inputs/` is git-ignored; imports record a rights statement
  (`owner` or `licensed`) in `inputs/import_log.json`. Reason: the knowledge base must belong to the person running it.
