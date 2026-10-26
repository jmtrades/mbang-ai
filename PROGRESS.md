# Progress

| Phase | Status | Notes |
|---|---|---|
| 0 | done | skeleton, config, /health /ready, React shell (sign-in + Pitch demo + sidebar), PWA files, run.sh |
| 1 | done | corpus loader (quality flags, bylines, timestamps, exclusions, content policy), own BM25 + local dense + RRF, /api/search, /api/source (indexed files only), 57 gold / 12 not-covered / 7 audit tasks, scripts/eval_retrieval.py — fixture corpus hit@5 1.0, hit@10 1.0 |
| 2 | done | llm.py budget gate (fails closed, any thread) + replay store + redaction; runtime traces; guardrails matrix (§2) + content policy; rubrics + self-critique loop; audit; approval queue (block → edit → approve; approve never sends: mock transport or "ready to send manually"); never-invite list; settings API 403 on BOX_ONLY; scripts/tenant_admin.py |
