"""Uploads with a "what I read" receipt. TXT, MD, CSV, JSON, PDF, DOCX, SRT/VTT, HTML and images
(local OCR: tesseract or macOS Vision when present; otherwise the receipt says so)."""
from __future__ import annotations

import csv
import html as html_mod
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from . import db

MAX_BYTES = 10 * 1024 * 1024
TEXT_EXT = {".txt", ".md"}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".tiff", ".bmp"}
SUPPORTED = TEXT_EXT | IMAGE_EXT | {".csv", ".json", ".pdf", ".docx", ".srt", ".vtt", ".html", ".htm"}
_TS = re.compile(r"^\s*(\d+|WEBVTT.*|\d{1,2}:\d{2}(:\d{2})?[.,]\d{1,3}\s*-->\s*\d{1,2}:\d{2}(:\d{2})?[.,]\d{1,3}.*|NOTE.*)\s*$")


class UploadError(Exception):
    pass


_VTT_VOICE = re.compile(r"^\s*<v(?:\.[\w.\-]+)?\s+([^>]{1,60})>(.*)$")


def _subtitles(raw: str) -> str:
    """Caption text without cue numbers or timings. A WebVTT voice tag (<v Name>) is kept as a "Name: " prefix so
    the speaker survives into the knowledge base; SRT speaker prefixes ("NAME:", "- Name:") are already text."""
    out = []
    for line in raw.splitlines():
        if not line.strip() or _TS.match(line):
            continue
        m = _VTT_VOICE.match(line)
        if m:
            text = re.sub(r"<[^>]+>", "", m.group(2)).strip()
            out.append(f"{m.group(1).strip()}: {text}" if text else "")
            continue
        out.append(re.sub(r"<[^>]+>", "", line).strip())
    # collapse consecutive duplicates (common in auto captions)
    dedup = [l for i, l in enumerate(out) if i == 0 or l != out[i - 1]]
    return "\n".join(dedup)


def _html(raw: str) -> str:
    raw = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", raw)
    raw = re.sub(r"(?i)<br\s*/?>|</p>|</h\d>|</li>|</div>", "\n", raw)
    text = html_mod.unescape(re.sub(r"<[^>]+>", " ", raw))
    return "\n".join(re.sub(r"[ \t]+", " ", l).strip() for l in text.splitlines() if l.strip())


def ocr_backend() -> str | None:
    if shutil.which("tesseract"):
        return "tesseract"
    if shutil.which("swift") and Path("/System/Library/Frameworks/Vision.framework").exists():
        return "macos-vision"
    return None


_SWIFT = r'''
import Foundation
import Vision
let url = URL(fileURLWithPath: CommandLine.arguments[1])
let handler = VNImageRequestHandler(url: url, options: [:])
let req = VNRecognizeTextRequest()
req.recognitionLevel = .accurate
try? handler.perform([req])
for obs in (req.results ?? []) { if let c = obs.topCandidates(1).first { print(c.string) } }
'''


def _vision_binary() -> Path:
    """Compile the tiny Vision OCR helper once and cache it (compiling on every call is slow)."""
    from . import config

    out = Path(os.environ.get("OPERATOR_BIN_DIR", str(config.ROOT / "data" / "bin"))) / "vision-ocr"
    if not out.exists():
        out.parent.mkdir(parents=True, exist_ok=True)
        src = out.with_suffix(".swift")
        src.write_text(_SWIFT)
        subprocess.run(["swiftc", "-O", str(src), "-o", str(out)], capture_output=True, timeout=300, check=True)
    return out


def ocr(data: bytes, suffix: str) -> tuple[str, str]:
    backend = ocr_backend()
    if backend is None:
        return "", "none"
    with tempfile.TemporaryDirectory() as d:
        img = Path(d) / f"img{suffix}"
        img.write_bytes(data)
        try:
            if backend == "tesseract":
                out = subprocess.run(["tesseract", str(img), "-", "--psm", "3"], capture_output=True, text=True, timeout=60)
            else:
                out = subprocess.run([str(_vision_binary()), str(img)], capture_output=True, text=True, timeout=120)
        except (subprocess.TimeoutExpired, OSError):
            return "", backend + " (failed)"
    return out.stdout.strip(), backend


