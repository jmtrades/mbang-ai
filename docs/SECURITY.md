# Security

How Operator AI protects accounts, tenants, secrets and the public demo. Every control below has tests
(`tests/test_hardening.py` unless noted). Content guardrails are in `docs/GUARDRAILS.md`.

## Sign-in and sessions

- Passwords: scrypt (n=2^14, r=8, p=1), constant-time compare. An unknown username costs the same scrypt work as a
  wrong password, so response timing doesn't reveal which usernames exist.
- Throttle: 8 failed sign-ins per 15 minutes per IP or per username → 429.
- MCP (`/mcp`): at most 20 calls per JSON-RPC batch and 120 calls a minute per network (`OPERATOR_LIMIT_MCP`).
- Sessions: a random 256-bit token; only its SHA-256 is stored. Expiry `OPERATOR_SESSION_HOURS` (12 h). Deactivated
  users lose access at once. Logout deletes the session server-side.
- **No session fixation:** every sign-in (password or demo one-click) creates a new server-side session and ends any
  session the browser presented.
- Cookie `op_s`: `HttpOnly`, `SameSite=Lax`, `Path=/t/<tenant>` (a session never reaches another tenant's API), and
  `Secure` when `OPERATOR_COOKIE_SECURE=1` (the deploy bundle sets it) or whenever the request arrived over HTTPS.
- A session token from one tenant used on another answers 404 (it never reveals anything).

## Cross-site requests (CSRF)

Defence in depth on top of `SameSite=Lax`: every state-changing API call (`POST`/`PUT`/`PATCH`/`DELETE` under
`/t/<tenant>/api/`) whose `Origin` (or, failing that, `Referer`) names another site, or `null`, is refused with 403.
Allowed: the request's own `Host`, `X-Forwarded-Host`, `OPERATOR_PUBLIC_URL` and the comma-separated
`OPERATOR_ALLOWED_ORIGINS`. Requests with neither header (server-to-server, scripts) are judged by the cookie alone.
FastAPI only parses JSON bodies sent as `application/json`, so a cross-site HTML form can't post JSON either.

## Access control

- Every tenant API route requires a session except `/public` and the `/auth/*` sign-in routes. A test walks every
  route's dependencies and fails on any new route without one (`test_every_tenant_api_route_needs_a_session`;
  signed webhooks, MCP and the public minis are the only other session-free routes, each with its own check). Each route needs the tab its page belongs to (`deps.require("<tab>")`); owner-only
  actions use `require_owner`. Non-owners only see their own rows (queue items they own, their students, their threads).
