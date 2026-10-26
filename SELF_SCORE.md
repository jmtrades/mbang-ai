# SELF_SCORE

Scored against §12 (0 = missing, 1 = partial/stub, 2 = works and tested) and §11. Status labels:
**REAL_OK** works now on real or practice/mock paths · **NEEDS_LOGIN** built on a mock, only a key or account is
missing · **NEEDS_AI_BUDGET** rules/replay path works, the rest needs a model.

The same rows drive the in-app **Go-live scorecard** (`operator_ai/scorecard.py`, rendered here by
`scripts/self_score.py`). `tests/test_phase10_ops.py::test_scorecard_evidence_exists` checks that every test cited
below exists.

**Read this first — what these numbers are measured on.** No `inputs/` folder was present in the build
environment, so every retrieval, honesty and reviewer number below comes from the small fictional fixture corpus in
`tests/fixtures/corpus/` (6 lessons, 4 SOPs, 1 PDF-text SOP, 1 summary) and from eval sets written alongside the code.
They prove the pipeline works; they do **not** prove the targets on Junior's ~570k-word corpus. Re-run
`scripts/eval_retrieval.py` after copying the course into `inputs/` (and extend `tests/gold_questions.json` with
questions about that material).

### A. Parity rows

| ID | Item | Score | Status | Evidence | Notes |
|---|---|---|---|---|---|
| A1 | Chat in the creator's voice (cited, honest 'not covered', replay when AI off) | 2 | NEEDS_AI_BUDGET | `tests/test_phase4_agents.py::test_thunder_dome_cited_sop_answer`<br>`tests/test_phase4_agents.py::test_tiktok_shop_not_covered`<br>`tests/test_phase4_agents.py::test_ai_answer_then_labelled_replay_when_ai_off` | Rules path: extractive answers from passages; style needs AI.; waiting for: AI key + ai_token_budget for voice/style rewriting |
| A2 | Files, screenshots, links (uploads + local OCR + receipt) | 1 | REAL_OK | `tests/test_phase8_platform.py::test_upload_kinds_and_receipt`<br>`tests/test_phase8_platform.py::test_image_ocr_local`<br>`tests/test_phase8_platform.py::test_upload_pdf_and_docx` | OCR via tesseract (Docker image) or macOS Vision. Links are not fetched (no outbound web access); paste the text instead. |
| A3 | Threads and history | 2 | REAL_OK | `tests/test_phase8_platform.py::test_threads_title_rename_search_archive_share` |  |
| A4 | Projects and memory (scoped, user-deletable) | 2 | REAL_OK | `tests/test_phase8_platform.py::test_projects_scoped_user_deletable_memory` |  |
| A5 | Business profile (8 sections, read by every skill) | 2 | REAL_OK | `tests/test_phase8_platform.py::test_profile_8_sections_read_by_skills`<br>`tests/test_phase6_chains_studio_ads.py::test_brief_builder_reports_missing_and_reads_profile` |  |
| A6 | Skills library | 2 | REAL_OK | `tests/test_phase8_platform.py::test_skills_library_size` |  |
| A7 | Workers (run on input or name the missing login) | 2 | REAL_OK | `tests/test_phase8_platform.py::test_worker_names_missing_login` | waiting for: instagram_access_token for the posting worker |
| A8 | Triggers (schedule + on-source-change) | 2 | REAL_OK | `tests/test_phase8_platform.py::test_daily_trigger_runs_due_workers`<br>`tests/test_phase8_platform.py::test_on_source_change_trigger_drafts_only` |  |
| A9 | Bottleneck diagnosis (pure maths, cited benchmarks) | 2 | REAL_OK | `tests/test_phase4_agents.py::test_diagnostic_pure_maths`<br>`tests/test_phase4_agents.py::test_diagnose_cited_with_next_best_action` |  |
| A10 | Ad, VSL and webinar diagnosis | 2 | REAL_OK | `tests/test_phase4_agents.py::test_reviewer_flaw_detection`<br>`tests/test_phase6_chains_studio_ads.py::test_diagnose_is_read_only` | Heuristic scoring when AI is off, labelled.; waiting for: AI budget for per-criterion AI scoring |
| A11 | Sales-call analysis (consent → transcript → score → drills) | 2 | REAL_OK | `tests/test_phase8_platform.py::test_call_review_requires_every_party_consent` | Transcripts are pasted/uploaded; audio transcription needs a model (not bundled). |
| A12 | Lead Rescue Desk (anonymous ids) | 2 | REAL_OK | `tests/test_phase5_ops_agents.py::test_rescue_desk_anonymous` |  |
| A13 | Execution chain (webinar chain, linked queue items) | 2 | REAL_OK | `tests/test_phase6_chains_studio_ads.py::test_webinar_chain_produces_linked_queue_items`<br>`tests/test_phase6_chains_studio_ads.py::test_rerun_only_dependents` |  |
| A14 | Web voice (consent, quota, transcript, recap; mock) | 2 | NEEDS_LOGIN | `tests/test_phase8_platform.py::test_web_voice_consent_quota_transcript_recap` | Mock: transcript typed in; consent and quota enforced.; waiting for: a speech-to-text provider for real audio |
| A15 | Phone and SMS (opt-in first, fictional numbers; mock) | 2 | NEEDS_LOGIN | `tests/test_phase8_platform.py::test_sms_opt_in_first_and_fictional_numbers` | waiting for: sms_account_sid, sms_auth_token (SMS provider) |
| A16 | Telegram (link code, ask, check-in, group; mock after approval) | 2 | NEEDS_LOGIN | `tests/test_phase8_platform.py::test_telegram_link_ask_checkin_group` | waiting for: telegram_bot_token |
| A17 | Slack (signature check, DM, mention; mock) | 2 | NEEDS_LOGIN | `tests/test_phase8_platform.py::test_slack_signature` | waiting for: slack_bot_token, slack_signing_secret |
| A18 | Email channel (inbound parse, quote strip, drafts only) | 2 | NEEDS_LOGIN | `tests/test_phase8_platform.py::test_email_inbound_parse_quote_strip_drafts_only` | waiting for: smtp_host, smtp_user, smtp_password, an inbound email webhook |
| A19 | MCP server (read-only, token auth) | 2 | REAL_OK | `tests/test_phase8_platform.py::test_mcp_read_only_token_auth` |  |
| A20 | Meta Ads (diagnose read-only, propose PAUSED; mock; live client coded and off) | 2 | NEEDS_LOGIN | `tests/test_phase6_chains_studio_ads.py::test_propose_queues_only`<br>`tests/test_phase9_live_tenancy.py::test_meta_ads_adapter_forces_paused` | waiting for: meta_access_token, meta_app_secret, live_meta_enabled + allowlist (server CLI) |
| A21 | CRM / GoHighLevel-style (mock; terminal moves need approval) | 2 | NEEDS_LOGIN | `tests/test_phase8_platform.py::test_crm_terminal_moves_need_approval` | Mock CRM only; a real GoHighLevel adapter is not written.; waiting for: crm_api_key, crm_location_id (real CRM adapter not built) |
| A22 | App marketplace / integrations list mapped to vault | 2 | REAL_OK | `tests/test_phase8_platform.py::test_integrations_list`<br>`tests/test_phase8_platform.py::test_vault_api_set_not_set_only` |  |
| A23 | General sandbox / Studio (7 lines from rules; free-form needs AI) | 2 | REAL_OK | `tests/test_phase6_chains_studio_ads.py::test_each_studio_line_queues_reviewed_drafts` | waiting for: AI budget for free-form drafts |
| A24 | KB admin (licence, provenance, priority, notes) | 2 | REAL_OK | `tests/test_phase8_platform.py::test_kb_admin_labels_priority_notes` |  |
| A25 | Source sync (local Drive inbox: list → diff → fetch → screen → stage) | 2 | REAL_OK | `tests/test_phase8_platform.py::test_drive_list_diff_fetch_screen_stage` | Local inbox folder; Google Drive API sync not built.; waiting for: drive_folder_id for a real Google Drive sync |
| A26 | Personality (name, limits, banned words always enforced) | 2 | REAL_OK | `tests/test_phase8_platform.py::test_personality_banned_words_always_enforced`<br>`tests/test_phase8_platform.py::test_daily_question_limit` | Style rewriting needs AI.; waiting for: AI budget for style |
| A27 | Branding and embed (white-label, embed widget, local preview) | 2 | REAL_OK | `tests/test_phase8_platform.py::test_mini_scope_limit_consent_embed`<br>`tests/test_phase11_quality.py::test_no_hardcoded_brand_names` |  |
| A28 | Access and roles (5 roles, RBAC matrix test) | 2 | REAL_OK | `tests/test_phase3_auth_demo.py::test_rbac_api`<br>`tests/test_phase11_quality.py::test_rbac_matrix_every_tab` |  |
| A29 | Members and payments (signed mock webhook → access) | 2 | NEEDS_LOGIN | `tests/test_phase8_platform.py::test_signed_payment_webhook_grants_and_revokes_access` | waiting for: payments_api_key, payments_webhook_secret (payment provider) |
| A30 | Recent queries + unanswered list | 2 | REAL_OK | `tests/test_phase8_platform.py::test_recent_and_unanswered_queries` |  |
| A31 | Engagement alerts | 2 | REAL_OK | `tests/test_phase8_platform.py::test_engagement_alerts` |  |
| A32 | Onboarding (gap analysis → cited 30/60/90 → queued welcome) | 2 | REAL_OK | `tests/test_phase5_ops_agents.py::test_onboarding_flow` |  |
| A33 | Lead-magnet minis (scope, question limit, consent, embed) | 2 | REAL_OK | `tests/test_phase8_platform.py::test_mini_scope_limit_consent_embed` |  |
| A34 | Auto-translation (FR/ES/PT detect + glossary) | 2 | REAL_OK | `tests/test_phase4_agents.py::test_detect_language`<br>`tests/test_phase4_agents.py::test_translated_question_answered_with_note` | waiting for: AI budget to translate full answers |
| A35 | Brief builder | 2 | REAL_OK | `tests/test_phase6_chains_studio_ads.py::test_brief_builder_reports_missing_and_reads_profile` |  |
| A36 | Approvals (guardrail holds; approve never sends) | 2 | REAL_OK | `tests/test_phase2_core.py::test_queue_block_edit_approve_never_sends` |  |
| A37 | Accountability (risk, nudges queued, per-coach, 2,000 students) | 2 | REAL_OK | `tests/test_phase5_ops_agents.py::test_board_2000_students_fast`<br>`tests/test_phase5_ops_agents.py::test_interventions_queue_nudges_and_escalate` |  |
| A38 | Mobile (installable PWA, offline shell) | 2 | REAL_OK | `tests/test_ui_playwright.py::test_pwa_installable_and_offline_shell`<br>`tests/test_ui_playwright.py::test_every_page_every_role` |  |

