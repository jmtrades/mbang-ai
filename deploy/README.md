# Deploying Operator AI (install guide)

Nothing here has been deployed. These files are a ready bundle; going live needs a server, a
domain and the accounts listed by `scripts/preflight_live.py`.

## Option A — Docker Compose (recommended)

1. Server: any Linux VM with Docker (2 vCPU / 4 GB is plenty). Point your domain's A record at it.
2. Copy the repo to the server, then the course folders into `inputs/` (mounted read-only).
3. `cp deploy/.env.template deploy/.env` and fill in `OPERATOR_DOMAIN`, `OPERATOR_PUBLIC_URL`, and (optionally) an AI key.
4. `docker compose -f deploy/docker-compose.yml --env-file deploy/.env up -d --build`
5. Caddy obtains HTTPS certificates automatically. Check: `deploy/smoke_local.sh https://your.domain`.
6. Read the owner password: `docker compose exec app cat /app/data/tenants/owner/owner_password.txt` (then delete the file).
7. Enter integration keys in the app (Owner → Integrations). They go into the encrypted vault.
8. Live switches stay OFF until you run, on the server:
   `docker compose exec app python scripts/tenant_admin.py switch owner live_meta_enabled on --owner-yes "<your recorded reason>"`

## Option B — systemd

1. `git clone` to `/opt/operator-ai`, create `.venv` with Python 3.12+, `pip install -r requirements.lock`, `cd web && npm ci && npm run build`.
2. `cp deploy/systemd/* /etc/systemd/system/ && systemctl daemon-reload`
3. `systemctl enable --now operator-ai.service operator-ai-health.timer operator-ai-backup.timer operator-ai-logrotate.timer`
4. Put Caddy (or any HTTPS proxy) in front with `reverse_proxy 127.0.0.1:8850`; keep `OPERATOR_COOKIE_SECURE=1`.

## Operations

- Health: `GET /health` (liveness), `GET /ready` (db, index, workers, embeddings, AI state, ads adapter, allowlist, switches, tenants).
- Backups: `scripts/backup.sh` (SQLite online backup of every tenant, 20 kept, mode 0600; the vault keys go to a separate `backup-keys/` bundle, never into the archive); offsite: `deploy/offsite_backup.sh` (data archive only). Keep the keys bundle somewhere else; restore steps: `docs/SECURITY.md`.
- Restore: `scripts/restore.sh backups/operator-<stamp>.tgz --check` then without `--check` (stop the app first).
- Watchdog (non-systemd hosts): `scripts/watchdog.sh` restarts after 4 failed checks, rotates logs, backs up every 6 h.
- Preflight: `scripts/preflight_live.py` lists every go-live check as OK / MISSING without printing any value.
