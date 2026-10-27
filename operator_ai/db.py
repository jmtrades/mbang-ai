"""SQLite helpers. One database per tenant (plus the registry and the demo DB),
WAL mode, a fresh connection per unit of work."""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable

TENANT_SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL, display TEXT, role TEXT NOT NULL,
  pw_hash TEXT NOT NULL, coach_id INTEGER, student_id INTEGER, active INTEGER DEFAULT 1, created REAL);
CREATE TABLE IF NOT EXISTS sessions (
  token_hash TEXT PRIMARY KEY, user_id INTEGER, created REAL, expires REAL, ip TEXT);
CREATE TABLE IF NOT EXISTS login_fails (key TEXT, at REAL);
CREATE TABLE IF NOT EXISTS audit (
  id INTEGER PRIMARY KEY, at REAL, actor TEXT, kind TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY, agent TEXT, title TEXT, plan TEXT, steps TEXT, result TEXT, status TEXT,
  ms INTEGER, ai_mode TEXT, created REAL, actor TEXT);
CREATE TABLE IF NOT EXISTS queue (
  id INTEGER PRIMARY KEY, kind TEXT, channel TEXT, recipient_ref TEXT, title TEXT, body TEXT,
  meta TEXT, status TEXT, flags TEXT, decided_by TEXT, decided_at REAL, created REAL,
  run_id INTEGER, delivered TEXT, demo INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS queue_status ON queue(status);
CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY, key TEXT UNIQUE, kind TEXT, payload TEXT, status TEXT, attempts INTEGER DEFAULT 0,
  next_at REAL, result TEXT, error TEXT, created REAL, updated REAL, batch TEXT);
CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status, next_at);
CREATE TABLE IF NOT EXISTS actions (
  id INTEGER PRIMARY KEY, kind TEXT, proposal TEXT, prior_state TEXT, status TEXT, rollback_of INTEGER,
  queue_id INTEGER, result TEXT, created REAL, decided_by TEXT);
CREATE TABLE IF NOT EXISTS students (
  id INTEGER PRIMARY KEY, name TEXT, email TEXT, coach_id INTEGER, joined REAL, niche TEXT,
  stage TEXT, clients INTEGER DEFAULT 0, demo INTEGER DEFAULT 0, intake TEXT, plan TEXT);
