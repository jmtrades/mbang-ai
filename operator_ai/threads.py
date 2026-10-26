"""Chat threads: auto-title, rename, search, archive, internal share link; projects with
scoped, user-deletable memory."""
from __future__ import annotations

import secrets

from . import db


def _title_from(text: str) -> str:
    t = " ".join((text or "").split())
    return (t[:57] + "…") if len(t) > 58 else (t or "New thread")


def create(tenant, user_id: int | None, first_text: str = "", project_id: int | None = None) -> int:
    with tenant.db() as c:
        return c.execute("INSERT INTO threads(user_id,project_id,title,created,updated) VALUES(?,?,?,?,?)",
                         (user_id, project_id, _title_from(first_text), db.now(), db.now())).lastrowid


def add_message(tenant, thread_id: int, role: str, content: str, meta: dict | None = None) -> int:
    with tenant.db() as c:
        mid = c.execute("INSERT INTO messages(thread_id,role,content,meta,created) VALUES(?,?,?,?,?)",
                        (thread_id, role, content, db.jdump(meta or {}), db.now())).lastrowid
        c.execute("UPDATE threads SET updated=? WHERE id=?", (db.now(), thread_id))
    return mid


def get(tenant, thread_id: int, user_id: int | None = None) -> dict | None:
    with tenant.db() as c:
        t = db.row(c.execute("SELECT * FROM threads WHERE id=?", (thread_id,)).fetchone())
        if t is None or (user_id is not None and t["user_id"] != user_id):
            return None
        msgs = db.rows(c.execute("SELECT * FROM messages WHERE thread_id=? ORDER BY id", (thread_id,)))
    for m in msgs:
        m["meta"] = db.jload(m["meta"], {})
    t["messages"] = msgs
    return t


def by_share(tenant, token: str) -> dict | None:
    with tenant.db() as c:
        r = c.execute("SELECT id FROM threads WHERE share_token=?", (token,)).fetchone()
    return get(tenant, r["id"]) if r else None


def list_for(tenant, user_id: int, q: str | None = None, archived: bool = False, project_id: int | None = None) -> list[dict]:
    sql = "SELECT * FROM threads WHERE user_id=? AND archived=?"
    args: list = [user_id, 1 if archived else 0]
    if project_id:
        sql += " AND project_id=?"
        args.append(project_id)
    if q:
        sql += " AND (title LIKE ? OR id IN (SELECT thread_id FROM messages WHERE content LIKE ?))"
        args += [f"%{q}%", f"%{q}%"]
    sql += " ORDER BY updated DESC LIMIT 200"
    with tenant.db() as c:
        return db.rows(c.execute(sql, args))


def update(tenant, thread_id: int, user_id: int, title: str | None = None, archived: bool | None = None) -> bool:
    with tenant.db() as c:
        t = c.execute("SELECT user_id FROM threads WHERE id=?", (thread_id,)).fetchone()
        if t is None or t["user_id"] != user_id:
            return False
        if title is not None:
            c.execute("UPDATE threads SET title=? WHERE id=?", (title[:120], thread_id))
        if archived is not None:
            c.execute("UPDATE threads SET archived=? WHERE id=?", (1 if archived else 0, thread_id))
    return True


def share(tenant, thread_id: int, user_id: int) -> str | None:
    tok = secrets.token_urlsafe(12)
    with tenant.db() as c:
        t = c.execute("SELECT user_id, share_token FROM threads WHERE id=?", (thread_id,)).fetchone()
        if t is None or t["user_id"] != user_id:
            return None
        if t["share_token"]:
            return t["share_token"]
        c.execute("UPDATE threads SET share_token=? WHERE id=?", (tok, thread_id))
    return tok


# ---------------------------------------------------------------- projects + memory
def create_project(tenant, user_id: int, name: str, description: str = "") -> int:
    with tenant.db() as c:
        return c.execute("INSERT INTO projects(user_id,name,description,created) VALUES(?,?,?,?)",
                         (user_id, name[:120], description[:2000], db.now())).lastrowid


def projects(tenant, user_id: int) -> list[dict]:
    with tenant.db() as c:
        return db.rows(c.execute("SELECT * FROM projects WHERE user_id=? ORDER BY id DESC", (user_id,)))


def add_memory(tenant, user_id: int, project_id: int | None, content: str) -> int:
    with tenant.db() as c:
        if project_id is not None:
            p = c.execute("SELECT user_id FROM projects WHERE id=?", (project_id,)).fetchone()
            if p is None or p["user_id"] != user_id:
                raise PermissionError("not your project")
        return c.execute("INSERT INTO memories(user_id,project_id,content,created) VALUES(?,?,?,?)",
                         (user_id, project_id, content[:2000], db.now())).lastrowid


def memories(tenant, user_id: int, project_id: int | None = None) -> list[dict]:
    with tenant.db() as c:
        if project_id is None:
            return db.rows(c.execute("SELECT * FROM memories WHERE user_id=? AND project_id IS NULL ORDER BY id DESC", (user_id,)))
        return db.rows(c.execute("SELECT * FROM memories WHERE user_id=? AND project_id=? ORDER BY id DESC",
                                 (user_id, project_id)))


def delete_memory(tenant, user_id: int, memory_id: int) -> bool:
    with tenant.db() as c:
        return c.execute("DELETE FROM memories WHERE id=? AND user_id=?", (memory_id, user_id)).rowcount > 0