### B. Beyond parity

| ID | Item | Score | Status | Evidence | Notes |
|---|---|---|---|---|---|
| B1 | Studio 7 lines | 2 | REAL_OK | `tests/test_phase6_chains_studio_ads.py::test_each_studio_line_queues_reviewed_drafts` |  |
| B2 | Next best action | 2 | REAL_OK | `tests/test_phase6_chains_studio_ads.py::test_next_best_action_opens_studio_line` |  |
| B3 | Launch kit | 2 | REAL_OK | `tests/test_phase6_chains_studio_ads.py::test_launch_kit_queues_every_line` |  |
| B4 | Go-live scorecard screen | 2 | REAL_OK | `tests/test_phase10_ops.py::test_golive_scorecard` |  |
| B5 | Morning brief | 2 | REAL_OK | `tests/test_phase10_ops.py::test_morning_brief_ranked_read_only` |  |
| B6 | AI departments builder (§11 script) | 2 | REAL_OK | `tests/test_builder_headless.py::test_headless_click_through_three_times` | waiting for: AI budget for the AI pass (rules draft used otherwise) |
| B7 | Outreach loop + opener lab + weekly funnel + setter league + goal calculator | 2 | REAL_OK | `tests/test_phase6_chains_studio_ads.py::test_opener_approve_goes_to_mock_instagram_and_tracks_stage` | waiting for: instagram_access_token for real sends |
| B8 | Ads Ops PAUSED-only + rollback | 2 | REAL_OK | `tests/test_phase6_chains_studio_ads.py::test_build_test_paused_zero_budget_and_rollback`<br>`tests/test_phase6_chains_studio_ads.py::test_create_forced_back_to_paused_and_failed` |  |
| B9 | Self-critique loop with budgets | 2 | NEEDS_AI_BUDGET | `tests/test_phase2_core.py::test_critique_rewrites_with_budget_max_two`<br>`tests/test_phase2_core.py::test_critique_without_budget_sets_needs_human` | waiting for: critique_token_budget > 0 and an AI key |
| B10 | Webinar rehearsal scorer | 2 | REAL_OK | `tests/test_phase6_chains_studio_ads.py::test_rehearsal_scorer` |  |
| B11 | Objection drill bank (spaced repetition) | 2 | REAL_OK | `tests/test_phase8_platform.py::test_drills_spaced_repetition` |  |
| B12 | Retention Star | 2 | REAL_OK | `tests/test_phase5_ops_agents.py::test_retention_star` |  |
| B13 | Provenance panel | 2 | REAL_OK | `tests/test_phase8_platform.py::test_provenance_panel` |  |

