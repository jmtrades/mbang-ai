# Progress

| Phase | Status | Notes |
|---|---|---|
| 0 | done | skeleton, config, /health /ready, React shell (sign-in + Pitch demo + sidebar), PWA files, run.sh |
| 1 | done | corpus loader (quality flags, bylines, timestamps, exclusions, content policy), own BM25 + local dense + RRF, /api/search, /api/source (indexed files only), 57 gold / 12 not-covered / 7 audit tasks, scripts/eval_retrieval.py — fixture corpus hit@5 1.0, hit@10 1.0 |
| 2 | done | llm.py budget gate (fails closed, any thread) + replay store + redaction; runtime traces; guardrails matrix (§2) + content policy; rubrics + self-critique loop; audit; approval queue (block → edit → approve; approve never sends: mock transport or "ready to send manually"); never-invite list; settings API 403 on BOX_ONLY; scripts/tenant_admin.py |
| 3 | done | 5 roles + tab-based RBAC, queue kind permissions, login throttle (8 fails / 15 min per IP or user), session expiry, tenant-scoped HttpOnly cookies (Secure when hosted); demo DB split (data/demo.db) + one-click Pitch demo roles + seeded fictional cohort (24 students × 6 weeks, 37 leads, 18 drafts, practice ad account) + reset with backup, refused on real DB |
