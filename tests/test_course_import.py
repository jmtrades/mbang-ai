"""Course importer: exported files -> the corpus layout corpus.load reads, with timestamps, speakers, titles,
quality flags, idempotent re-runs and a recorded rights statement."""
from __future__ import annotations

import json
from pathlib import Path

import docx
import pytest

from operator_ai import corpus, course_import
from scripts import import_course

VTT = """WEBVTT

NOTE exported from the course platform

1
00:00:01.000 --> 00:00:04.000
<v Coach>Welcome to the offer lesson. Today we build the unique mechanism.

2
00:00:04.500 --> 00:00:09.000
<v Coach>A mechanism is the named method that makes your offer different from every other agency.

3
00:00:09.000 --> 00:00:12.000
<v Student>How specific does the who need to be?

4
00:00:12.500 --> 00:01:05.000
<v Coach>Dental clinics with two to five chairs is a who. Business owners is not a who.
"""

SRT = """1
00:00:00,500 --> 00:00:03,000
Openers get one question, never a pitch.

2
00:00:03,000 --> 00:00:03,900
Openers get one question, never a pitch.

3
00:01:02,000 --> 00:01:06,000
Follow up on day two and day five with something useful, then stop.

4
01:00:01,000 --> 01:00:04,000
Five thoughtful conversations a day per sender beats fifty copied ones.
"""

SOP_TEXT = ("Show rate checklist. Send the confirmation within five minutes of the booking. Send a reminder the day "
            "before and two hours before. Ask one question in the reminder so the prospect replies. Call anyone who "
            "has not replied an hour before the call. Track the show rate weekly per closer and per source.")


def _fake_transcriber(calls: list):
    def run(path: Path):
        calls.append(path.name)
        return [(0.0, "This lesson is about booking calls and keeping show rates high."),
                (75.4, "Confirm every booking within five minutes and remind them the day before."),
                (3725.0, "Track it every week by closer and by lead source.")]
    return run


@pytest.fixture
def source(tmp_path: Path) -> Path:
    src = tmp_path / "export"
    core = src / "Core Programme"
    core.mkdir(parents=True)
    (core / "01 - Offer_basics.mp4").write_bytes(b"\x00fake video")
    (core / "01 - Offer_basics.vtt").write_text(VTT)            # captions beside the video win
    (core / "02 DM setting.srt").write_text(SRT)                # captions on their own
    (core / "03 - Booking calls.mov").write_bytes(b"\x00fake")  # needs transcription
    sops = src / "SOPs"
    sops.mkdir()
    d = docx.Document()
    for para in SOP_TEXT.split(". "):
        d.add_paragraph(para)
    d.save(sops / "Show rate SOP.docx")
    (src / "Worksheet notes.md").write_text("Worksheet notes\n\n" + SOP_TEXT)
    (src / "link.txt").write_text("https://example.invalid/x")  # a link stub: too short to import
    (src / "cover.png").write_bytes(b"\x89PNG")
    (src / ".DS_Store").write_bytes(b"")
    return src


def _run(source: Path, dest: Path, **kw):
    kw.setdefault("rights", "owner")
    return course_import.run(source, dest, **kw)


def test_import_produces_the_layout_corpus_load_reads(source: Path, tmp_path: Path):
    dest = tmp_path / "inputs"
    calls: list = []
    report = _run(source, dest, transcriber=_fake_transcriber(calls))
    by = {o.source: o for o in report.outcomes}

    assert by["Core Programme/01 - Offer_basics.mp4"].status == "imported"
    assert "captions" in by["Core Programme/01 - Offer_basics.mp4"].note
    assert calls == ["03 - Booking calls.mov"], "a video with captions beside it must not be transcribed"
    assert by["SOPs/Show rate SOP.docx"].kind == "sop"
    assert by["Worksheet notes.md"].kind == "course_doc"
    assert by["link.txt"].status == "skipped" and by["cover.png"].status == "skipped"
    assert not any(".DS_Store" in o.source for o in report.outcomes)

    docs = corpus.load(dest)
    kinds = sorted(d.kind for d in docs)
    assert kinds == ["pdf_sop", "sop", "transcript", "transcript", "transcript"]
    transcripts = {d.title: d for d in docs if d.kind == "transcript"}
    assert set(transcripts) == {"Offer basics", "DM setting", "Booking calls"}
    assert all(d.product == "Core Programme" for d in transcripts.values())

    offer = transcripts["Offer basics"]
    assert offer.file.startswith("timestamped/"), "the timestamped copy is preferred"
    assert offer.ts[:4] == ["00:01", "00:04", "00:09", "00:12"]
    assert offer.speakers[0] == "Coach" and offer.speakers[2] == "Student"

    dm = transcripts["DM setting"]
    assert len(dm.lines) == 3, "a repeated rolling caption is collapsed"
    assert dm.ts == ["00:00", "01:02", "1:00:01"]
    assert transcripts["Booking calls"].ts[-1] == "1:02:05"

    chunks, _ = corpus.chunk_docs(docs)
    assert any(c.ts_start == "00:01" and c.title == "Offer basics" for c in chunks)

    log = json.loads((dest / course_import.LOG_NAME).read_text())
    assert log["rights"] == "owner"
    assert "course/media_ids.json" in log["outputs"]


