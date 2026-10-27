"""Read-only MCP server: POST /mcp (JSON-RPC 2.0) with owner-minted bearer tokens.
Tools: ask_coach, search_library, list_skills, run_skill (returns a draft, never queues or
sends), get_business_profile."""
from __future__ import annotations

import hashlib
import secrets

from . import audit, db, retrieval, tenancy

PROTOCOL = "2025-06-18"
TOOLS = [
    {"name": "ask_coach", "description": "Ask the programme coach. Cited answers only from the owner's material.",
     "inputSchema": {"type": "object", "properties": {"question": {"type": "string"}}, "required": ["question"]}},
    {"name": "search_library", "description": "Search the knowledge base.",
     "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}, "k": {"type": "integer"}},
                     "required": ["query"]}},
    {"name": "list_skills", "description": "List the skills library.", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "run_skill", "description": "Draft with a skill. Read-only: returns the draft; nothing is queued or sent.",
     "inputSchema": {"type": "object", "properties": {"skill": {"type": "string"}, "inputs": {"type": "object"}},
                     "required": ["skill"]}},
    {"name": "get_business_profile", "description": "The 8-section business profile.",
     "inputSchema": {"type": "object", "properties": {}}},
]


def _hash(tok: str) -> str:
    return hashlib.sha256(tok.encode()).hexdigest()


def mint(tenant, label: str, actor: str) -> str:
    tok = f"opmcp.{tenant.id}.{secrets.token_urlsafe(24)}"
    with tenant.db() as c:
        c.execute("INSERT INTO mcp_tokens(label,token_hash,created) VALUES(?,?,?)", (label[:80], _hash(tok), db.now()))
    audit.log(tenant, actor, "mcp.token", {"minted": label[:80]})
    return tok  # shown once to the owner


def revoke(tenant, token_id: int, actor: str) -> None:
    with tenant.db() as c:
        c.execute("UPDATE mcp_tokens SET revoked=1 WHERE id=?", (token_id,))
    audit.log(tenant, actor, "mcp.token", {"revoked": token_id})


def list_tokens(tenant) -> list[dict]:
    with tenant.db() as c:
        return db.rows(c.execute("SELECT id,label,created,revoked FROM mcp_tokens ORDER BY id DESC"))


def tenant_for_token(auth_header: str | None, tid: str | None = None):
    if not auth_header or not auth_header.lower().startswith("bearer "):
        return None
    tok = auth_header.split(" ", 1)[1].strip()
    parts = tok.split(".")
    if len(parts) != 3 or parts[0] != "opmcp":
        return None
    t_id = parts[1]
    if tid and tid != t_id:
        return None
    try:
        t = tenancy.get(t_id)
    except tenancy.TenantNotFound:
        return None
    with t.db() as c:
        r = c.execute("SELECT revoked FROM mcp_tokens WHERE token_hash=?", (_hash(tok),)).fetchone()
    if r is None or r["revoked"]:
        return None
    return t


def _err(rid, code: int, msg: str) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": msg}}


def handle(tenant, req: dict) -> dict | None:
    rid = req.get("id")
    method = req.get("method")
    params = req.get("params") or {}
    if req.get("jsonrpc") != "2.0" or not isinstance(method, str):
        return _err(rid, -32600, "Invalid Request")
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": rid, "result": {"protocolVersion": PROTOCOL, "capabilities": {"tools": {}},
                                                        "serverInfo": {"name": "operator-ai", "version": "0.1.0"}}}
    if method.startswith("notifications/"):
        return None
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS}}
    if method != "tools/call":
        return _err(rid, -32601, "Method not found")
    name = params.get("name")
    args = params.get("arguments") or {}
    try:
        data = _call(tenant, name, args)
    except KeyError as e:
        return _err(rid, -32602, f"Unknown tool or skill: {e}")
    except (TypeError, ValueError) as e:
        return _err(rid, -32602, f"Bad arguments: {type(e).__name__}")
    import json

    return {"jsonrpc": "2.0", "id": rid, "result": {"content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False)}],
                                                    "isError": False}}


def _call(tenant, name: str, args: dict):
    if name == "ask_coach":
        from .agents import coach

        q = str(args["question"])[:2000]
        r = coach.answer(tenant, q, record=False)
        # external 2026 Playbooks entries [X#] stay a separate list (never course citations)
        return {"covered": r["covered"], "answer": r["answer"], "citations": r["citations"],
                "external": r.get("external", [])}
    if name == "search_library":
        hits = retrieval.search(tenant, str(args["query"])[:500], k=max(1, min(20, int(args.get("k", 5)))))
        return [{k: h[k] for k in ("id", "file", "lines", "title", "snippet")} for h in hits]
    if name == "list_skills":
        from .skills import SKILLS

        return [{"key": s.key, "name": s.name, "description": s.description, "needs": s.needs} for s in SKILLS.values()]
    if name == "run_skill":
        from .skills import SKILLS, get_profile

        sk = SKILLS[str(args["skill"])]
        return {"skill": sk.key, "draft": sk.fn(args.get("inputs") or {}, get_profile(tenant)), "queued": False,
                "note": "Read-only: copy the draft into the app to queue it for approval."}
    if name == "get_business_profile":
        from .skills import get_profile

        return get_profile(tenant)
    raise KeyError(name)
