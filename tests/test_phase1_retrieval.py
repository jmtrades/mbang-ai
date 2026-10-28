import json
import shutil

import pytest

from operator_ai import config, corpus, index, textnorm


def test_fixture_fallback_when_inputs_empty(env):
    assert config.get().using_fixture_corpus


def test_quality_flags_from_digest(env):
    idx = index.course_index()
    q = {d.file: d.quality for d in idx.docs.values()}
    assert q["course/transcripts/vid_j1k2l3.txt"] == "garbled"
    assert q["course/transcripts/vid_p7q8r9.txt"] == "partial"
    assert q["course/transcripts/vid_a1b2c3.txt"] == "ok"


def test_heuristic_quality():
    assert corpus.heuristic_quality(["uh uh um the the [inaudible] uh so so um"] * 10) == "garbled"
    assert corpus.heuristic_quality(["This is a clear sentence about booking calls and reminders for leads."] * 10) == "ok"


def test_weights():
    d = corpus.Doc(file="x", kind="transcript", title="t", lines=[], quality="garbled")
    assert d.weight == 0.5
    assert corpus.Doc(file="x", kind="transcript", title="t", lines=[], quality="partial").weight == 0.85
    assert corpus.Doc(file="x", kind="sop", title="t", lines=[]).weight == pytest.approx(1.1)


def test_private_notes_and_ids_not_indexed(env):
    idx = index.course_index()
    files = set(idx.docs)
    assert not any(f.endswith("INDEX.md") or f.endswith("ids.txt") for f in files)
    assert not any("zebra-lantern" in c.text for c in idx.chunks)


def test_link_stub_skipped(env):
    assert "course/link_stub.txt" not in index.course_index().docs


def test_byline_stripped(env):
    idx = index.course_index()
    assert not any("Presented By" in c.text for c in idx.chunks)
    assert idx.docs["sops/thunderdome.txt"].lines[1] == ""


def test_chunks_keep_line_ranges(env):
    idx = index.course_index()
    for c in idx.chunks:
        doc = idx.docs[c.file]
        first = next(ln for ln in doc.lines[c.start - 1:c.end] if ln.strip())
        assert first in c.text
        assert c.provenance == "source_unconfirmed" and c.licence_status == "course"


def test_content_policy_excludes_tactics(env):
    idx = index.course_index()
    labels = [e.label for e in idx.excluded]
    assert "Not used: this passage describes planting audience questions or fake chat accounts" in labels
    assert "Not used: this passage describes using drugs or stimulants" in labels
    assert not any("plant questions in the chat from a second account" in c.text for c in idx.chunks)
    # negated wording stays: "Never plant questions or invent attendees."
    assert any("Never plant questions" in c.text for c in idx.chunks)


def test_compounds_and_synonyms():
    assert "thunderdom" in textnorm.tokens("Thunder Dome rules")
    assert textnorm.tokens("no-show") == textnorm.tokens("no show") == textnorm.tokens("noshow")
    assert textnorm.tokens("opt-in") == textnorm.tokens("opt in")
    exp = dict(textnorm.expand(textnorm.tokens("price")))
    assert exp[textnorm.stem("price")] == 1.0 and textnorm.stem("expensive") in exp


def test_search_thunderdome(owner):
    r = owner.get("/t/owner/api/search", params={"q": "What are the rules for a Thunder Dome test?"})
    assert r.status_code == 200
    assert r.json()["hits"][0]["file"] == "sops/thunderdome.txt"


def test_search_requires_login(client):
    assert client.get("/t/owner/api/search", params={"q": "x"}).status_code == 401


def test_source_serves_indexed_lines(owner):
    r = owner.get("/t/owner/api/source", params={"file": "sops/thunderdome.txt", "lines": "9-10"})
    assert r.status_code == 200
    d = r.json()
    hits = [l for l in d["lines"] if l["hit"]]
    assert [l["n"] for l in hits] == [9, 10]
    assert any("Rule 1" in l["text"] for l in hits)


@pytest.mark.parametrize("bad", ["../../etc/passwd", "/etc/passwd", "sops/../sops/INDEX.md", "sops/INDEX.md",
                                 "sops/ids.txt", "..%2F..%2Fpyproject.toml", "operator_ai/config.py",
                                 "sops/thunderdome.txt/../../x"])
def test_source_traversal_refused(owner, bad):
    r = owner.get("/t/owner/api/source", params={"file": bad, "lines": "1-2"})
    assert r.status_code == 404


def test_source_hides_excluded_lines(owner):
    d = owner.get("/t/owner/api/source", params={"file": "course/transcripts/vid_m4n5o6.txt", "lines": "9-10"}).json()
    assert d["lines"][8]["text"].startswith("[Not used")


def test_timestamped_preferred(env, tmp_path, monkeypatch):
    root = tmp_path / "inputs2"
    shutil.copytree(config.FIXTURE_CORPUS, root)
    (root / "timestamped").mkdir()
    (root / "timestamped" / "vid_a1b2c3.txt").write_text(
        "[0:00:01] Welcome to module one about the USP formula.\n[0:00:09] Pick a niche that can afford you.\n" * 30)
    monkeypatch.setenv("OPERATOR_CORPUS_DIR", str(root))
    index.invalidate()
    idx = index.course_index()
    assert "timestamped/vid_a1b2c3.txt" in idx.docs
    assert "course/transcripts/vid_a1b2c3.txt" not in idx.docs
    c = next(c for c in idx.chunks if c.file == "timestamped/vid_a1b2c3.txt")
    assert c.ts_start == "0:00:01"


def test_owner_exclusion_by_line_range(tenant):
    # exclusions belong to one tenant (tests/test_hardening.py covers the isolation between tenants)
    index.save_exclusions(tenant, [{"file": "sops/thunderdome.txt", "lines": "9-10"}])
    idx = index.for_tenant(tenant)
    assert not any("Rule 1: Test between three and five" in c.text for c in idx.chunks)
    assert any(e.file == "sops/thunderdome.txt" and e.tactic == "an owner exclusion" for e in idx.excluded)
    assert any("Rule 1: Test between three and five" in c.text for c in index.course_index().chunks)
    index.save_exclusions(tenant, [])


def test_retrieval_eval_meets_targets(tenant):
    from operator_ai import evals

    m = evals.retrieval_eval(tenant)
    assert m["n"] >= 50
    assert m["hit@10"] >= 0.95 and m["hit@5"] >= 0.90


def test_gold_set_shape():
    gold = json.loads((config.ROOT / "tests" / "gold_questions.json").read_text())
    nc = json.loads((config.ROOT / "tests" / "not_covered_questions.json").read_text())
    audit = json.loads((config.ROOT / "tests" / "audit_tasks.json").read_text())
    assert len(gold) >= 50 and len(nc) >= 10 and len(audit) >= 6
    assert any(g["multi_part"] for g in gold)