### C. Guardrails and safety

| ID | Item | Score | Status | Evidence | Notes |
|---|---|---|---|---|---|
| C1 | Nothing sends without approval (approve never sends to a real channel) | 2 | REAL_OK | `tests/test_phase2_core.py::test_queue_block_edit_approve_never_sends`<br>`tests/test_phase9_live_tenancy.py::test_switch_off_means_no_outbound_calls` |  |
| C2 | No impersonation / banned-names scrub | 2 | REAL_OK | `tests/test_guardrails.py::test_banned_names_block_and_scrub`<br>`tests/test_phase11_quality.py::test_name_leak_sweep` |  |
| C3 | Fake-proof and deception rules incl. negation and reasoned-deadline review path | 2 | REAL_OK | `tests/test_guardrails.py::test_deception_blocks`<br>`tests/test_guardrails.py::test_negated_and_clean_text_passes`<br>`tests/test_guardrails.py::test_deadline_without_reason_is_review_not_block` |  |
| C4 | Outreach money rule vs client-asset rule | 2 | REAL_OK | `tests/test_guardrails.py::test_outreach_allowed_hedged_range_for_warm_lead`<br>`tests/test_guardrails.py::test_client_asset_may_state_price`<br>`tests/test_guardrails.py::test_outreach_rule_wins_when_both` |  |
| C5 | Demo labelled + separate DB + reset refused on real | 2 | REAL_OK | `tests/test_phase3_auth_demo.py::test_demo_reset_refused_on_real`<br>`tests/test_phase3_auth_demo.py::test_real_workspace_has_no_demo_records` |  |
| C6 | Secrets: vault AES-GCM with tenant AD, redaction, set/not set APIs | 2 | REAL_OK | `tests/test_phase8_platform.py::test_vault_aes_gcm_tenant_bound`<br>`tests/test_phase2_core.py::test_redact_token_patterns` |  |
| C7 | Course KB owner-tenant only (LicenceError) | 2 | REAL_OK | `tests/test_phase9_live_tenancy.py::test_licence_error_on_course_for_clients` |  |
| C8 | Live switches OFF, server-CLI-only with recorded yes, 403 from API | 2 | REAL_OK | `tests/test_phase2_core.py::test_settings_api_refuses_box_only`<br>`tests/test_phase2_core.py::test_switch_on_needs_recorded_yes` |  |
| C9 | Never-invite list (canonicalised, checked twice) | 2 | REAL_OK | `tests/test_phase2_core.py::test_never_invite_checked_on_propose_and_send`<br>`tests/test_phase9_live_tenancy.py::test_invite_blocked_at_send_time` |  |
| C10 | AI budget 0 fails closed (no request leaves) | 2 | REAL_OK | `tests/test_phase2_core.py::test_budget_zero_fails_closed_from_any_thread` |  |
| C11 | KB content policy (tactic passages excluded and listed) | 2 | REAL_OK | `tests/test_phase1_retrieval.py::test_content_policy_excludes_tactics`<br>`tests/test_phase4_agents.py::test_not_used_passages_listed` |  |
| C12 | Audit log coverage | 2 | REAL_OK | `tests/test_phase2_core.py::test_audit_covers_queue_lifecycle`<br>`tests/test_phase11_quality.py::test_audit_coverage_all_kinds` |  |

