"""Corpus loader: transcripts, SOPs, PDF-text SOPs and summaries -> documents -> chunks.

Each chunk keeps its line range (and timestamps when the source has them) and carries
corpus_id, licence_status, provenance (owner_confirmed / source_unconfirmed) and weight.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import content_policy

CHUNK_WORDS = 180
SKIP_FILES = {"INDEX.md", "ids.txt", "course_digest.md", "media_ids.json"}
BYLINE = re.compile(r"^\s*(presented|written|created|prepared|delivered|recorded)\s+by\b.*$", re.I)
TS_LINE = re.compile(r"^\s*\[(\d{1,2}:\d{2}(?::\d{2})?)\]\s*(.*)$")
FILLER = {"uh", "um", "er", "erm", "uhh", "umm", "[inaudible]", "inaudible", "like"}

QUALITY_WEIGHT = {"ok": 1.0, "partial": 0.85, "garbled": 0.5}
KIND_WEIGHT = {"sop": 1.1, "pdf_sop": 1.1, "transcript": 1.0, "summary": 0.9, "upload": 1.0, "note": 1.0}


@dataclass
class Doc:
    file: str            # path relative to the corpus root, used in citations and /api/source
    kind: str            # transcript | sop | pdf_sop | summary | upload
    title: str
    lines: list[str]
    ts: list[str | None] = field(default_factory=list)
    quality: str = "ok"
    licence_status: str = "course"
    provenance: str = "source_unconfirmed"
    corpus_id: str = "course"
    product: str = ""
    # who says each line (None = unattributed), parallel to `lines`; parsed from speaker labels at ingest
    speakers: list[str | None] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.speakers and self.lines:
            self.speakers = parse_speakers(self.lines, self.kind)

    @property
    def weight(self) -> float:
        return QUALITY_WEIGHT.get(self.quality, 1.0) * KIND_WEIGHT.get(self.kind, 1.0)


@dataclass
class Chunk:
    id: int
    file: str
    title: str
    kind: str
    start: int           # 1-based, inclusive
    end: int
    text: str
    ts_start: str | None
    ts_end: str | None
    quality: str
    weight: float
    licence_status: str
    provenance: str
    corpus_id: str
    speaker_words: dict = field(default_factory=dict)  # speaker -> words in this chunk (attributed lines only)

    @property
    def lines_label(self) -> str:
        return f"{self.start}-{self.end}"

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Excluded:
    file: str
    lines: str
    tactic: str
    text: str

    @property
    def label(self) -> str:
        return content_policy.not_used_label(self.tactic)

    def to_dict(self) -> dict:
        return {"file": self.file, "lines": self.lines, "tactic": self.tactic, "label": self.label,
                "text": self.text[:200]}


# --------------------------------------------------------------------------- quality
def _digest_flags(root: Path) -> dict[str, str]:
    out = {}
    for p in [root / "course" / "course_digest.md", root / "course_digest.md"]:
        if p.exists():
            for line in p.read_text(errors="replace").splitlines():
                m = re.search(r"([A-Za-z0-9_\-]{4,})", line)
                if not m:
                    continue
                low = line.lower()
                if "garbled" in low:
                    out[m.group(1)] = "garbled"
                elif "partial" in low:
                    out[m.group(1)] = "partial"
    return out


def heuristic_quality(lines: list[str]) -> str:
    words = " ".join(lines).lower().split()
    if len(words) < 30:
        return "partial"
    filler = sum(1 for w in words if w.strip(",.") in FILLER)
    repeats = sum(1 for a, b in zip(words, words[1:]) if a == b)
    ratio = (filler + repeats) / len(words)
    if ratio > 0.12:
        return "garbled"
    return "ok"


# --------------------------------------------------------------------------- speaker attribution
# Speaker labels are kept at ingest so nothing is ever guessed later: WebVTT <v Name> tags, "Name:" prefixes
# (SRT/plain transcripts, ">> NAME:" and "- Name:" caption styles), "[Name] text", Zoom "[Name] 00:01:02" header
# lines (the speaker holds until the next header) and Zoom "00:01:02 Name: text" lines. The line text itself is
# never changed, so citations and line ranges stay exact.
_NAME = r"[A-Z][A-Za-z'.\-]*(?:\s+[A-Z][A-Za-z'.\-]*){0,3}(?:\s+\([A-Z]{2,8}\))?"
VTT_VOICE = re.compile(r"^\s*<v(?:\.[\w.\-]+)?\s+(?P<name>[^>]{1,60})>(?P<text>.*?)(?:</v>)?\s*$")
ZOOM_HEADER = re.compile(r"^\s*\[(?P<name>[A-Za-z][A-Za-z'.\-() ]{0,60}?)\]\s+\d{1,2}:\d{2}(?::\d{2})?\s*$")
ZOOM_INLINE = re.compile(r"^\s*\d{1,2}:\d{2}(?::\d{2})?\s+(?P<name>" + _NAME + r")\s*:\s+(?P<text>\S.*)$")
LABEL_COLON = re.compile(r"^\s*(?:>>\s*|-\s*)?(?P<name>" + _NAME + r")\s*:\s+(?P<text>\S.*)$")
LABEL_BRACKET = re.compile(r"^\s*\[(?P<name>[A-Za-z][A-Za-z'.\-() ]{0,60}?)\]\s*(?P<text>[^\d\s\[].*)$")
# headings that look like "Name:" labels in written material
NOT_SPEAKERS = {
    "note", "notes", "step", "goal", "goals", "subject", "tip", "tips", "example", "examples", "homework", "rule", "rules",
    "q", "a", "question", "answer", "summary", "title", "module", "lesson", "part", "day", "week", "action", "warning",
    "important", "source", "date", "time", "re", "fwd", "fw", "hook", "cta", "outcome", "offer", "gap", "discovery",
    "rapport", "objection", "objections", "result", "results", "why", "how", "what", "when", "who", "where", "mechanism",
    "pain", "proof", "promise", "price", "timeframe", "close", "next", "agenda", "intro", "outro", "recap", "context",
    "problem", "solution", "update", "status", "owner", "target", "metric", "metrics", "benchmark", "definition",
    "key", "takeaway", "takeaways", "script", "opener", "follow", "reminder", "ps", "p.s", "bonus", "faq", "overview",
}
SPEAKER_KINDS = {"transcript", "upload", "note"}


def speaker_key(name: str) -> str:
    """Normalised speaker label for matching ("Host Name (DEMO):" -> "host name")."""
    return " ".join((name or "").replace("(DEMO)", "").split()).strip(" .:-").lower()


def parse_speakers(lines: list[str], kind: str = "transcript") -> list[str | None]:
    """Who says each line (None = unattributed). Written material (SOPs, PDFs, summaries) never gets speakers.
    "Name:" / "[Name]" labels count only when the document is clearly a labelled transcript: at least three labelled
    lines, at least a fifth of the non-empty lines, and one label used more than once. Headings such as "Goal:" are
    never speakers."""
    out: list[str | None] = [None] * len(lines)
    if kind not in SPEAKER_KINDS:
        return out
    current = None
    strong = False  # VTT voices and Zoom headers are explicit; label prefixes need document-level evidence
    weak: list[tuple[int, str]] = []
    for i, ln in enumerate(lines):
        m = VTT_VOICE.match(ln)
        if m:
            out[i] = m.group("name").strip()
            strong = True
            continue
        m = ZOOM_HEADER.match(ln)
        if m:
            current = m.group("name").strip()
            out[i] = current
            strong = True
            continue
        m = ZOOM_INLINE.match(ln) or LABEL_COLON.match(ln) or LABEL_BRACKET.match(ln)
        if m and speaker_key(m.group("name")) not in NOT_SPEAKERS:
            weak.append((i, m.group("name").strip()))
            continue
        if current and ln.strip():
            out[i] = current
    if weak:
        nonempty = sum(1 for ln in lines if ln.strip()) or 1
        counts: dict[str, int] = {}
        for _, n in weak:
            counts[speaker_key(n)] = counts.get(speaker_key(n), 0) + 1
        if strong or (len(weak) >= max(3, 0.2 * nonempty) and max(counts.values()) >= 2):
            labelled = {i for i, _ in weak}
            for i, n in weak:
                out[i] = n
                # a labelled turn continues on the following unlabelled lines until the next label or a blank line
                j = i + 1
                while j < len(lines) and j not in labelled and lines[j].strip() and not ZOOM_HEADER.match(lines[j]):
                    out[j] = n
                    j += 1
    return out


def speaker_text(line: str) -> str:
    """The spoken words of a line without its speaker label (for style analysis only; citations keep the line)."""
    if ZOOM_HEADER.match(line):
        return ""
    for pat in (VTT_VOICE, ZOOM_INLINE, LABEL_COLON, LABEL_BRACKET):
        m = pat.match(line)
        if m and (pat is VTT_VOICE or speaker_key(m.group("name")) not in NOT_SPEAKERS):
            return re.sub(r"<[^>]+>", "", m.group("text")).strip()
    return re.sub(r"<[^>]+>", "", line).strip()


# --------------------------------------------------------------------------- loading
def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8", errors="replace")


def _clean_lines(raw: list[str]) -> tuple[list[str], list[str | None]]:
    lines, ts = [], []
    for ln in raw:
        m = TS_LINE.match(ln)
        if m:
            ts.append(m.group(1))
            ln = m.group(2)
        else:
            ts.append(None)
        lines.append("" if BYLINE.match(ln) else ln.rstrip())
    return lines, ts


def load(root: Path) -> list[Doc]:
    root = Path(root)
    docs: list[Doc] = []
    if not root.exists():
        return docs
    titles: dict[str, tuple[str, str]] = {}
    for mp in [root / "course" / "media_ids.json", root / "media_ids.json"]:
        if mp.exists():
            try:
                for item in json.loads(_read(mp)):
                    titles[str(item.get("wistia_id"))] = (item.get("title") or "", item.get("product") or "")
            except (ValueError, TypeError):
                pass
    digest = _digest_flags(root)

    # transcripts: prefer timestamped copies of the same video
    stamped = {p.stem: p for p in (root / "timestamped").glob("*.txt")} if (root / "timestamped").exists() else {}
    plain = {p.stem: p for p in (root / "course" / "transcripts").glob("*.txt")} if (root / "course" / "transcripts").exists() else {}
    for vid in sorted(set(stamped) | set(plain)):
        p = stamped.get(vid) or plain[vid]
        lines, ts = _clean_lines(_read(p).splitlines())
        title, product = titles.get(vid, (vid.replace("_", " "), ""))
        q = digest.get(vid) or heuristic_quality(lines)
        docs.append(Doc(file=str(p.relative_to(root)), kind="transcript", title=title or vid, lines=lines, ts=ts,
                        quality=q, product=product))

    # SOPs
    if (root / "sops").exists():
        for p in sorted((root / "sops").glob("*.txt")):
            if p.name in SKIP_FILES:
                continue
            lines, ts = _clean_lines(_read(p).splitlines())
            if len(" ".join(lines).split()) < 25:
                continue
            title = next((ln.strip() for ln in lines if ln.strip()), p.stem)
            docs.append(Doc(file=str(p.relative_to(root)), kind="sop", title=title, lines=lines, ts=ts))

    # course PDF texts (skip link-only stubs / near-empty files)
    if (root / "course").exists():
        for p in sorted((root / "course").glob("*.txt")):
            if p.name in SKIP_FILES:
                continue
            lines, ts = _clean_lines(_read(p).splitlines())
            if len(" ".join(lines).split()) < 25:
                continue
            title = next((ln.strip() for ln in lines if ln.strip()), p.stem)
            docs.append(Doc(file=str(p.relative_to(root)), kind="pdf_sop", title=title, lines=lines, ts=ts))

    # per-lesson summaries
    if (root / "course" / "summaries").exists():
        for p in sorted((root / "course" / "summaries").glob("*.md")):
            lines, ts = _clean_lines(_read(p).splitlines())
            title = (lines[0].lstrip("# ").strip() if lines else p.stem) + " (summary)"
            docs.append(Doc(file=str(p.relative_to(root)), kind="summary", title=title, lines=lines, ts=ts))
    return docs


def load_uploads(kb_dir: Path, corpus_id: str) -> list[Doc]:
    """A client tenant's own uploads (kb_scope=own)."""
    docs = []
    if not kb_dir.exists():
        return docs
    for p in sorted(kb_dir.glob("*.txt")):
        lines, ts = _clean_lines(_read(p).splitlines())
        if not any(ln.strip() for ln in lines):
            continue
        title = next((ln.strip() for ln in lines if ln.strip()), p.stem)
        docs.append(Doc(file=f"kb/{p.name}", kind="upload", title=title, lines=lines, ts=ts, licence_status="own",
                        provenance="owner_confirmed", corpus_id=corpus_id))
    return docs


