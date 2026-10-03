"""Approval service + mock enterprise APIs (ServiceNow) for the PoC.

Run:  uvicorn poc.approval_api:app --port 8000

State machine (server-side, the model has NO tool that changes state):
    DRAFT → PENDING_APPROVAL → APPROVED → EXECUTING → COMPLETED
                         ↘ REJECTED                ↘ FAILED

Guards:
  • approve: caller identity (X-User-Id, simulating the Entra token) must be the requester; payload hash must
    match the hash shown to the user; request not expired.
  • execute: only from APPROVED; stored payload re-hashed and compared with the approved hash; idempotent;
    only callable by the tool service (X-Service-Token).
  • the mock write API itself refuses any call without an EXECUTING approval whose hash matches.
"""
import datetime as dt
import hashlib
import json
import os
import pathlib
import sqlite3
import threading
import uuid

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

DB_PATH = os.getenv("APPROVAL_DB", str(pathlib.Path(__file__).resolve().parent.parent / "approvals.db"))
SERVICE_TOKEN = os.getenv("TOOL_SERVICE_TOKEN", "poc-tool-service")
APPROVAL_TTL_HOURS = 24
_lock = threading.Lock()
STATE = {"servicenow_down": False}

INCIDENTS = {
    "INC001234": {"number": "INC001234", "short_description": "VPN disconnects every 30 minutes",
                  "state": "In Progress", "assignment_group": "Network Team", "requester_id": "emp01",
                  "updated": "2026-09-30T10:15:00Z"},
    "INC004567": {"number": "INC004567", "short_description": "HR portal access error",
                  "state": "Resolved", "assignment_group": "Application Support", "requester_id": "hr01",
                  "updated": "2026-09-28T08:00:00Z"},
}

app = FastAPI(title="PoC approval service + mock enterprise APIs")