### D. Platform and ops

| ID | Item | Score | Status | Evidence | Notes |
|---|---|---|---|---|---|
| D1 | Multi-tenant routing + cross-tenant 404 | 2 | REAL_OK | `tests/test_phase9_live_tenancy.py::test_cross_tenant_404` |  |
| D2 | Live chain E2E for 3 tenants against fakes; stray host fails | 2 | NEEDS_LOGIN | `tests/test_phase9_live_tenancy.py::test_chain_end_to_end_three_tenants`<br>`tests/test_phase0.py::test_stray_host_blocked` | waiting for: Meta app + page + lead form, Google OAuth client + refresh token, switches on via CLI |
| D3 | Webhook HMAC + idempotency + retries/backoff | 2 | REAL_OK | `tests/test_phase9_live_tenancy.py::test_meta_webhook_signature_and_size`<br>`tests/test_phase9_live_tenancy.py::test_get_retries_429_and_5xx_and_meta_rate_codes`<br>`tests/test_phase9_live_tenancy.py::test_inbox_gives_up_after_8_attempts` |  |
| D4 | Rules fallback for every agent when AI is unavailable (labelled) | 2 | REAL_OK | `tests/test_phase11_quality.py::test_every_agent_runs_with_ai_off` |  |
| D5 | Token budgets (ai_token_budget, critique_token_budget, builder daily cap) | 2 | REAL_OK | `tests/test_phase2_core.py::test_budget_exhausted_stops`<br>`tests/test_builder_headless.py::test_builder_daily_cap_and_switch` |  |
| D6 | /health + /ready | 2 | REAL_OK | `tests/test_phase0.py::test_ready_reports_components` |  |
| D7 | Watchdog + backup/restore (tested in temp dir) + log rotation | 2 | REAL_OK | `tests/test_phase10_ops.py::test_backup_restore_roundtrip`<br>`tests/test_phase10_ops.py::test_watchdog_restarts_dead_process` | waiting for: a server to install the systemd timers on |
| D8 | Deploy bundle + preflight_live.py | 2 | REAL_OK | `tests/test_phase10_ops.py::test_deploy_bundle_lint`<br>`tests/test_phase9_live_tenancy.py::test_preflight_lists_missing_without_values` | Linted, not built: the Docker image was not built in this environment.; waiting for: a server, a domain and DNS (not deployed) |
| D9 | Login throttle + session expiry + secure cookies | 2 | REAL_OK | `tests/test_phase3_auth_demo.py::test_login_throttle_per_ip`<br>`tests/test_phase3_auth_demo.py::test_session_expiry`<br>`tests/test_phase3_auth_demo.py::test_secure_cookie_when_hosted` |  |
| D10 | API sweep: no 5xx, 422 on bad bodies | 2 | REAL_OK | `tests/test_phase11_quality.py::test_api_sweep` |  |
| D11 | Load test + 500-lead burst | 2 | REAL_OK | `tests/test_phase5_ops_agents.py::test_burst_500_zero_failures`<br>`scripts/load_test.py` |  |
| D12 | Playwright all pages × all roles × desktop/mobile | 2 | REAL_OK | `tests/test_ui_playwright.py::test_every_page_every_role`<br>`screens/` |  |