CREATE TABLE IF NOT EXISTS checkins (
  id INTEGER PRIMARY KEY, student_id INTEGER, at REAL, outreach INTEGER, calls_booked INTEGER,
  calls_showed INTEGER, mood INTEGER, wins TEXT, blockers TEXT, demo INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS checkins_student ON checkins(student_id, at);
CREATE TABLE IF NOT EXISTS coach_tasks (
  id INTEGER PRIMARY KEY, coach_id INTEGER, student_id INTEGER, kind TEXT, detail TEXT, status TEXT,
  due REAL, created REAL, demo INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS leads (
  id INTEGER PRIMARY KEY, name TEXT, handle TEXT, email TEXT, source TEXT, answers TEXT, tier TEXT,
  score INTEGER, route TEXT, stage TEXT, created REAL, demo INTEGER DEFAULT 0, anon_id TEXT, batch TEXT);
CREATE TABLE IF NOT EXISTS clients (
  id INTEGER PRIMARY KEY, student_id INTEGER, name TEXT, niche TEXT, offer TEXT, price REAL,
  metrics TEXT, demo INTEGER DEFAULT 0, created REAL);
CREATE TABLE IF NOT EXISTS threads (
  id INTEGER PRIMARY KEY, user_id INTEGER, project_id INTEGER, title TEXT, archived INTEGER DEFAULT 0,
  share_token TEXT, created REAL, updated REAL);
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY, thread_id INTEGER, role TEXT, content TEXT, meta TEXT, created REAL);
CREATE TABLE IF NOT EXISTS projects (
  id INTEGER PRIMARY KEY, user_id INTEGER, name TEXT, description TEXT, created REAL);
CREATE TABLE IF NOT EXISTS memories (
  id INTEGER PRIMARY KEY, user_id INTEGER, project_id INTEGER, content TEXT, created REAL);
CREATE TABLE IF NOT EXISTS uploads (
  id INTEGER PRIMARY KEY, user_id INTEGER, project_id INTEGER, filename TEXT, kind TEXT, size INTEGER,
  text TEXT, receipt TEXT, created REAL);
CREATE TABLE IF NOT EXISTS profiles (id INTEGER PRIMARY KEY, scope TEXT UNIQUE, data TEXT, updated REAL);
CREATE TABLE IF NOT EXISTS workers (
  id INTEGER PRIMARY KEY, name TEXT, skill TEXT, input TEXT, trigger TEXT, schedule TEXT, needs TEXT,
  last_run REAL, last_status TEXT, enabled INTEGER DEFAULT 1, created REAL);
CREATE TABLE IF NOT EXISTS eval_runs (
  id INTEGER PRIMARY KEY, kind TEXT, metrics TEXT, corpus TEXT, created REAL);
CREATE TABLE IF NOT EXISTS calendar_events (
  id INTEGER PRIMARY KEY, title TEXT, start REAL, end REAL, attendees TEXT, status TEXT, ext_id TEXT,
  demo INTEGER DEFAULT 0, created REAL);
CREATE TABLE IF NOT EXISTS chain_inbox (
  id INTEGER PRIMARY KEY, dedupe_key TEXT UNIQUE, body TEXT, status TEXT, attempts INTEGER DEFAULT 0,
  next_at REAL, error TEXT, created REAL);
CREATE TABLE IF NOT EXISTS chain_leads (
  id INTEGER PRIMARY KEY, leadgen_id TEXT UNIQUE, data TEXT, tier TEXT, score INTEGER, citations TEXT,
  stage TEXT, created REAL);
CREATE TABLE IF NOT EXISTS chain_bookings (
  id INTEGER PRIMARY KEY, lead_id INTEGER, slot_start REAL, slot_end REAL, event_id TEXT, status TEXT,
  queue_id INTEGER, created REAL);
CREATE TABLE IF NOT EXISTS chain_events (
  id INTEGER PRIMARY KEY, lead_id INTEGER, kind TEXT, detail TEXT, created REAL);
CREATE TABLE IF NOT EXISTS outreach_loops (
  id INTEGER PRIMARY KEY, prospect_id INTEGER, step TEXT, queue_id INTEGER, opener_move TEXT, sender TEXT,
  status TEXT, created REAL);
CREATE TABLE IF NOT EXISTS outreach_practice_prospects (
  id INTEGER PRIMARY KEY, name TEXT, handle TEXT, niche TEXT, followers INTEGER, stage TEXT,
  opener_move TEXT, sender TEXT, tier TEXT, replied INTEGER DEFAULT 0, created REAL, demo INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS biz_builds (
  id INTEGER PRIMARY KEY, preset TEXT, business TEXT, departments TEXT, status TEXT, created REAL);
CREATE TABLE IF NOT EXISTS biz_agent_runs (
  id INTEGER PRIMARY KEY, build_id INTEGER, agent_key TEXT, run_id INTEGER, summary TEXT, created REAL);
CREATE TABLE IF NOT EXISTS studio_runs (
  id INTEGER PRIMARY KEY, line TEXT, brief TEXT, run_id INTEGER, queue_ids TEXT, kit_id TEXT, created REAL);
CREATE TABLE IF NOT EXISTS mock_ads (
  id TEXT PRIMARY KEY, kind TEXT, parent TEXT, name TEXT, status TEXT, daily_budget REAL, data TEXT,
  metrics TEXT, updated REAL);
CREATE TABLE IF NOT EXISTS crm_contacts (
  id INTEGER PRIMARY KEY, name TEXT, email TEXT, stage TEXT, tags TEXT, demo INTEGER DEFAULT 0, created REAL);
CREATE TABLE IF NOT EXISTS crm_notes (id INTEGER PRIMARY KEY, contact_id INTEGER, body TEXT, created REAL);
CREATE TABLE IF NOT EXISTS crm_tasks (
  id INTEGER PRIMARY KEY, contact_id INTEGER, title TEXT, status TEXT, due REAL, created REAL);
CREATE TABLE IF NOT EXISTS calls (
  id INTEGER PRIMARY KEY, title TEXT, rep TEXT, transcript TEXT, parties TEXT, status TEXT, review TEXT,
  created REAL, demo INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS call_consents (
  id INTEGER PRIMARY KEY, call_id INTEGER, party TEXT, method TEXT, at REAL);
CREATE TABLE IF NOT EXISTS drills (
  id INTEGER PRIMARY KEY, objection TEXT, answer TEXT, citation TEXT, source TEXT, created REAL);
CREATE TABLE IF NOT EXISTS drill_cards (
  id INTEGER PRIMARY KEY, drill_id INTEGER, user_id INTEGER, ease REAL DEFAULT 2.5, interval REAL DEFAULT 0,
  reps INTEGER DEFAULT 0, due REAL, last_grade INTEGER);
CREATE TABLE IF NOT EXISTS channel_links (
  id INTEGER PRIMARY KEY, channel TEXT, ext_id TEXT, user_id INTEGER, code TEXT, opted_in INTEGER DEFAULT 0,
  status TEXT, created REAL);
CREATE TABLE IF NOT EXISTS channel_messages (
  id INTEGER PRIMARY KEY, channel TEXT, direction TEXT, ext_id TEXT, body TEXT, meta TEXT, created REAL);
CREATE TABLE IF NOT EXISTS mcp_tokens (
  id INTEGER PRIMARY KEY, label TEXT, token_hash TEXT UNIQUE, created REAL, revoked INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS minis (
  id INTEGER PRIMARY KEY, slug TEXT UNIQUE, title TEXT, scope TEXT, question_limit INTEGER, consent_text TEXT,
  created REAL);
CREATE TABLE IF NOT EXISTS mini_sessions (
  id INTEGER PRIMARY KEY, mini_id INTEGER, token TEXT UNIQUE, email TEXT, consent INTEGER, questions INTEGER DEFAULT 0,
  created REAL);
CREATE TABLE IF NOT EXISTS payment_events (
  id INTEGER PRIMARY KEY, event_id TEXT UNIQUE, kind TEXT, email TEXT, data TEXT, created REAL);
CREATE TABLE IF NOT EXISTS members (
  id INTEGER PRIMARY KEY, email TEXT UNIQUE, status TEXT, plan TEXT, updated REAL, demo INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS drive_staged (
  id INTEGER PRIMARY KEY, filename TEXT, sha TEXT, status TEXT, flags TEXT, text TEXT, created REAL);
CREATE TABLE IF NOT EXISTS voice_sessions (
  id INTEGER PRIMARY KEY, user_id INTEGER, consent INTEGER, minutes REAL, transcript TEXT, recap TEXT,
  created REAL);
CREATE TABLE IF NOT EXISTS rehearsals (
  id INTEGER PRIMARY KEY, title TEXT, script TEXT, score TEXT, created REAL);
CREATE TABLE IF NOT EXISTS roast_docs (id INTEGER PRIMARY KEY, title TEXT, body TEXT, review TEXT, created REAL);
CREATE TABLE IF NOT EXISTS ai_replays (
  key TEXT PRIMARY KEY, purpose TEXT, output TEXT, run_id INTEGER, created REAL);
CREATE TABLE IF NOT EXISTS queries (
  id INTEGER PRIMARY KEY, user_id INTEGER, question TEXT, covered TEXT, lang TEXT, run_id INTEGER, created REAL);
CREATE TABLE IF NOT EXISTS kb_notes (
  id INTEGER PRIMARY KEY, file TEXT, priority REAL, note TEXT, provenance TEXT, licence TEXT, updated REAL);
CREATE TABLE IF NOT EXISTS chain_steps (
  id INTEGER PRIMARY KEY, chain_id TEXT, step TEXT, deps TEXT, output TEXT, queue_id INTEGER, run_id INTEGER,
  version INTEGER DEFAULT 1, created REAL);
CREATE TABLE IF NOT EXISTS academy_lessons (
  id INTEGER PRIMARY KEY, path_key TEXT, ord INTEGER, title TEXT, body TEXT, key_points TEXT, citations TEXT,
  source_file TEXT, source_kind TEXT, created REAL);
CREATE TABLE IF NOT EXISTS academy_quiz_items (
  id INTEGER PRIMARY KEY, lesson_id INTEGER, kind TEXT, prompt TEXT, options TEXT, answer TEXT, explanation TEXT,
  citation TEXT, created REAL);
CREATE TABLE IF NOT EXISTS academy_progress (
  id INTEGER PRIMARY KEY, user_id INTEGER, lesson_id INTEGER, status TEXT, score REAL, attempts INTEGER DEFAULT 0,
  updated REAL, UNIQUE(user_id, lesson_id));
CREATE TABLE IF NOT EXISTS academy_attempts (
  id INTEGER PRIMARY KEY, user_id INTEGER, quiz_item_id INTEGER, answer TEXT, correct INTEGER, created REAL);
CREATE TABLE IF NOT EXISTS academy_review (
  id INTEGER PRIMARY KEY, user_id INTEGER, quiz_item_id INTEGER, ease REAL DEFAULT 2.5, interval REAL DEFAULT 0,
  reps INTEGER DEFAULT 0, due REAL, UNIQUE(user_id, quiz_item_id));
CREATE TABLE IF NOT EXISTS roleplays (
  id INTEGER PRIMARY KEY, user_id INTEGER, scenario_key TEXT, transcript TEXT, state TEXT, score TEXT, status TEXT,
  created REAL, updated REAL);
CREATE TABLE IF NOT EXISTS certifications (
  id INTEGER PRIMARY KEY, user_id INTEGER, path_key TEXT, level TEXT, awarded REAL, evidence TEXT,
  UNIQUE(user_id, path_key, level));
CREATE TABLE IF NOT EXISTS missions (
  id INTEGER PRIMARY KEY, user_id INTEGER, goal TEXT, template TEXT, status TEXT, plan TEXT, context TEXT,
  progress TEXT, metrics TEXT, created REAL, updated REAL, next_check REAL);
CREATE TABLE IF NOT EXISTS mission_steps (
  id INTEGER PRIMARY KEY, mission_id INTEGER, ord INTEGER, title TEXT, agent TEXT, params TEXT, depends TEXT,
  status TEXT, run_id INTEGER, output TEXT, queue_ids TEXT, due REAL, created REAL, updated REAL);
CREATE TABLE IF NOT EXISTS mission_events (
  id INTEGER PRIMARY KEY, mission_id INTEGER, kind TEXT, detail TEXT, created REAL);
CREATE TABLE IF NOT EXISTS prospects (
  id INTEGER PRIMARY KEY, owner_user_id INTEGER, list_name TEXT, name TEXT, handle TEXT, email TEXT, website TEXT,
  niche TEXT, followers INTEGER, offer TEXT, notes TEXT, research TEXT, fit_score INTEGER, stage TEXT, source TEXT,
  lawful_basis TEXT, do_not_contact INTEGER DEFAULT 0, demo INTEGER DEFAULT 0, mission_id INTEGER, created REAL,
  updated REAL);
CREATE INDEX IF NOT EXISTS prospects_owner ON prospects(owner_user_id, stage);
CREATE TABLE IF NOT EXISTS proposals (
  id INTEGER PRIMARY KEY, owner_user_id INTEGER, prospect_id INTEGER, title TEXT, body TEXT, terms TEXT, status TEXT,
  queue_id INTEGER, created REAL);
CREATE TABLE IF NOT EXISTS persona_profiles (
  id INTEGER PRIMARY KEY, person TEXT, status TEXT, consent TEXT, profile TEXT, created REAL, updated REAL);
CREATE TABLE IF NOT EXISTS invites (
  id INTEGER PRIMARY KEY, email TEXT, role TEXT, token_hash TEXT UNIQUE, created_by TEXT, created REAL, expires REAL,
  used REAL);
CREATE TABLE IF NOT EXISTS app_events (
  id INTEGER PRIMARY KEY, name TEXT, payload TEXT, created REAL);
CREATE INDEX IF NOT EXISTS app_events_name ON app_events(name, created);
-- [academy] tables
-- [roleplay] tables
-- [missions] tables
-- [prospects] tables
-- [delivery] tables
-- [inbound] tables
-- [client_ops] tables
CREATE TABLE IF NOT EXISTS client_metrics (
  id INTEGER PRIMARY KEY, client_id INTEGER, period_start REAL, metrics TEXT, source TEXT, note TEXT, created REAL,
  demo INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS client_metrics_client ON client_metrics(client_id, period_start);
CREATE TABLE IF NOT EXISTS client_tasks (
  id INTEGER PRIMARY KEY, client_id INTEGER, key TEXT, kind TEXT, title TEXT, detail TEXT, status TEXT, verified_by TEXT,
  due REAL, done_at REAL, done_by TEXT, created REAL, demo INTEGER DEFAULT 0, UNIQUE(client_id, key));
-- [webinars] tables
CREATE TABLE IF NOT EXISTS webinars (
  id INTEGER PRIMARY KEY, chain_id TEXT, slug TEXT UNIQUE, title TEXT, starts_at REAL, tz TEXT, join_url TEXT,
  replay_url TEXT, status TEXT, duration_min INTEGER DEFAULT 60, offer_at_min INTEGER DEFAULT 35, client_id INTEGER,
  owner_user_id INTEGER, page TEXT, page_qid INTEGER, visits INTEGER DEFAULT 0, post_event_at REAL, demo INTEGER DEFAULT 0,
  created REAL, updated REAL);
CREATE TABLE IF NOT EXISTS webinar_registrants (
  id INTEGER PRIMARY KEY, webinar_id INTEGER, name TEXT, email TEXT, source TEXT, consent INTEGER DEFAULT 0,
  registered_at REAL, attended INTEGER DEFAULT 0, minutes_watched REAL DEFAULT 0, stayed_past_offer INTEGER DEFAULT 0,
  booked INTEGER DEFAULT 0, unsubscribed INTEGER DEFAULT 0, token TEXT, post_done INTEGER DEFAULT 0, demo INTEGER DEFAULT 0,
  UNIQUE(webinar_id, email));
CREATE INDEX IF NOT EXISTS webinar_registrants_w ON webinar_registrants(webinar_id);
CREATE TABLE IF NOT EXISTS webinar_templates (
  id INTEGER PRIMARY KEY, webinar_id INTEGER, key TEXT, phase TEXT, body TEXT, queue_id INTEGER, status TEXT,
  approved_by TEXT, approved_at REAL, created REAL, UNIQUE(webinar_id, key));
CREATE TABLE IF NOT EXISTS webinar_sends (
  id INTEGER PRIMARY KEY, webinar_id INTEGER, registrant_id INTEGER, template_key TEXT, queue_id INTEGER, created REAL,
  UNIQUE(webinar_id, registrant_id, template_key));
-- [persona] tables
-- [team] tables
-- [playbooks] tables
-- [claims] tables
-- [memory] tables
"""

# Columns added to existing tables after first release. CREATE TABLE IF NOT EXISTS never alters a table that
# already exists, so each new column is listed here and added on open when missing: (table, column, declaration).
COLUMN_MIGRATIONS: list[tuple[str, str, str]] = [
    # [academy]
    # [roleplay]
    # [missions]
    # [prospects]
    # [delivery]
    # [inbound]
    # [client_ops]
    ("clients", "status", "TEXT"),
    ("clients", "start_date", "REAL"),
    ("clients", "retainer", "REAL"),
    ("clients", "brief", "TEXT"),
    ("clients", "kpi_targets", "TEXT"),
    ("clients", "access", "TEXT"),
    ("clients", "owner_user_id", "INTEGER"),
    ("clients", "email", "TEXT"),
    ("clients", "contact_name", "TEXT"),
    ("clients", "report_weekday", "INTEGER DEFAULT 0"),
    ("clients", "intake_token", "TEXT"),
    ("clients", "intake_at", "REAL"),
    ("clients", "last_report_at", "REAL"),
    ("clients", "last_review_at", "REAL"),
    ("clients", "risk", "TEXT"),
    ("clients", "prospect_id", "INTEGER"),
    # [webinars]
    # [persona]
    # [team]
    # [playbooks]
    # [claims]
]

REGISTRY_SCHEMA = """
CREATE TABLE IF NOT EXISTS tenants (
  id TEXT PRIMARY KEY, name TEXT, kb_scope TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'client', created REAL,
  is_demo INTEGER DEFAULT 0);
"""


def connect(path: Path | str) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30, check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


_initialised: set[str] = set()


def init_tenant_db(path: Path | str) -> None:
    key = str(path)
    conn = connect(path)
    try:
        conn.executescript(TENANT_SCHEMA)
        _migrate_columns(conn)
    finally:
        conn.close()
    _initialised.add(key)


def _migrate_columns(conn: sqlite3.Connection) -> None:
    have: dict[str, set[str]] = {}
    for table, col, decl in COLUMN_MIGRATIONS:
        if table not in have:
            have[table] = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if col not in have[table]:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
            have[table].add(col)


def init_registry(path: Path | str) -> None:
    conn = connect(path)
    try:
        conn.executescript(REGISTRY_SCHEMA)
    finally:
        conn.close()


@contextmanager
def open_db(path: Path | str):
    if str(path) not in _initialised:
        init_tenant_db(path)
    conn = connect(path)
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def tx(conn: sqlite3.Connection):
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def rows(cur: Iterable[sqlite3.Row]) -> list[dict]:
    return [dict(r) for r in cur]


def row(r: sqlite3.Row | None) -> dict | None:
    return dict(r) if r is not None else None


def jdump(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


def jload(s: str | None, default: Any = None) -> Any:
    if s is None or s == "":
        return default
    try:
        return json.loads(s)
    except (TypeError, ValueError):
        return default


def now() -> float:
    return time.time()