def extract(filename: str, data: bytes) -> dict:
    """Returns {text, kind, method, pages, warnings}."""
    ext = Path(filename).suffix.lower()
    if ext not in SUPPORTED:
        raise UploadError(f"Unsupported file type {ext or '(none)'}")
    if len(data) > MAX_BYTES:
        raise UploadError("File too large (10 MB max)")
    warnings: list[str] = []
    pages = None
    if ext in TEXT_EXT:
        text, kind, method = data.decode("utf-8", errors="replace"), "text", "utf-8"
    elif ext == ".csv":
        rows = list(csv.reader(io.StringIO(data.decode("utf-8", errors="replace"))))
        text = "\n".join(", ".join(r) for r in rows)
        kind, method = "csv", f"{len(rows)} rows"
    elif ext == ".json":
        try:
            obj = json.loads(data)
        except ValueError:
            raise UploadError("Invalid JSON")
        text, kind, method = json.dumps(obj, indent=1, ensure_ascii=False), "json", "parsed"
    elif ext == ".pdf":
        from pypdf import PdfReader

        try:
            reader = PdfReader(io.BytesIO(data))
            parts = [(p.extract_text() or "") for p in reader.pages]
        except Exception:  # noqa: BLE001
            raise UploadError("Could not read this PDF")
        pages = len(parts)
        text, kind, method = "\n".join(parts), "pdf", "pypdf text layer"
        if not text.strip():
            warnings.append("No text layer found (scanned PDF?). OCR of PDFs is not supported here.")
    elif ext == ".docx":
        import docx

        try:
            d = docx.Document(io.BytesIO(data))
        except Exception:  # noqa: BLE001
            raise UploadError("Could not read this DOCX")
        text, kind, method = "\n".join(p.text for p in d.paragraphs), "docx", "python-docx"
    elif ext in (".srt", ".vtt"):
        text, kind, method = _subtitles(data.decode("utf-8", errors="replace")), "subtitles", "timestamps stripped"
    elif ext in (".html", ".htm"):
        text, kind, method = _html(data.decode("utf-8", errors="replace")), "html", "tags stripped"
    else:
        text, backend = ocr(data, ext)
        kind, method = "image", f"local OCR ({backend})"
        if backend == "none":
            warnings.append("Image stored, but no local OCR engine is installed (install tesseract). No text read.")
        elif not text:
            warnings.append("OCR found no text in this image.")
    return {"text": text, "kind": kind, "method": method, "pages": pages, "warnings": warnings}


def receipt(filename: str, size: int, ex: dict) -> dict:
    text = ex["text"]
    return {"filename": filename, "kind": ex["kind"], "bytes": size, "method": ex["method"], "pages": ex["pages"],
            "chars": len(text), "words": len(text.split()), "lines": len(text.splitlines()),
            "preview": text[:300], "warnings": ex["warnings"],
            "summary": f"I read {len(text.split())} words from {filename} ({ex['kind']}, {ex['method']})."}


def save(tenant, user_id: int, filename: str, data: bytes, project_id: int | None = None, to_kb: bool = False) -> dict:
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", Path(filename).name)[:120] or "upload"
    ex = extract(safe, data)
    rec = receipt(safe, len(data), ex)
    with tenant.db() as c:
        uid = c.execute("INSERT INTO uploads(user_id,project_id,filename,kind,size,text,receipt,created) VALUES(?,?,?,?,?,?,?,?)",
                        (user_id, project_id, safe, ex["kind"], len(data), ex["text"], db.jdump(rec), db.now())).lastrowid
    if to_kb and ex["text"].strip():
        # a client tenant's own material (kb_scope=own) is searchable once uploaded by its owner
        tenant.kb_dir.mkdir(parents=True, exist_ok=True)
        (tenant.kb_dir / f"{Path(safe).stem}.txt").write_text(ex["text"])
        from . import index, skills

        index.invalidate()
        rec["added_to_kb"] = True
        skills.on_source_change(tenant, f"upload:{safe}")  # workers, stale Academy lessons, role-play rubric citations
    rec["id"] = uid
    return rec


def list_for(tenant, user_id: int, project_id: int | None = None) -> list[dict]:
    with tenant.db() as c:
        if project_id:
            rows = db.rows(c.execute("SELECT id,filename,kind,size,receipt,created,project_id FROM uploads WHERE user_id=? "
                                     "AND project_id=? ORDER BY id DESC", (user_id, project_id)))
        else:
            rows = db.rows(c.execute("SELECT id,filename,kind,size,receipt,created,project_id FROM uploads WHERE user_id=? "
                                     "ORDER BY id DESC LIMIT 200", (user_id,)))
    for r in rows:
        r["receipt"] = db.jload(r["receipt"], {})
    return rows


def delete(tenant, user_id: int, upload_id: int) -> bool:
    with tenant.db() as c:
        return c.execute("DELETE FROM uploads WHERE id=? AND user_id=?", (upload_id, user_id)).rowcount > 0
