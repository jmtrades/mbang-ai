"""Claims substantiation ("proof records").

A results figure may only reach a prospect while the owner holds a current substantiation record for it:
at least one evidence file, a typicality note (what typical results look like, in plain words) and an
expiry date. Today that covers the one figure the outreach money rule allows, the hedged "$50k-$100k a
month" range. Every new workspace starts with no record, so:

* guardrails block the range in prospect outreach (``unsubstantiated_claim``), and
* the drafting helpers (``guardrails.range_line``, ``agents.sales.reply_line``, the Studio call offer) fall
  back to the no-figure line "Let me help [Brand] grow through ...".

The owner adds, documents and expires records (Owner console -> Proof records). The demo is seeded with one
fictional, labelled record so its story still shows the range; it is read-only there.
"""
from __future__ import annotations

import hashlib
import re
import shutil
import sqlite3
import time
from pathlib import Path

from . import audit, db, guardrails
from .errors import InputError

CLAIM_KEYS = {"outreach_range": "$50k-$100k a month (the hedged outreach range)"}
DEFAULT_KEY = "outreach_range"
MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_FILES = 10
EVIDENCE_TYPES = {".pdf": "application/pdf", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                  ".webp": "image/webp", ".csv": "text/csv", ".txt": "text/plain", ".md": "text/markdown"}
MIN_NOTE = 20
MAX_VALID_DAYS = 366
DAY = 86400


class ClaimError(InputError):
    """Bad input for a proof record (the API answers 422, or 404 for an unknown record)."""


# --------------------------------------------------------------------------- validity
def _row(r) -> dict:
    d = dict(r)
    d["evidence"] = db.jload(d.get("evidence"), []) or []
    return d


def _state(rec: dict, now: float | None = None) -> str:
    now = now or time.time()
    if rec.get("status") != "active":
        return "expired"
    if (rec.get("expires") or 0) <= now:
        return "expired"
    if not rec.get("evidence"):
        return "needs_evidence"
    if len((rec.get("typicality_note") or "").strip()) < MIN_NOTE:
        return "needs_note"
    return "current"


def valid_record(tenant, claim_key: str = DEFAULT_KEY) -> dict | None:
    """The newest current record for a claim, or None. Never raises: no tenant, no table -> None."""
    if tenant is None:
        return None
    try:
        with tenant.db() as c:
            rows = c.execute("SELECT * FROM claims_substantiation WHERE claim_key=? AND status='active' AND expires>? "
                             "ORDER BY id DESC", (claim_key, time.time())).fetchall()
    except sqlite3.Error:
        return None
    for r in rows:
        rec = _row(r)
        if _state(rec) == "current":
            return rec
    return None


def has_valid(tenant, claim_key: str = DEFAULT_KEY) -> bool:
    return valid_record(tenant, claim_key) is not None


# --------------------------------------------------------------------------- guardrail plug-in
_RANGE_HINT = re.compile(r"\$\s?50\s?k", re.I)


def guard_rules(text: str, mode: str, meta: dict, tenant) -> list[dict]:
    """In prospect outreach the $50k-$100k range needs a current proof record (none by default)."""
    if mode != "outreach" or not _RANGE_HINT.search(text or ""):
        return []
    if not guardrails._ALLOWED_RANGE.search(guardrails._money_norm(text)):
        return []
    if has_valid(tenant, DEFAULT_KEY):
        return []
    return [guardrails._flag(
        "unsubstantiated_claim", "block",
        "The $50k-$100k figure needs a current proof record (evidence and a typicality note, Owner console -> "
        "Proof records). Until then use the no-figure line: \"Let me help [Brand] grow through ...\"",
        "$50k-$100k")]


# --------------------------------------------------------------------------- owner actions
def _dir(tenant, rid: int) -> Path:
    return tenant.dir / "claims" / str(int(rid))


def _get(tenant, rid: int) -> dict:
    with tenant.db() as c:
        r = c.execute("SELECT * FROM claims_substantiation WHERE id=?", (rid,)).fetchone()
    if r is None:
        raise ClaimError("Proof record not found", 404)
    return _row(r)


def public(rec: dict) -> dict:
    """What the API returns: no file paths, just names, sizes and hashes of the evidence."""
    out = {k: rec.get(k) for k in ("id", "claim_key", "statement", "typicality_note", "status", "created_by",
                                   "created", "expires", "expired_at", "expired_by", "expire_reason", "demo")}
    out["evidence"] = [{k: e.get(k) for k in ("id", "name", "size", "sha256", "type", "added", "demo")}
                       for e in rec.get("evidence") or []]
    out["state"] = _state(rec)
    out["claim_label"] = CLAIM_KEYS.get(rec.get("claim_key"), rec.get("claim_key"))
    return out


def list_records(tenant) -> list[dict]:
    with tenant.db() as c:
        rows = c.execute("SELECT * FROM claims_substantiation ORDER BY id DESC LIMIT 200").fetchall()
    return [public(_row(r)) for r in rows]