### E. Quality

| ID | Item | Score | Status | Evidence | Notes |
|---|---|---|---|---|---|
| E1 | Retrieval hit@10 / hit@5 on the gold set | 1 | REAL_OK | `tests/test_phase1_retrieval.py::test_retrieval_eval_meets_targets` | waiting for: Junior's corpus in inputs/ (measured on the fictional fixture corpus only) |
| E2 | Out-of-scope honesty | 1 | REAL_OK | `tests/test_phase4_agents.py::test_out_of_scope_honesty_and_in_scope` | waiting for: Junior's corpus in inputs/ |
| E3 | Zero invented citations | 2 | REAL_OK | `tests/test_phase4_agents.py::test_invented_citation_stripped`<br>`tests/test_phase11_quality.py::test_zero_invented_citations_on_gold_set` |  |
| E4 | Reviewer flaw detection on audit tasks | 1 | REAL_OK | `tests/test_phase4_agents.py::test_reviewer_flaw_detection` | 1.0 on 7 tasks written alongside the rules (optimistic).; waiting for: harder, independently written audit tasks |
| E5 | Test count and pass rate | 2 | REAL_OK | `pytest summary in SELF_SCORE.md` |  |
| E6 | Docs complete and honest | 2 | REAL_OK | `README.md`<br>`docs/` |  |

