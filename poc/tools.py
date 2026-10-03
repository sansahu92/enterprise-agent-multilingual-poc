"""Tool registry — the ONLY actions the agent can take.

Each tool declares: name, description, input schema, classification (READ / DRAFT / WRITE),
approval requirement and handler. The model proposes a tool + arguments; this module validates the
arguments, applies the user's entitlements (from identity, never from arguments) and executes.
There is deliberately no generic HTTP / SQL / URL tool.
"""
import json
from dataclasses import dataclass
from typing import Callable

import httpx
from jsonschema import Draft202012Validator

from poc import config
from poc.identity import User
from poc.telemetry import span

SERVICE_HEADERS = {"X-Service-Token": config.env("TOOL_SERVICE_TOKEN", "poc-tool-service")}


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    schema: dict
    classification: str          # READ | DRAFT | WRITE
    approval_required: bool
    required_permission: str
    handler: Callable


class ToolError(Exception):
    pass


# ───────────────────────── handlers
def _search_policies(args: dict, user: User, ctx: dict) -> dict:
    from poc.retrieval import search_policies
    from poc.safety import shield_prompt

    results = search_policies(args["query"], user)
    shield = shield_prompt(ctx.get("question", args["query"]), [r["content"] for r in results]) if results else \
        {"enabled": False, "doc_attacks": []}
    attacks = list(shield.get("doc_attacks") or [])
    attacks += [False] * (len(results) - len(attacks))
    kept, quarantined = [], []
    for r, attacked in zip(results, attacks):
        (quarantined if attacked else kept).append(r)
    ctx.setdefault("retrieved", []).extend(kept)
    ctx.setdefault("quarantined", []).extend(q["document_id"] for q in quarantined)
    if not kept:
        return {"evidence": "NO_APPROVED_EVIDENCE", "results": []}
    return {
        "evidence": "FOUND",
        "results": [{
            "doc_id": r["document_id"], "title": r["title"], "section": r["section"], "version": r["version"],
            "effective_date": r["effective_date"], "language": r["language"], "source_url": r["source_url"],
            "text": r["content"],
        } for r in kept],
    }


def _get_incident_status(args: dict, user: User, ctx: dict) -> dict:
    number = args["incident_number"].upper()
    try:
        r = httpx.get(f"{config.APPROVAL_API_URL}/servicenow/incidents/{number}", timeout=8)
    except httpx.HTTPError:
        return {"error": "SERVICE_UNAVAILABLE", "message": "ServiceNow could not be reached."}
    if r.status_code == 503:
        return {"error": "SERVICE_UNAVAILABLE", "message": "ServiceNow is currently unavailable."}
    if r.status_code == 404:
        return {"error": "NOT_FOUND_OR_NOT_PERMITTED"}
    r.raise_for_status()
    inc = r.json()
    # Entitlement: only the requester may see their incident (production: requester / assignee / group check).
    if inc.get("requester_id") != user.user_id:
        return {"error": "NOT_FOUND_OR_NOT_PERMITTED"}   # do not reveal that the record exists
    return {k: inc[k] for k in ("number", "short_description", "state", "assignment_group", "updated")}


def _draft_purchase_request(args: dict, user: User, ctx: dict) -> dict:
    return {
        "type": "PURCHASE_REQUEST_DRAFT",
        "submitted": False,
        "next_step": "Review the draft and submit it yourself in SAP. The assistant does not submit purchase requests in Release 1.",
        "draft": {**args, "requester_id": user.user_id},
    }


def _create_access_request(args: dict, user: User, ctx: dict) -> dict:
    payload = {**args, "requester_id": user.user_id, "subject_user_id": user.user_id}  # identity from token, not args
    r = httpx.post(f"{config.APPROVAL_API_URL}/approvals", headers=SERVICE_HEADERS, timeout=8,
                   json={"tool": "create_access_request", "requester_id": user.user_id, "payload": payload})
    if r.status_code >= 500:
        return {"error": "APPROVAL_SERVICE_UNAVAILABLE"}
    r.raise_for_status()
    a = r.json()
    ctx.setdefault("approvals", []).append(a["id"])
    return {
        "approval_id": a["id"], "state": a["state"], "submitted": False,
        "message": "Draft created and waiting for the employee's approval in the approval panel. It has NOT been submitted.",
    }


