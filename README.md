# Operator AI

White-label, approval-gated agentic operating system for a high-ticket coaching / growth-operator programme.
Trained on the owner's material, speaks under the owner's brand, and puts every outgoing item in front of a
human for approval. Works with AI off (rules, templates, retrieval, labelled replays).

## Quick start

```bash
python3.14 -m venv .venv && .venv/bin/pip install -r requirements.lock
(cd web && npm install && npm run build)
scripts/run.sh            # http://127.0.0.1:8850
```

The owner password for the real workspace is written to `data/tenants/owner/owner_password.txt` (mode 600).
The sign-in page also has a **Pitch demo** button that opens the separate, fictional demo workspace.

## Tests

```bash
.venv/bin/pytest
```

Tests run offline: AI disabled, temp data dir, fakes for every outside service; any socket to a non-loopback
host fails the test.