### Totals

| Section | Score | Max |
|---|---|---|
| A. Parity rows | 75 | 76 |
| B. Beyond parity | 26 | 26 |
| C. Guardrails and safety | 24 | 24 |
| D. Platform and ops | 24 | 24 |
| E. Quality | 9 | 12 |
| **All** | **158** | **162** |

## Eval numbers (fixture corpus — see the note above)

| Metric | Target | Hashed local embedding (tests) | bge-small-en-v1.5 (local, cached) |
|---|---|---|---|
| Retrieval hit@5 (57 gold questions, 3 multi-part) | ≥ 0.90 | **1.00** | **0.93** |
| Retrieval hit@10 | ≥ 0.95 | **1.00** | **1.00** |
| Out-of-scope honesty (12 off-curriculum questions) | ≥ 0.9 | **1.00** | **1.00** |
| In-scope questions answered (not "not covered") | — | 1.00 | 1.00 |
| Reviewer flaw detection (7 flawed assets, 18 known flaws) | ≥ 0.8 | **1.00** (18/18) | 1.00 |
| Invented citations | 0 | **0** (gold set; plus a fake-model test where an invented `[S99]` is stripped) | — |

Source: `docs/evidence/evals.txt`. The reviewer score is optimistic: the 7 audit tasks were written alongside the
rules that detect them.

## Load-test numbers (`scripts/load_test.py` against a running server, local Mac)

| Check | Target | Result |
|---|---|---|
| `/health` | ≥ 300 RPS | **4,379 RPS** (21,906 requests, 0 errors, 16 threads, 5 s) |
| Accountability dashboard over 2,000 DEMO students | p95 < 1 s | **p95 65 ms** (p50 56 ms) |
| 500-lead burst through the job queue (template drafts, no AI) | 0 failures | **500 done, 0 failed, 3.6 s** (hot 116 / warm 283 / cold 101) |
| API sweep (`scripts/api_sweep.py`, every route, malformed bodies) | no 5xx | **398 requests, 0 problems** |

Sources: `docs/evidence/load_test.txt`, `docs/evidence/api_sweep.txt`.

## Tests

- **327 passed, 0 failed** (`.venv/bin/pytest`, 2 m 45 s; `docs/evidence/pytest_summary.txt`), all offline: AI off,
  temp data dir, in-process fakes for Meta / Google / the model; any socket to a non-loopback host raises.
- Browser: `tests/test_ui_playwright.py` visits every page for every role at 1440×900 and 390×844 — **159 screens**
  with no console errors, no error boxes and no horizontal overflow on mobile (screenshots in `screens/desktop/` and
  `screens/mobile/`), plus a real offline check of the PWA shell.