# --------------------------------------------------------------------------- chunking
def _in_exclusion(doc: Doc, n: int, exclusions: list[dict]) -> bool:
    for ex in exclusions:
        if ex.get("file") != doc.file:
            continue
        if "lines" in ex:
            a, _, b = str(ex["lines"]).partition("-")
            if int(a) <= n <= int(b or a):
                return True
        if "ts" in ex and doc.ts and doc.ts[n - 1]:
            a, _, b = str(ex["ts"]).partition("-")
            if _ts_sec(a) <= _ts_sec(doc.ts[n - 1]) <= _ts_sec(b or a):
                return True
    return False


def _ts_sec(s: str) -> int:
    parts = [int(x) for x in s.strip().split(":")]
    while len(parts) < 3:
        parts.insert(0, 0)
    return parts[0] * 3600 + parts[1] * 60 + parts[2]


def chunk_docs(docs: list[Doc], exclusions: list[dict] | None = None) -> tuple[list[Chunk], list[Excluded]]:
    exclusions = exclusions or []
    chunks: list[Chunk] = []
    excluded: list[Excluded] = []
    for doc in docs:
        buf: list[tuple[int, str]] = []
        words = 0

        def flush(final: bool = False):
            nonlocal buf, words
            if buf and any(t.strip() for _, t in buf):
                prev = chunks[-1] if chunks else None
                if final and prev is not None and prev.file == doc.file and words < CHUNK_WORDS * 0.35:
                    # fold a short tail into the previous chunk of the same document
                    prev.text += "\n" + "\n".join(t for _, t in buf if t.strip())
                    prev.end = buf[-1][0]
                    prev.ts_end = doc.ts[prev.end - 1] if doc.ts else None
                    for k, v in _speaker_words(doc, buf).items():
                        prev.speaker_words[k] = prev.speaker_words.get(k, 0) + v
                    buf, words = [], 0
                    return
                s, e = buf[0][0], buf[-1][0]
                text = "\n".join(t for _, t in buf if t.strip())
                chunks.append(Chunk(
                    id=len(chunks), file=doc.file, title=doc.title, kind=doc.kind, start=s, end=e, text=text,
                    ts_start=doc.ts[s - 1] if doc.ts else None, ts_end=doc.ts[e - 1] if doc.ts else None,
                    quality=doc.quality, weight=doc.weight, licence_status=doc.licence_status,
                    provenance=doc.provenance, corpus_id=doc.corpus_id, speaker_words=_speaker_words(doc, buf)))
            buf, words = [], 0

        for i, line in enumerate(doc.lines, start=1):
            if not line.strip():
                if doc.kind != "transcript" and words >= CHUNK_WORDS * 0.6:
                    flush()
                continue
            if _in_exclusion(doc, i, exclusions):
                excluded.append(Excluded(doc.file, f"{i}-{i}", "an owner exclusion", line))
                continue
            tactic = content_policy.classify(line)
            if tactic:
                excluded.append(Excluded(doc.file, f"{i}-{i}", tactic, line))
                continue
            buf.append((i, line))
            words += len(line.split())
            if words >= CHUNK_WORDS:
                flush()
        flush(final=True)
    return chunks, excluded


def _speaker_words(doc: Doc, buf: list[tuple[int, str]]) -> dict:
    if not doc.speakers or not any(doc.speakers):
        return {}
    out: dict[str, int] = {}
    for n, t in buf:
        sp = doc.speakers[n - 1] if n - 1 < len(doc.speakers) else None
        if sp:
            out[sp] = out.get(sp, 0) + len(speaker_text(t).split())
    return out


def fingerprint(root: Path, extra: str = "") -> str:
    h = hashlib.sha256(extra.encode())
    root = Path(root)
    if root.exists():
        for p in sorted(root.rglob("*")):
            if p.is_file():
                st = p.stat()
                h.update(f"{p.relative_to(root)}|{st.st_size}|{int(st.st_mtime)}".encode())
    return h.hexdigest()[:16]