def add(tenant, claim_key: str, typicality_note: str, valid_days: int, actor: str, statement: str | None = None,
        demo: bool = False) -> dict:
    if claim_key not in CLAIM_KEYS:
        raise ClaimError(f"Unknown claim. Known: {', '.join(CLAIM_KEYS)}")
    note = " ".join(str(typicality_note or "").split())
    if len(note) < MIN_NOTE or len(note) > 1000:
        raise ClaimError(f"The typicality note must be {MIN_NOTE}-1000 characters: say what typical results look like")
    try:
        days = int(valid_days)
    except (TypeError, ValueError):
        raise ClaimError("valid_days must be a whole number")
    if not 1 <= days <= MAX_VALID_DAYS:
        raise ClaimError(f"A proof record is valid for 1-{MAX_VALID_DAYS} days; review it at least yearly")
    stmt = " ".join(str(statement or CLAIM_KEYS[claim_key]).split())[:300]
    now = time.time()
    with tenant.db() as c:
        rid = c.execute("INSERT INTO claims_substantiation(claim_key,statement,typicality_note,evidence,status,created_by,"
                        "created,expires,demo) VALUES(?,?,?,?,?,?,?,?,?)",
                        (claim_key, stmt, note, "[]", "active", actor, now, now + days * DAY,
                         1 if (demo or tenant.is_demo) else 0)).lastrowid
    audit.log(tenant, actor, "claims.add", {"id": rid, "claim": claim_key, "days": days})
    return public(_get(tenant, rid))


_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def safe_name(filename: str) -> str:
    base = Path(str(filename or "").replace("\\", "/")).name  # never a path: "../../x" -> "x"
    base = _SAFE.sub("-", base).strip(".-")[:80]
    return base or "evidence"


def add_evidence(tenant, rid: int, filename: str, data: bytes, actor: str) -> dict:
    rec = _get(tenant, rid)
    if rec["status"] != "active":
        raise ClaimError("This record has expired; add a new one instead", 409)
    name = safe_name(filename)
    ext = Path(name).suffix.lower()
    if ext not in EVIDENCE_TYPES:
        raise ClaimError(f"Evidence must be one of: {', '.join(sorted(EVIDENCE_TYPES))}")
    if not data:
        raise ClaimError("The file is empty")
    if len(data) > MAX_FILE_BYTES:
        raise ClaimError(f"Evidence files are limited to {MAX_FILE_BYTES // (1024 * 1024)} MB each")
    if len(rec["evidence"]) >= MAX_FILES:
        raise ClaimError(f"A record holds at most {MAX_FILES} evidence files")
    eid = (max((e.get("id", 0) for e in rec["evidence"]), default=0) or 0) + 1
    d = _dir(tenant, rid)
    d.mkdir(parents=True, exist_ok=True)
    stored = d / f"{eid}-{name}"
    stored.write_bytes(data)
    stored.chmod(0o600)
    ev = {"id": eid, "name": name, "size": len(data), "sha256": hashlib.sha256(data).hexdigest(),
          "type": EVIDENCE_TYPES[ext], "added": time.time(), "stored": stored.name, "by": actor,
          "demo": 1 if tenant.is_demo else 0}
    with tenant.db() as c:
        c.execute("UPDATE claims_substantiation SET evidence=? WHERE id=?", (db.jdump(rec["evidence"] + [ev]), rid))
    audit.log(tenant, actor, "claims.evidence", {"id": rid, "file": name, "size": len(data)})
    return public(_get(tenant, rid))


def evidence_file(tenant, rid: int, eid: int) -> tuple[Path, dict]:
    rec = _get(tenant, rid)
    ev = next((e for e in rec["evidence"] if e.get("id") == eid), None)
    if ev is None:
        raise ClaimError("Evidence not found", 404)
    p = (_dir(tenant, rid) / str(ev.get("stored") or "")).resolve()
    if not p.is_relative_to(_dir(tenant, rid).resolve()) or not p.is_file():
        raise ClaimError("Evidence not found", 404)
    return p, ev


def expire(tenant, rid: int, actor: str, reason: str = "") -> dict:
    rec = _get(tenant, rid)
    if rec["status"] != "active":
        return public(rec)
    with tenant.db() as c:
        c.execute("UPDATE claims_substantiation SET status='expired', expired_at=?, expired_by=?, expire_reason=? WHERE id=?",
                  (time.time(), actor, " ".join(str(reason or "").split())[:300], rid))
    audit.log(tenant, actor, "claims.expire", {"id": rid})
    return public(_get(tenant, rid))


# --------------------------------------------------------------------------- demo
def seed_demo(tenant) -> None:
    """One fictional, labelled record so the demo story can show the range. Idempotent; demo DB only."""
    from . import tenancy

    if not tenancy.is_demo_db(tenant):
        return
    with tenant.db() as c:
        if c.execute("SELECT 1 FROM claims_substantiation WHERE demo=1 LIMIT 1").fetchone():
            return
    rec = add(tenant, DEFAULT_KEY,
              "(DEMO) Fictional record for the pitch demo. The range describes the best months of a few fictional "
              "clinics; typical results are lower and some clients see no increase at all.",
              365, "system", statement=CLAIM_KEYS[DEFAULT_KEY] + " (DEMO)", demo=True)
    add_evidence(tenant, rec["id"], "evidence-summary-DEMO.txt",
                 b"(DEMO) Fictional evidence summary for the pitch demo. Not a real client, not a real result.\n",
                 "system")


def reset_files(tenant) -> None:
    """Demo reset: evidence files go with the rows (demo DB only)."""
    from . import tenancy

    if tenancy.is_demo_db(tenant):
        shutil.rmtree(tenant.dir / "claims", ignore_errors=True)
