"""Course importer: a folder of the owner's own exported course files -> the corpus layout `corpus.load` reads.

    source/                          inputs/
      Core Programme/                  timestamped/<id>.txt          "[mm:ss] text" per caption cue or speech segment
        01 - Offer basics.mp4   ->     course/transcripts/<id>.txt   the same lines without timestamps
        01 - Offer basics.vtt          course/media_ids.json         id -> lesson title and product
        Worksheet.pdf           ->     course/<slug>.txt             PDF / Word / notes text
      SOPs/Thunder Dome.docx    ->     sops/<slug>.txt               anything under a folder named like "SOPs"
                                       course/course_digest.md       garbled / partial transcripts flagged
                                       import_log.json               what was imported, from where, under which rights

Captions beside a video win over transcription. Videos and audio without captions are transcribed locally with
faster-whisper when it is installed (nothing leaves the machine); otherwise they are listed as waiting. The importer
only adds and updates files it wrote itself (tracked in import_log.json), never deletes, and skips unchanged sources.
The person running it must state that they own or license the material; that statement is recorded in the log.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable

from . import corpus

MEDIA_EXT = {".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi", ".mp3", ".m4a", ".wav", ".aac", ".flac", ".ogg"}
CAPTION_EXT = {".vtt", ".srt"}
DOC_EXT = {".pdf", ".docx", ".txt", ".md", ".html", ".htm"}
RIGHTS = {"owner", "licensed"}
LOG_NAME = "import_log.json"
SOP_FOLDER = re.compile(r"\bsops?\b|standard operating", re.I)
MIN_DOC_WORDS = 25  # corpus.load skips shorter course/sop files

# A transcriber turns a media file into (start_seconds, text) segments.
Segment = tuple[float, str]
Transcriber = Callable[[Path], list[Segment]]


class ImportRefused(Exception):
    pass


@dataclass
class Outcome:
    source: str            # path relative to the source folder
    status: str            # imported | updated | unchanged | waiting | skipped | error
    kind: str = ""         # transcript | sop | course_doc
    lesson_id: str = ""
    outputs: list[str] = field(default_factory=list)
    note: str = ""


@dataclass
class Report:
    outcomes: list[Outcome]
    dry_run: bool

    def count(self, status: str) -> int:
        return sum(1 for o in self.outcomes if o.status == status)

    def summary(self) -> str:
        parts = [f"{self.count(s)} {s}" for s in ("imported", "updated", "unchanged", "waiting", "skipped", "error")
                 if self.count(s)]
        return ("Dry run: " if self.dry_run else "") + (", ".join(parts) or "nothing to import")


# --------------------------------------------------------------------------- names
def lesson_id(rel_stem: str) -> str:
    """Stable id from the source path without its extension, so re-imports update the same lesson."""
    return "les_" + hashlib.sha1(rel_stem.lower().encode()).hexdigest()[:10]


def title_from_name(stem: str) -> str:
    """"01 - Offer_basics" -> "Offer basics"; keeps the words, drops leading numbering and separators."""
    s = re.sub(r"[_]+", " ", stem)
    s = re.sub(r"^\s*(?:(?:lesson|module|part|video|ep(?:isode)?)\s*)?\d{1,3}(?:\.\d+)*(?:\s*[-.:)_]+\s*|\s+)", "", s, flags=re.I)
    s = re.sub(r"\s+", " ", s).strip(" -.")
    return s or stem


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")[:60] or "doc"


def _fmt_ts(seconds: float) -> str:
    s = max(0, int(seconds))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m:02d}:{sec:02d}"


def _fingerprint(p: Path) -> str:
    """Content hash for text-sized files; size and mtime for media, which can be gigabytes."""
    if p.suffix.lower() in MEDIA_EXT:
        st = p.stat()
        return f"{st.st_size}:{st.st_mtime_ns}"
    return _sha(p)


def _sha(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


# --------------------------------------------------------------------------- captions
_CUE_TIME = re.compile(r"^\s*(?:(\d+):)?(\d{1,2}):(\d{2})[.,]\d{1,3}\s*-->")
_VOICE = re.compile(r"^\s*<v(?:\.[\w.\-]+)?\s+([^>]{1,60})>(.*)$")


def parse_captions(raw: str) -> list[Segment]:
    """WebVTT or SRT -> (start_seconds, text) per cue. A <v Name> voice tag becomes a "Name: " prefix so the corpus
    keeps the speaker; other tags are dropped; repeated rolling-caption lines are collapsed."""
    segs: list[Segment] = []
    start: float | None = None
    buf: list[str] = []

    def flush():
        nonlocal buf
        text = " ".join(t for t in buf if t).strip()
        if start is not None and text and (not segs or segs[-1][1] != text):
            segs.append((start, text))
        buf = []

    for line in raw.replace("﻿", "").splitlines():
        m = _CUE_TIME.match(line)
        if m:
            flush()
            start = int(m.group(1) or 0) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
            continue
        if not line.strip():
            flush()
            continue
        if start is None or line.strip().isdigit() and not buf:
            continue  # header, NOTE blocks before the first cue, SRT cue numbers
        v = _VOICE.match(line)
        if v:
            text = re.sub(r"<[^>]+>", "", v.group(2)).strip()
            buf.append(f"{v.group(1).strip()}: {text}" if text else "")
        else:
            buf.append(re.sub(r"<[^>]+>", "", line).strip())
    flush()
    return segs


# --------------------------------------------------------------------------- documents
def doc_text(p: Path) -> str:
    ext = p.suffix.lower()
    if ext == ".pdf":
        from pypdf import PdfReader

        return "\n".join((pg.extract_text() or "") for pg in PdfReader(str(p)).pages)
    if ext == ".docx":
        import docx

        return "\n".join(par.text for par in docx.Document(str(p)).paragraphs)
    raw = p.read_text(encoding="utf-8", errors="replace")
    if ext in (".html", ".htm"):
        raw = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", raw)
        raw = re.sub(r"(?i)<br\s*/?>|</p>|</h\d>|</li>|</div>", "\n", raw)
        raw = re.sub(r"<[^>]+>", " ", raw)
        import html

        raw = html.unescape(raw)
    return "\n".join(re.sub(r"[ \t]+", " ", ln).rstrip() for ln in raw.splitlines())


# --------------------------------------------------------------------------- transcription
def whisper_transcriber(model: str = "small", language: str | None = None) -> Transcriber | None:
    """faster-whisper on the CPU, int8, with voice-activity filtering. None when it is not installed."""
    try:
        from faster_whisper import WhisperModel  # type: ignore
    except ImportError:
        return None
    engine = WhisperModel(model, device="cpu", compute_type="int8")

    def run(path: Path) -> list[Segment]:
        segments, _info = engine.transcribe(str(path), language=language, vad_filter=True)
        return [(s.start, s.text.strip()) for s in segments if s.text.strip()]

    return run


# --------------------------------------------------------------------------- plan
@dataclass
class _Item:
    rel: str
    path: Path
    kind: str                      # lesson | doc
    caption: Path | None = None    # for lessons: the caption file to read instead of transcribing
    media: Path | None = None


def _product_for(rel_parts: tuple[str, ...], default: str) -> str:
    return rel_parts[0] if len(rel_parts) > 1 else default


def plan(source: Path) -> list[_Item]:
    files = sorted(p for p in source.rglob("*") if p.is_file() and not any(part.startswith(".") for part in p.relative_to(source).parts))
    by_stem: dict[tuple[Path, str], dict[str, Path]] = {}
    items: list[_Item] = []
    for p in files:
        ext = p.suffix.lower()
        if ext in MEDIA_EXT or ext in CAPTION_EXT:
            by_stem.setdefault((p.parent, p.stem.lower()), {})["caption" if ext in CAPTION_EXT else "media"] = p
        elif ext in DOC_EXT:
            items.append(_Item(rel=str(p.relative_to(source)), path=p, kind="doc"))
        else:
            items.append(_Item(rel=str(p.relative_to(source)), path=p, kind="other"))
    for (_parent, _stem), pair in sorted(by_stem.items(), key=lambda kv: str(next(iter(kv[1].values())))):
        anchor = pair.get("media") or pair["caption"]
        items.append(_Item(rel=str(anchor.relative_to(source)), path=anchor, kind="lesson",
                           caption=pair.get("caption"), media=pair.get("media")))
    return sorted(items, key=lambda i: i.rel.lower())


# --------------------------------------------------------------------------- run
def _load_log(dest: Path) -> dict:
    p = dest / LOG_NAME
    if p.exists():
        try:
            return json.loads(p.read_text())
        except ValueError:
            pass
    return {"version": 1, "sources": {}, "outputs": []}


def _write(dest: Path, rel: str, text: str, owned: set[str], dry_run: bool) -> bool:
    """Write a file the importer owns. Refuses to replace a file it did not create."""
    target = dest / rel
    if target.exists() and rel not in owned:
        return False
    if not dry_run:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        owned.add(rel)
    return True


def run(source: Path, dest: Path, *, rights: str, product: str | None = None, transcriber: Transcriber | None = None,
        force: bool = False, dry_run: bool = False, progress: Callable[[str], None] | None = None) -> Report:
    source, dest = Path(source).resolve(), Path(dest).resolve()
    if rights not in RIGHTS:
        raise ImportRefused("State the rights you hold: --rights owner (you made it) or --rights licensed (the owner "
                            "gave you permission). Course material you only bought access to cannot be imported.")
    if not source.is_dir():
        raise ImportRefused(f"Source folder not found: {source}")
    if dest == source or source in dest.parents:
        raise ImportRefused("The destination must be outside the source folder.")
    say = progress or (lambda _m: None)
    default_product = product or source.name
    log = _load_log(dest)
    owned: set[str] = set(log.get("outputs", []))
    media_path = dest / "course" / "media_ids.json"
    media_ids: list[dict] = json.loads(media_path.read_text()) if media_path.exists() else []
    media_by_id = {str(m.get("wistia_id")): m for m in media_ids}
    digest: dict[str, str] = {}
    outcomes: list[Outcome] = []

    for it in plan(source):
        parts = Path(it.rel).parts
        if it.kind == "other":
            outcomes.append(Outcome(it.rel, "skipped", note=f"unsupported file type {it.path.suffix or '(none)'}"))
            continue
        src_files = [p for p in (it.caption, it.media, it.path) if p]
        fp = hashlib.sha256("|".join(_fingerprint(p) for p in dict.fromkeys(src_files)).encode()).hexdigest()
        prev = log["sources"].get(it.rel)
        if prev and prev.get("fingerprint") == fp and not force and all((dest / o).exists() for o in prev.get("outputs", [])):
            outcomes.append(Outcome(it.rel, "unchanged", prev.get("kind", ""), prev.get("lesson_id", ""), prev.get("outputs", [])))
            if prev.get("lesson_id") and prev.get("quality"):
                digest[prev["lesson_id"]] = f"{prev.get('title', '')}. {prev['quality'].upper()}"
            continue
        status = "updated" if prev else "imported"
        try:
            if it.kind == "lesson":
                lid = lesson_id(str(Path(it.rel).with_suffix("")))
                title = title_from_name(it.path.stem)
                if it.caption:
                    segs, how = parse_captions(it.caption.read_text(encoding="utf-8", errors="replace")), "captions"
                elif transcriber is None:
                    outcomes.append(Outcome(it.rel, "waiting", "transcript", lid,
                                            note="no captions beside it and no transcriber installed (pip install faster-whisper)"))
                    continue
                else:
                    say(f"transcribing {it.rel}")
                    segs, how = ([] if dry_run else transcriber(it.media)), "local transcription"
                if not segs and not dry_run:
                    outcomes.append(Outcome(it.rel, "error", "transcript", lid, note=f"no speech found ({how})"))
                    continue
                lines = [t for _s, t in segs]
                quality = corpus.heuristic_quality(lines) if lines else "ok"
                outs = [f"timestamped/{lid}.txt", f"course/transcripts/{lid}.txt"]
                blocked = [o for o, text in zip(outs, ("\n".join(f"[{_fmt_ts(s)}] {t}" for s, t in segs) + "\n",
                                                      "\n".join(lines) + "\n"))
                           if not _write(dest, o, text, owned, dry_run)]
                if blocked:
                    outcomes.append(Outcome(it.rel, "skipped", "transcript", lid, note=f"would replace a file the importer did not create: {blocked[0]}"))
                    continue
                media_by_id[lid] = {"wistia_id": lid, "title": title, "product": _product_for(parts, default_product),
                                    "url": "", "source": it.rel}
                if quality != "ok":
                    digest[lid] = f"{title}. {quality.upper()}"
                record = {"kind": "transcript", "lesson_id": lid, "title": title, "quality": quality, "via": how}
                outcomes.append(Outcome(it.rel, status, "transcript", lid, outs, note=f"{len(segs)} segments from {how}"
                                        + ("" if quality == "ok" else f", flagged {quality}")))
            else:
                text = doc_text(it.path)
                words = len(text.split())
                if words < MIN_DOC_WORDS:
                    outcomes.append(Outcome(it.rel, "skipped", note=f"only {words} words of text (scanned PDF or a link stub?)"))
                    continue
                is_sop = any(SOP_FOLDER.search(part) for part in parts[:-1])
                folder = "sops" if is_sop else "course"
                name = f"{slug(it.path.stem)}_{hashlib.sha1(it.rel.lower().encode()).hexdigest()[:6]}.txt"
                out = f"{folder}/{name}"
                title = title_from_name(it.path.stem)
                body = text if text.lstrip().lower().startswith(title.lower()) else f"{title}\n\n{text}"
                if not _write(dest, out, body.rstrip() + "\n", owned, dry_run):
                    outcomes.append(Outcome(it.rel, "skipped", note=f"would replace a file the importer did not create: {out}"))
                    continue
                kind = "sop" if is_sop else "course_doc"
                record = {"kind": kind, "title": title}
                outs = [out]
                outcomes.append(Outcome(it.rel, status, kind, "", outs, note=f"{words} words"))
        except Exception as e:  # noqa: BLE001 - one bad file must not stop the rest
            outcomes.append(Outcome(it.rel, "error", note=f"{type(e).__name__}: {e}"))
            continue
        log["sources"][it.rel] = {**record, "fingerprint": fp, "outputs": outs}

    changed = any(o.status in ("imported", "updated") for o in outcomes)
    if not dry_run and (changed or not (dest / LOG_NAME).exists()):
        # only when something changed: rewriting these would change the corpus fingerprint and force a reindex
        _write_index_files(dest, media_by_id, digest, owned)
        log["outputs"] = sorted(owned)
        log["rights"] = rights
        log["last_run"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        log["source_folder_name"] = source.name
        (dest / LOG_NAME).write_text(json.dumps(log, indent=1, ensure_ascii=False) + "\n")
    return Report(outcomes, dry_run)


def _write_index_files(dest: Path, media_by_id: dict, digest: dict[str, str], owned: set[str]) -> None:
    course = dest / "course"
    course.mkdir(parents=True, exist_ok=True)
    (course / "media_ids.json").write_text(json.dumps(sorted(media_by_id.values(), key=lambda m: (m.get("product", ""), m.get("title", ""))),
                                                      indent=1, ensure_ascii=False) + "\n")
    owned.add("course/media_ids.json")
    # keep lines someone else wrote in the digest; replace the importer's own lines by id
    path = course / "course_digest.md"
    kept = [ln for ln in path.read_text().splitlines() if not re.match(r"^- les_[0-9a-f]{10}\b", ln)] if path.exists() else ["# Course digest", ""]
    lines = kept + [f"- {lid} — {note}" for lid, note in sorted(digest.items())]
    path.write_text("\n".join(lines).rstrip() + "\n")
    owned.add("course/course_digest.md")


def format_report(report: Report, limit: int | None = None) -> Iterable[str]:
    rows = [o for o in report.outcomes if o.status != "unchanged"]
    for o in rows[:limit] if limit else rows:
        yield f"{o.status:<9} {o.source}" + (f"  ({o.note})" if o.note else "")
    yield report.summary()
