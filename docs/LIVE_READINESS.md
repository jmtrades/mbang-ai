# Live readiness

Nothing has been deployed and no live switch has ever been turned on. Everything outbound is mocked or faked in
tests. This page lists what going live needs; `scripts/preflight_live.py` prints the same list as OK / MISSING for a
tenant without printing any value.

## Accounts and keys (entered in Owner → Integrations → encrypted vault)

| For | Fields | Notes |
|---|---|---|
| Meta lead ads + Ads Ops | `meta_app_id`, `meta_app_secret`, `meta_access_token`, `meta_verify_token` | A Meta app with `leads_retrieval`, `ads_read`, `ads_management`; subscribe the page to `leadgen` at `https://<domain>/t/<tid>/hooks/meta` |
| Google Calendar | `google_client_id`, `google_client_secret`, `google_refresh_token`, `google_calendar_id` | OAuth consent with `calendar.events` + `calendar.freebusy` |
| Instagram messaging | `instagram_access_token` | Real DM sending is not implemented; approved DMs are "ready to send manually" |
| Telegram | `telegram_bot_token`, `telegram_webhook_secret` | Real Bot API sending not implemented (mock transport) |
| Slack | `slack_bot_token`, `slack_signing_secret` | Real chat.postMessage not implemented (mock transport) |
| SMS / phone | `sms_account_sid`, `sms_auth_token`, `sms_webhook_secret` | Provider adapter not implemented (mock) |
| Email | `smtp_host`, `smtp_user`, `smtp_password`, `email_webhook_secret` | SMTP sending not implemented; inbound parsing is |
| Payments | `payments_api_key`, `payments_webhook_secret` | Signed webhook → member access is implemented |
| CRM | `crm_api_key`, `crm_location_id` | Only the mock CRM exists |
| AI (optional) | `ANTHROPIC_API_KEY` or `OPENAI_API_KEY` in the server env + `tenant_admin.py budget <tid> ai <tokens>` | Without it everything runs on rules / replays |

## Server

- A Linux VM, a domain with an A record, Docker (or systemd), `deploy/.env` filled in.
- `OPERATOR_COOKIE_SECURE=1` (default in the Docker image) behind Caddy's automatic HTTPS.
- Backups: `operator-ai-backup.timer` (6 h) + `deploy/offsite_backup.sh` to somewhere off the box.

## Switches (server CLI only, each with the owner's recorded yes)

```bash
python scripts/tenant_admin.py switch owner live_meta_enabled on --owner-yes "<who said yes, when, why>"
python scripts/tenant_admin.py allowlist owner add act_1234567890 --owner-yes "<recorded reason>"
python scripts/tenant_admin.py switch owner live_calendar_enabled on --owner-yes "<recorded reason>"
```

## What is real vs mock today

- REAL_OK: everything in the app on rules / retrieval / mock transports; the Meta webhook verification, lead fetch,
  Graph ads adapter and Google Calendar client are real code tested against in-process fakes of those APIs.
- NEEDS_LOGIN: any delivery to a real person/channel, real ad account changes, real calendar events.
- NEEDS_AI_BUDGET: AI drafting, AI scoring, full translation, style rewriting, critique rewrites.
