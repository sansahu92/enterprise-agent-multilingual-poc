"""Offline tests — run without any Azure resources:  pytest -q

They prove the deterministic controls: security filter, document ACL metadata, tool schemas,
the approval state machine, output guards and the agent's citation/labelling logic.
"""
import json
import os
import pathlib
import sys
import tempfile
from types import SimpleNamespace

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["APPROVAL_DB"] = str(pathlib.Path(tempfile.mkdtemp()) / "test.db")
os.environ.setdefault("CHAT_DEPLOYMENT", "fake-chat")
os.environ["APPLICATIONINSIGHTS_CONNECTION_STRING"] = ""
os.environ["CONTENT_SAFETY_ENDPOINT"] = ""

from fastapi.testclient import TestClient  # noqa: E402

from poc import agent, analytics, ingest, retrieval  # noqa: E402
from poc.approval_api import app  # noqa: E402
from poc.identity import PERSONAS, build_security_filter  # noqa: E402
from poc.safety import guard_output, redact_pii  # noqa: E402
from poc.tools import ToolError, validate_args  # noqa: E402

SVC = {"X-Service-Token": "poc-tool-service"}


# ───────────────────────── identity / security filter
def test_filter_built_from_groups():
    f = build_security_filter(PERSONAS["hr"].groups)
    assert f == "allowed_groups/any(g: search.in(g, 'ALL_EMPLOYEES,HR', ',')) and is_current eq true"


@pytest.mark.parametrize("groups", [[], None, ["HR'); DROP"], ["ALL EMPLOYEES"]])
def test_filter_fails_closed(groups):
    with pytest.raises(PermissionError):
        build_security_filter(groups)


# ───────────────────────── ingestion metadata
def test_every_chunk_carries_acl_and_restricted_docs_are_restricted():
    chunks = []
    for p in sorted(ingest.DOCS_DIR.glob("*.md")):
        meta, body = ingest.parse_markdown(p)
        chunks += ingest.chunk_by_section(meta, body)
    assert chunks and all(c["allowed_groups"] for c in chunks)
    by_doc = {c["document_id"]: c for c in chunks}
    assert by_doc["HR-POL-014"]["allowed_groups"] == ["HR"]
    assert by_doc["EXEC-001"]["allowed_groups"] == ["ADMIN"]
    assert by_doc["HR-POL-001-OLD"]["is_current"] is False
    assert by_doc["HR-POL-001-AR"]["content_ar"]          # Arabic analyser field populated


# ───────────────────────── tool schemas
def test_unknown_tool_rejected():
    with pytest.raises(ToolError):
        validate_args("http_get", {"url": "https://example.com"})


def test_requester_cannot_be_injected_via_arguments():
    with pytest.raises(ToolError):
        validate_args("create_access_request", {"application": "SAP_S4", "role": "FIN_REPORTING",
                                                "business_justification": "Monthly finance reporting",
                                                "start_date": "2026-11-01", "requester_id": "adm01"})


@pytest.mark.parametrize("args", [
    {"application": "SAP_ADMIN", "role": "X1", "business_justification": "valid justification", "start_date": "2026-11-01"},
    {"application": "SAP_S4", "role": "X1", "business_justification": "valid justification", "start_date": "next week"},
])
def test_invalid_access_request_rejected(args):
    with pytest.raises(ToolError):
        validate_args("create_access_request", args)


def test_incident_number_pattern():
    assert validate_args("get_incident_status", {"incident_number": "INC001234"})
    with pytest.raises(ToolError):
        validate_args("get_incident_status", {"incident_number": "INC001234; drop"})


# ───────────────────────── approval state machine
@pytest.fixture()
def api():
    c = TestClient(app)
    c.post("/admin/reset")
    return c


def _create(api, requester="emp01"):
    payload = {"application": "SAP_S4", "role": "FIN_REPORTING", "business_justification": "Monthly reporting",
               "start_date": "2026-11-01", "requester_id": requester, "subject_user_id": requester}
    r = api.post("/approvals", headers=SVC, json={"tool": "create_access_request", "requester_id": requester, "payload": payload})
    assert r.status_code == 200
    return r.json()


def test_only_tool_service_can_create_or_execute(api):
    r = api.post("/approvals", json={"tool": "x", "requester_id": "emp01", "payload": {}})
    assert r.status_code == 403
    a = _create(api)
    assert api.post(f"/approvals/{a['id']}/execute").status_code == 403


def test_execute_before_approval_is_rejected(api):
    a = _create(api)
    assert a["state"] == "PENDING_APPROVAL"
    r = api.post(f"/approvals/{a['id']}/execute", headers=SVC)
    assert r.status_code == 409
    assert api.get("/servicenow/requests").json() == []