def test_rerun_is_idempotent_and_does_not_touch_the_corpus(source: Path, tmp_path: Path):
    dest = tmp_path / "inputs"
    _run(source, dest, transcriber=_fake_transcriber([]))
    before = corpus.fingerprint(dest)
    calls: list = []
    again = _run(source, dest, transcriber=_fake_transcriber(calls))
    assert calls == []
    assert again.count("imported") == again.count("updated") == 0
    assert again.count("unchanged") == 5
    assert corpus.fingerprint(dest) == before, "a no-op run must not force a reindex"


def test_changed_source_is_updated_in_place(source: Path, tmp_path: Path):
    dest = tmp_path / "inputs"
    _run(source, dest, transcriber=_fake_transcriber([]))
    srt = source / "Core Programme" / "02 DM setting.srt"
    srt.write_text(SRT.replace("never a pitch", "never a price"))
    report = _run(source, dest, transcriber=_fake_transcriber([]))
    assert [o.source for o in report.outcomes if o.status == "updated"] == ["Core Programme/02 DM setting.srt"]
    dm = next(d for d in corpus.load(dest) if d.title == "DM setting")
    assert "never a price" in dm.lines[0]
    assert len([d for d in corpus.load(dest) if d.kind == "transcript"]) == 3, "same lesson id, no duplicate"


def test_rights_statement_is_required(source: Path, tmp_path: Path):
    with pytest.raises(course_import.ImportRefused):
        course_import.run(source, tmp_path / "inputs", rights="")
    with pytest.raises(course_import.ImportRefused):
        course_import.run(source, tmp_path / "inputs", rights="purchased")
    assert not (tmp_path / "inputs").exists()


def test_video_without_captions_waits_when_no_transcriber(source: Path, tmp_path: Path):
    report = _run(source, tmp_path / "inputs", transcriber=None)
    waiting = [o for o in report.outcomes if o.status == "waiting"]
    assert [o.source for o in waiting] == ["Core Programme/03 - Booking calls.mov"]
    assert "faster-whisper" in waiting[0].note


def test_never_replaces_a_file_it_did_not_write(source: Path, tmp_path: Path):
    dest = tmp_path / "inputs"
    lid = course_import.lesson_id("Core Programme/02 DM setting")
    mine = dest / "timestamped" / f"{lid}.txt"
    mine.parent.mkdir(parents=True)
    mine.write_text("[00:00] hand-corrected transcript\n")
    report = _run(source, dest, transcriber=_fake_transcriber([]))
    o = next(o for o in report.outcomes if o.source.endswith("02 DM setting.srt"))
    assert o.status == "skipped" and "did not create" in o.note
    assert mine.read_text() == "[00:00] hand-corrected transcript\n"


def test_dry_run_writes_nothing(source: Path, tmp_path: Path):
    calls: list = []
    report = _run(source, tmp_path / "inputs", transcriber=_fake_transcriber(calls), dry_run=True)
    assert report.count("imported") == 5 and calls == []
    assert not (tmp_path / "inputs").exists()


def test_garbled_transcript_is_flagged_in_the_digest(tmp_path: Path):
    src = tmp_path / "export"
    src.mkdir()
    noisy = "um uh like um so uh um like the the the uh um offer um uh like uh"
    (src / "Group call.srt").write_text("".join(f"{i}\n00:00:{i:02d},000 --> 00:00:{i:02d},900\n{noisy} {i}\n\n" for i in range(1, 6)))
    dest = tmp_path / "inputs"
    _run(src, dest)
    doc = next(d for d in corpus.load(dest) if d.kind == "transcript")
    assert doc.quality == "garbled"
    assert "GARBLED" in (dest / "course" / "course_digest.md").read_text()


def test_titles_drop_numbering_but_keep_words():
    assert course_import.title_from_name("01 - Offer_basics") == "Offer basics"
    assert course_import.title_from_name("Module 3: Booking calls") == "Booking calls"
    assert course_import.title_from_name("2024 Strategy") == "2024 Strategy"
    assert course_import.title_from_name("Wealth Math") == "Wealth Math"


def test_cli_refuses_without_rights_and_runs_with_them(source: Path, tmp_path: Path, capsys):
    with pytest.raises(SystemExit):
        import_course.main([str(source), "--dest", str(tmp_path / "inputs")])
    code = import_course.main([str(source), "--dest", str(tmp_path / "inputs"), "--rights", "licensed", "--no-transcribe"])
    out = capsys.readouterr().out
    assert code == 0
    assert "4 imported" in out and "1 waiting" in out
    assert json.loads((tmp_path / "inputs" / course_import.LOG_NAME).read_text())["rights"] == "licensed"