# ───────────────────────── storage
def db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS approvals(
            id TEXT PRIMARY KEY, tool TEXT, requester_id TEXT, payload TEXT, payload_hash TEXT,
            state TEXT, created_at TEXT, expires_at TEXT, approved_by TEXT, approved_at TEXT,
            approved_hash TEXT, result TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS audit(
            ts TEXT, approval_id TEXT, from_state TEXT, to_state TEXT, actor TEXT, detail TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS sn_requests(
            number TEXT PRIMARY KEY, approval_id TEXT UNIQUE, payload TEXT, created_at TEXT)""")


init_db()


def now():
    return dt.datetime.now(dt.timezone.utc)


def payload_hash(payload: dict) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def audit(c, approval_id, frm, to, actor, detail=""):
    c.execute("INSERT INTO audit VALUES (?,?,?,?,?,?)", (now().isoformat(), approval_id, frm, to, actor, detail))


def transition(c, approval_id, frm, to, actor, extra_sql="", extra_args=(), detail=""):
    """Atomic conditional transition: succeeds only if the record is still in state `frm`."""
    cur = c.execute(f"UPDATE approvals SET state=?{extra_sql} WHERE id=? AND state=?",
                    (to, *extra_args, approval_id, frm))
    if cur.rowcount != 1:
        raise HTTPException(409, f"Transition {frm}→{to} not allowed from current state")
    audit(c, approval_id, frm, to, actor, detail)


def get_row(c, approval_id):
    row = c.execute("SELECT * FROM approvals WHERE id=?", (approval_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Approval not found")
    return row


def public(row) -> dict:
    d = dict(row)
    d["payload"] = json.loads(d["payload"])
    d["result"] = json.loads(d["result"]) if d["result"] else None
    return d


# ───────────────────────── admin (demo controls)
@app.get("/health")
def health():
    return {"ok": True, "servicenow_down": STATE["servicenow_down"]}


@app.post("/admin/servicenow")
def set_servicenow(down: bool):
    STATE["servicenow_down"] = down
    return {"servicenow_down": down}


@app.post("/admin/reset")
def reset():
    with db() as c:
        c.execute("DELETE FROM approvals"); c.execute("DELETE FROM audit"); c.execute("DELETE FROM sn_requests")
    STATE["servicenow_down"] = False
    return {"reset": True}


# ───────────────────────── mock ServiceNow (read)
@app.get("/servicenow/incidents/{number}")
def get_incident(number: str):
    if STATE["servicenow_down"]:
        raise HTTPException(503, "ServiceNow unavailable")
    inc = INCIDENTS.get(number.upper())
    if not inc:
        raise HTTPException(404, "Not found")
    return inc


@app.get("/servicenow/requests")
def list_sn_requests():
    with db() as c:
        return [dict(r) for r in c.execute("SELECT * FROM sn_requests ORDER BY created_at")]


# ───────────────────────── approval service
class CreateApproval(BaseModel):
    tool: str
    requester_id: str
    payload: dict


class Decision(BaseModel):
    payload_hash: str


def require_service(token: str | None):
    if token != SERVICE_TOKEN:
        raise HTTPException(403, "Only the tool execution service may call this endpoint")


@app.post("/approvals")
def create_approval(body: CreateApproval, x_service_token: str | None = Header(default=None)):
    """Called by the tool service after schema + entitlement validation. Creates DRAFT then submits it."""
    require_service(x_service_token)
    aid = "APR-" + uuid.uuid4().hex[:8].upper()
    h = payload_hash(body.payload)
    with _lock, db() as c:
        c.execute("INSERT INTO approvals VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                  (aid, body.tool, body.requester_id, json.dumps(body.payload, ensure_ascii=False), h, "DRAFT",
                   now().isoformat(), (now() + dt.timedelta(hours=APPROVAL_TTL_HOURS)).isoformat(),
                   None, None, None, None))
        audit(c, aid, None, "DRAFT", "tool-service")
        transition(c, aid, "DRAFT", "PENDING_APPROVAL", "tool-service")
        return public(get_row(c, aid))


@app.get("/approvals")
def list_approvals(requester_id: str | None = None, state: str | None = None):
    q, args = "SELECT * FROM approvals WHERE 1=1", []
    if requester_id:
        q += " AND requester_id=?"; args.append(requester_id)
    if state:
        q += " AND state=?"; args.append(state)
    with db() as c:
        return [public(r) for r in c.execute(q + " ORDER BY created_at DESC", args)]


@app.get("/approvals/{approval_id}")
def read_approval(approval_id: str):
    with db() as c:
        return public(get_row(c, approval_id))


@app.get("/approvals/{approval_id}/audit")
def read_audit(approval_id: str):
    with db() as c:
        return [dict(r) for r in c.execute("SELECT * FROM audit WHERE approval_id=? ORDER BY ts", (approval_id,))]


@app.post("/approvals/{approval_id}/approve")
def approve(approval_id: str, body: Decision, x_user_id: str = Header(...)):
    """Called from the UI by the authenticated user (X-User-Id simulates the validated Entra identity)."""
    with _lock, db() as c:
        row = get_row(c, approval_id)
        if x_user_id != row["requester_id"]:
            audit(c, approval_id, row["state"], row["state"], x_user_id, "DENIED: approver not authorised")
            raise HTTPException(403, "Approver is not authorised for this request")
        if body.payload_hash != row["payload_hash"]:
            audit(c, approval_id, row["state"], row["state"], x_user_id, "DENIED: payload hash mismatch")
            raise HTTPException(409, "Payload changed since it was displayed — review again")
        if dt.datetime.fromisoformat(row["expires_at"]) < now():
            transition(c, approval_id, "PENDING_APPROVAL", "REJECTED", "system", detail="expired")
            raise HTTPException(409, "Approval request expired")
        transition(c, approval_id, "PENDING_APPROVAL", "APPROVED", x_user_id,
                   extra_sql=", approved_by=?, approved_at=?, approved_hash=?",
                   extra_args=(x_user_id, now().isoformat(), row["payload_hash"]))
        return public(get_row(c, approval_id))


@app.post("/approvals/{approval_id}/reject")
def reject(approval_id: str, x_user_id: str = Header(...)):
    with _lock, db() as c:
        row = get_row(c, approval_id)
        if x_user_id != row["requester_id"]:
            raise HTTPException(403, "Not authorised")
        transition(c, approval_id, "PENDING_APPROVAL", "REJECTED", x_user_id)
        return public(get_row(c, approval_id))


@app.post("/approvals/{approval_id}/execute")
def execute(approval_id: str, x_service_token: str | None = Header(default=None)):
    """Tool service executes an APPROVED request. Idempotent: a completed request returns its result."""
    require_service(x_service_token)
    with _lock, db() as c:
        row = get_row(c, approval_id)
        if row["state"] == "COMPLETED":
            return public(row)                                   # idempotent replay, no duplicate write
        if row["state"] != "APPROVED":
            audit(c, approval_id, row["state"], row["state"], "tool-service", "DENIED: execute before approval")
            raise HTTPException(409, f"Cannot execute from state {row['state']}")
        payload = json.loads(row["payload"])
        if payload_hash(payload) != row["approved_hash"]:
            audit(c, approval_id, "APPROVED", "APPROVED", "tool-service", "DENIED: payload changed after approval")
            raise HTTPException(409, "Payload does not match the approved payload")
        transition(c, approval_id, "APPROVED", "EXECUTING", "tool-service")
        try:
            result = _mock_servicenow_create(c, approval_id, payload)
            transition(c, approval_id, "EXECUTING", "COMPLETED", "tool-service",
                       extra_sql=", result=?", extra_args=(json.dumps(result),))
        except HTTPException as e:
            transition(c, approval_id, "EXECUTING", "FAILED", "tool-service",
                       extra_sql=", result=?", extra_args=(json.dumps({"error": e.detail}),))
            raise
        return public(get_row(c, approval_id))


def _mock_servicenow_create(c, approval_id: str, payload: dict) -> dict:
    """Mock ServiceNow catalog write. Refuses anything not in EXECUTING with a matching approved hash."""
    if STATE["servicenow_down"]:
        raise HTTPException(503, "ServiceNow unavailable")
    row = get_row(c, approval_id)
    if row["state"] != "EXECUTING" or payload_hash(payload) != row["approved_hash"]:
        raise HTTPException(403, "Write API: no valid approval")
    existing = c.execute("SELECT number FROM sn_requests WHERE approval_id=?", (approval_id,)).fetchone()
    if existing:
        return {"servicenow_request": existing["number"]}
    number = "RITM" + uuid.uuid4().hex[:7].upper()
    c.execute("INSERT INTO sn_requests VALUES (?,?,?,?)",
              (number, approval_id, json.dumps(payload, ensure_ascii=False), now().isoformat()))
    return {"servicenow_request": number, "note": "Line-manager and application-owner approval continues in ServiceNow"}