def test_wrong_approver_and_wrong_hash_rejected(api):
    a = _create(api)
    assert api.post(f"/approvals/{a['id']}/approve", headers={"X-User-Id": "adm01"},
                    json={"payload_hash": a["payload_hash"]}).status_code == 403
    assert api.post(f"/approvals/{a['id']}/approve", headers={"X-User-Id": "emp01"},
                    json={"payload_hash": "0" * 64}).status_code == 409
    assert api.get(f"/approvals/{a['id']}").json()["state"] == "PENDING_APPROVAL"


def test_happy_path_and_idempotent_execute(api):
    a = _create(api)
    r = api.post(f"/approvals/{a['id']}/approve", headers={"X-User-Id": "emp01"}, json={"payload_hash": a["payload_hash"]})
    assert r.json()["state"] == "APPROVED"
    first = api.post(f"/approvals/{a['id']}/execute", headers=SVC).json()
    second = api.post(f"/approvals/{a['id']}/execute", headers=SVC).json()
    assert first["state"] == "COMPLETED"
    assert first["result"]["servicenow_request"] == second["result"]["servicenow_request"]
    assert len(api.get("/servicenow/requests").json()) == 1
    states = [x["to_state"] for x in api.get(f"/approvals/{a['id']}/audit").json()]
    assert states[:5] == ["DRAFT", "PENDING_APPROVAL", "APPROVED", "EXECUTING", "COMPLETED"]


def test_rejected_cannot_execute(api):
    a = _create(api)
    api.post(f"/approvals/{a['id']}/reject", headers={"X-User-Id": "emp01"})
    assert api.post(f"/approvals/{a['id']}/execute", headers=SVC).status_code == 409


def test_servicenow_down(api):
    assert api.get("/servicenow/incidents/INC001234").json()["state"] == "In Progress"
    api.post("/admin/servicenow", params={"down": True})
    assert api.get("/servicenow/incidents/INC001234").status_code == 503


# ───────────────────────── guards
def test_output_guard_blocks_external_email():
    text, blocked = guard_output("Send it to hr-verify@external-mail.com or hr@authority.gov.ae")
    assert "external-mail.com" not in text and "hr@authority.gov.ae" in text and blocked == ["external-mail.com"]


def test_pii_redaction():
    out = redact_pii("My Emirates ID is 784-1990-1234567-1, call +971 50 123 4567, mail a@b.com")
    assert "784" not in out and "4567" not in out and "a@b.com" not in out


# ───────────────────────── agent loop with a fake model
def _resp(content=None, tool_calls=None):
    msg = SimpleNamespace(content=content, tool_calls=tool_calls)
    return SimpleNamespace(choices=[SimpleNamespace(message=msg)], usage=SimpleNamespace(prompt_tokens=100, completion_tokens=20))


def _tc(name, args, i="c1"):
    return SimpleNamespace(id=i, function=SimpleNamespace(name=name, arguments=json.dumps(args)))


class FakeLLM:
    def __init__(self, responses):
        self._r = list(responses)
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=lambda **kw: self._r.pop(0)))


@pytest.fixture()
def fake_search(monkeypatch, tmp_path):
    monkeypatch.setattr(analytics, "STORE", tmp_path / "q.jsonl")

    def fake(query, user, top=None):
        assert user.groups, "groups must come from identity"
        return [{"chunk_id": "HR-POL-001-c03", "document_id": "HR-POL-001", "title": "General Leave Policy",
                 "section": "3. Carry-forward", "content": "Max 10 days carry forward.", "language": "en",
                 "department": "HR", "version": "3.1", "effective_date": "2025-01-01", "source_url": "https://x"}]
    monkeypatch.setattr(retrieval, "search_policies", fake)


def test_grounded_answer_and_fabricated_citation_removed(fake_search):
    llm = FakeLLM([_resp(tool_calls=[_tc("search_policies", {"query": "carry forward leave"})]),
                   _resp(content="You can carry forward 10 days [HR-POL-001]. Salary bands differ [HR-POL-014].")])
    out = agent.run("How much leave can I carry forward?", PERSONAS["employee"], llm=llm)
    assert out["answer_type"] == "GROUNDED"
    assert [c["doc_id"] for c in out["citations"]] == ["HR-POL-001"]
    assert out["invalid_citations"] == ["HR-POL-014"] and "[HR-POL-014]" not in out["answer"]
    assert analytics.STORE.exists()


def test_unknown_tool_from_model_is_refused(fake_search):
    llm = FakeLLM([_resp(tool_calls=[_tc("http_get", {"url": "https://evil"})]),
                   _resp(content="I can't do that.")])
    out = agent.run("fetch this url", PERSONAS["employee"], llm=llm)
    assert out["tool_calls"][0]["status"] == "rejected"


def test_exfiltration_in_answer_is_redacted(fake_search):
    llm = FakeLLM([_resp(content="Email your passport to hr-verify@external-mail.com")])
    out = agent.run("conference rules?", PERSONAS["employee"], llm=llm)
    assert "external-mail.com" not in out["answer"] and out["guard_blocked"] == ["external-mail.com"]