- 1 warning: Starlette deprecates `httpx` in its TestClient (cosmetic).

## §11 demo result

- `scripts/builder_demo_click.py --times 3` against a running server: **CLEAN 3/3** (includes the bonus: bookkeeping
  support agent escalates the refund and legal tickets; gym ad analyst labels every $ figure DEMO).
  `docs/evidence/builder_demo_click.txt`
- `tests/test_builder_headless.py::test_headless_click_through_three_times`: passes 3/3 with AI off.

## Known gaps (honest)

1. **Measured on a fictional corpus.** All quality numbers above. Junior's real transcripts (garbled Whisper output,
   ~100 files) will be harder; expect to tune the coverage-judge thresholds and synonyms.
2. **No real sending anywhere.** Instagram DMs, Telegram, Slack, SMS/phone and email replies are mock transports;
   approved items are "ready to send manually". Only the Google Calendar insert and Meta Graph calls are real client
   code (tested against fakes, never against the real APIs).
3. **Links are not fetched** (A2 scored 1): there is no outbound web access by design; paste the text instead.
4. **No audio transcription.** Voice notes, web voice and sales calls take text/SRT/VTT transcripts.
5. **CRM is mock only.** No GoHighLevel adapter; no Google Drive API sync (the "Drive inbox" is a local folder).
6. **AI paths are tested only against a fake model.** No real model call was ever made (budget 0, no key). The AI
   prompts (coach JSON answers, reviewer criteria, critique rewrites, builder pass) have not been tuned on real output.
7. **Rules-mode quality is basic.** Extractive answers stitch 2–4 source sentences; call-review stage scores are
   keyword cues; the coverage judge is lexical. Good enough to be honest and cited, not as good as an AI answer.
8. **Optional items not built:** the LLM rerank stage (RRF-fused), external "best practice" `[X#]` sources (the
   third-party switch exists and is off), full answer translation without AI (a note in the student's language is
   added instead).
9. **Deploy bundle not exercised.** The Docker image was not built (no Docker in this environment); compose, Caddy and
   systemd files are linted only. The watchdog, backup/restore and log rotation are tested locally.
10. **Single-process design.** SQLite + in-process worker threads suit one server; horizontal scaling would need a
    shared database and queue.
11. **Python 3.14** was used (3.15 was too new for some wheels); the Dockerfile uses 3.12. `requirements.lock` is a
    pip freeze from 3.14.

## What going live needs (no values)

- **Server:** a Linux VM (2 vCPU / 4 GB), Docker or systemd, persistent disk for `data/` and `backups/`.
- **Domain:** an A record for the app; Caddy obtains HTTPS automatically; set `OPERATOR_DOMAIN`, `OPERATOR_PUBLIC_URL`,
  keep `OPERATOR_COOKIE_SECURE=1`.
- **Course material:** the course folders copied into `inputs/` (mounted read-only).
- **AI (optional):** an Anthropic or OpenAI API key in the server environment, then
  `scripts/tenant_admin.py budget <tid> ai <tokens> --owner-yes "<reason>"` (and `critique` for rewrites).
- **Meta:** an app with lead-retrieval and ads permissions, a page subscribed to `leadgen`, an access token, the app
  secret and a verify token (entered in Integrations → vault), then the live switch and an allowlisted ad account via
  `scripts/tenant_admin.py … --owner-yes`.
- **Google Calendar:** an OAuth client with `calendar.events` + `calendar.freebusy`, a refresh token, a calendar id;
  then the live calendar switch.
- **Real senders (not built yet):** Instagram messaging, Telegram Bot API, Slack, an SMS/phone provider and SMTP
  delivery adapters, plus their credentials; a payments provider for the signed membership webhook.
- **Operations:** enable the health, backup (6 h) and logrotate timers; configure `deploy/offsite_backup.sh`.
- Run `scripts/preflight_live.py` — today it reports **5/23 OK, 18 MISSING** for a fresh install
  (`docs/evidence/preflight_live.txt`).