- Run traces carry private data (a student's prospects, their questions to the coach, their clients). The owner reads
  every trace; a coach reads their own and those of the people they coach (students.coach_id, users.coach_id, team
  assignments), plus the trace of a reply that arrived in one of those people's inboxes; everyone else only their own.
  Actors are matched exactly (`name(role)`), never by prefix. A coach's student digest, sweep, Retention Star and
  Morning brief cover only their own students (no clients, no logins). `tests/test_isolation_review.py`.
- Tenant ids come from the URL and must match `^[a-z0-9][a-z0-9_-]{1,31}$`; anything else is simply "not found"
  before any database or file is touched.
- KB admin (exclusions, provenance, priority, Drive inbox) and proof records: owner only, refused in the demo.

## Files and paths

- `/source` serves only files that are in the tenant's index, looked up by their exact indexed path (no traversal).
- Static files: a path must resolve inside `web/dist` (checked with `Path.is_relative_to`, not a string prefix, so
  `../dist-old/x` is refused).
- Uploaded evidence files keep only a sanitised base name, are stored under the tenant folder (mode 600) and are served
  back to the owner with `X-Content-Type-Options: nosniff` and a sandboxing CSP.
- Response headers: `X-Content-Type-Options: nosniff`, `Referrer-Policy: strict-origin-when-cross-origin`,
  `X-Frame-Options: SAMEORIGIN` on the app (the public minis under `/m/` stay embeddable), `Cache-Control: no-store`
  on API responses.

## Input handling

- Malformed bodies get 422, never 500. JSON containing `NaN` or `Infinity` is refused for every endpoint.
- User-input errors raised below the routes (`operator_ai/errors.InputError`) become plain-English 4xx answers.
- Settings are validated by shape before they are stored (`docs/GUARDRAILS.md`, "Settings have shapes"); old
  malformed values are ignored rather than breaking the response scrub.
- The diagnostic refuses negative, non-finite and absurdly large numbers; check-ins clamp their numbers (so a
  `/checkin 99999999999999999999` message can't overflow the database); onboarding refuses a blank name before
  writing anything; the accountability sweep skips (and logs) a bad row instead of failing for everyone.

## The public pitch demo

Anyone on the internet can sign in to `/t/demo/` with one click, as any role, including owner. So:

- **Its own AI budget.** The demo never inherits the server-wide `OPERATOR_AI_TOKEN_BUDGET`. It gets
  `min(server default, OPERATOR_DEMO_AI_TOKEN_BUDGET)` (default cap 20,000 tokens; critique 5,000). A reset keeps
  both the budgets **and what has been used of them**, so resetting never refills the budget. Only the server CLI can
  change it: `scripts/tenant_admin.py budget demo ai <tokens> --owner-yes "<reason>"`.
- **Rate limits** (per client IP; uvicorn's `--proxy-headers` supplies the real IP behind Caddy): 20 new demo
  sessions per 10 minutes (switching roles with a live session is a swap, not a new session); 3 resets per hour per
  IP and 12 per hour overall. Tunable with `OPERATOR_LIMIT_DEMO_LOGIN`, `OPERATOR_LIMIT_DEMO_RESET`,
  `OPERATOR_LIMIT_DEMO_RESET_ALL`. Counters are in memory (one process); a restart clears them.
- **Backups capped.** Each reset backs up the demo DB first; only the newest 5 copies are kept.
- **Never the real course.** The demo reads the fictional fixture corpus even when `inputs/` holds the real course,
  unless the owner turns on the server-only switch:
  `scripts/tenant_admin.py switch demo demo_course_material on --owner-yes "<recorded reason>"`.
- **Read-only where it matters.** KB admin, Drive sync, uploads to the knowledge base and proof records can't be
  changed in the demo (403 with a plain note; the upload checkbox is hidden there).
- **Heavy runs budgeted.** Evals, the lead burst (at most 500 leads in the demo) and the Academy build share 6 runs per
  hour per network and 60 overall (`OPERATOR_LIMIT_DEMO_HEAVY`, `OPERATOR_LIMIT_DEMO_HEAVY_ALL`): the demo runs in the
  same process as every real workspace.
- **Public demo pages say so.** The webinar sign-up, client intake, mini and proposal pages served for the demo carry a
  "Pitch demo (DEMO)" notice, and their forms take only made-up addresses ending in `@example.invalid` (anyone can sign
  in as the demo owner and read what was typed). The demo's logo can only be a path on this site, never another
  site's image (every visitor's browser would fetch it).

## Secrets

- Integration secrets live in a per-tenant AES-256-GCM vault (`vault.bin`), with a fresh nonce on every write and
  the tenant id as associated data; the key file `vault.key` is mode 600. APIs only say "set / not set".
- `redact()` removes API keys, tokens and `Bearer …` values from errors, logs and the audit log.
- Live switches and AI budgets are server-only (`scripts/tenant_admin.py`, audited with the owner's recorded yes).

## Backups and the vault keys

`scripts/backup.sh` (or `scripts/backup.py`) writes two things:

1. `backups/operator-<stamp>.tgz` (mode 600): every database, the encrypted vault blobs (`vault.bin`), tenant KB
   folders and exclusions. It **never** contains `vault.key` or the first-start `owner_password.txt`.
2. `backup-keys/keys-<stamp>.tgz` (mode 600, folder mode 700): the vault keys only, in a separate folder
   (`--keys-dest` or `OPERATOR_BACKUP_KEYS_DIR` to put it elsewhere; it may not be inside the archive folder).

`deploy/offsite_backup.sh` copies only the data archive, and refuses an archive that contains a vault key. An
offsite copy therefore holds ciphertext without the keys that open it. **Keep the keys bundle somewhere else**: a
password manager, a second machine, or an encrypted USB drive. In Docker the keys folder is its own volume
(`operator-backup-keys`).

### Restore procedure

1. Stop the app (the watchdog / systemd restarts it afterwards).
2. Check the archive: `scripts/restore.sh backups/operator-<stamp>.tgz --check` (every database must say `ok`).
3. Restore:
   - same machine, keys unchanged: `scripts/restore.sh backups/operator-<stamp>.tgz` — the keys are taken from the
     matching `backup-keys/keys-<stamp>.tgz`, or else from the data folder being replaced;
   - new machine: copy the archive and its keys bundle over, then
     `scripts/restore.py backups/operator-<stamp>.tgz --keys /secure/place/keys-<stamp>.tgz`.
4. The previous data folder is kept as `data.pre-restore-<stamp>`. If the output names tenants whose vault key was
   found nowhere, sign in to each and re-enter its integration secrets (Owner → Integrations); everything else is
   restored.

## Reporting

Security issues: contact the programme owner directly; don't open a public issue. Include the steps to reproduce.