# ───────────────────────── registry
REGISTRY: dict[str, ToolSpec] = {t.name: t for t in [
    ToolSpec(
        name="search_policies",
        description="Search approved Authority policies, procedures and forms (HR, Finance, Procurement, IT). "
                    "Use for any policy or procedure question. Results are already limited to what the user may access.",
        schema={"type": "object", "additionalProperties": False, "required": ["query"],
                "properties": {"query": {"type": "string", "minLength": 2, "maxLength": 400,
                                         "description": "Search query in the user's language; include key terms in English and Arabic when helpful."}}},
        classification="READ", approval_required=False, required_permission="any employee (ACL-filtered)",
        handler=_search_policies),
    ToolSpec(
        name="get_incident_status",
        description="Get the current status of the user's own ServiceNow incident by incident number (e.g. INC001234).",
        schema={"type": "object", "additionalProperties": False, "required": ["incident_number"],
                "properties": {"incident_number": {"type": "string", "pattern": "^[Ii][Nn][Cc]\\d{6,7}$"}}},
        classification="READ", approval_required=False, required_permission="requester of the incident",
        handler=_get_incident_status),
    ToolSpec(
        name="draft_purchase_request",
        description="Prepare a structured purchase request draft for the user to submit in SAP. Never submits anything.",
        schema={"type": "object", "additionalProperties": False,
                "required": ["description", "quantity", "estimated_cost_aed", "cost_centre", "business_justification"],
                "properties": {
                    "description": {"type": "string", "maxLength": 300},
                    "quantity": {"type": "integer", "minimum": 1, "maximum": 10000},
                    "estimated_cost_aed": {"type": "number", "minimum": 0},
                    "cost_centre": {"type": "string", "pattern": "^[A-Z0-9\\-]{3,20}$"},
                    "preferred_supplier": {"type": "string", "maxLength": 120},
                    "business_justification": {"type": "string", "maxLength": 500}}},
        classification="DRAFT", approval_required=False, required_permission="any employee",
        handler=_draft_purchase_request),
    ToolSpec(
        name="create_access_request",
        description="Prepare a request for the user's OWN access to a business application. Creates a draft that the "
                    "user must approve in the approval panel; it is never submitted by this tool. Ask the user for any "
                    "missing field before calling.",
        schema={"type": "object", "additionalProperties": False,
                "required": ["application", "role", "business_justification", "start_date"],
                "properties": {
                    "application": {"type": "string", "enum": ["SAP_S4", "D365_FIN"]},
                    "role": {"type": "string", "minLength": 2, "maxLength": 64},
                    "business_justification": {"type": "string", "minLength": 10, "maxLength": 500},
                    "start_date": {"type": "string", "pattern": "^\\d{4}-\\d{2}-\\d{2}$"}}},
        classification="WRITE", approval_required=True, required_permission="self — requester is the access subject",
        handler=_create_access_request),
]}


def openai_tool_definitions() -> list[dict]:
    return [{"type": "function", "function": {"name": t.name, "description": t.description, "parameters": t.schema}}
            for t in REGISTRY.values()]


def validate_args(name: str, raw_args: str | dict) -> dict:
    if name not in REGISTRY:
        raise ToolError(f"Unknown tool '{name}' — not in registry")
    try:
        args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
    except json.JSONDecodeError as e:
        raise ToolError(f"Arguments are not valid JSON: {e}")
    errors = sorted(Draft202012Validator(REGISTRY[name].schema).iter_errors(args), key=lambda e: e.path)
    if errors:
        raise ToolError("; ".join(e.message for e in errors[:3]))
    return args


def execute_tool(name: str, raw_args, user: User, ctx: dict) -> dict:
    """Validate → authorise → execute. Returns a JSON-serialisable result (errors are returned, not raised)."""
    with span(f"tool.{name}", {"tool": name, "user": user.pseudonym,
                               "classification": REGISTRY[name].classification if name in REGISTRY else "UNKNOWN"}) as sp:
        try:
            args = validate_args(name, raw_args)
        except ToolError as e:
            sp.set_attribute("status", "rejected")
            ctx.setdefault("tool_calls", []).append({"tool": name, "status": "rejected", "error": str(e)})
            return {"error": "INVALID_TOOL_CALL", "detail": str(e)}
        result = REGISTRY[name].handler(args, user, ctx)
        status = "error" if isinstance(result, dict) and result.get("error") else "ok"
        sp.set_attribute("status", status)
        ctx.setdefault("tool_calls", []).append({"tool": name, "status": status, "args": args,
                                                 "error": result.get("error") if status == "error" else None})
        return result
