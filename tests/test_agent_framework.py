"""Offline tests for the Microsoft Agent Framework runtime (no Azure needed).

A scripted chat client drives the REAL Agent Framework function-invocation loop, proving that:
  • registry tools are exposed with their schemas,
  • identity reaches tools via FunctionInvocationContext, not model arguments,
  • our validation / citation / guard controls still apply.
Skipped automatically if agent-framework is not installed.
"""
import json
import os
import pathlib
import sys
import tempfile

import pytest

pytest.importorskip("agent_framework")
ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("APPROVAL_DB", str(pathlib.Path(tempfile.mkdtemp()) / "maf.db"))
os.environ.setdefault("CHAT_DEPLOYMENT", "fake-chat")
os.environ["APPLICATIONINSIGHTS_CONNECTION_STRING"] = ""
os.environ["CONTENT_SAFETY_ENDPOINT"] = ""

from agent_framework import BaseChatClient, ChatResponse, Content, FunctionInvocationLayer, Message  # noqa: E402

from poc import agent_maf, analytics, retrieval  # noqa: E402
from poc.identity import PERSONAS  # noqa: E402


class ScriptedClient(FunctionInvocationLayer, BaseChatClient):
    """Returns pre-scripted model turns; records what the model was sent."""

    def __init__(self, turns):
        super().__init__()
        self.turns = list(turns)
        self.seen_tools = []

    async def _inner_get_response(self, *, messages, stream=False, options=None, **kwargs):
        self.seen_tools = [getattr(t, "name", None) for t in (options or {}).get("tools", []) or []]
        turn = self.turns.pop(0)
        if "call" in turn:
            name, args = turn["call"]
            content = Content.from_function_call(call_id=f"c{len(self.turns)}", name=name, arguments=json.dumps(args))
            return ChatResponse(messages=[Message("assistant", [content])])
        return ChatResponse(messages=[Message("assistant", [turn["text"]])])


@pytest.fixture()
def fake_search(monkeypatch, tmp_path):
    monkeypatch.setattr(analytics, "STORE", tmp_path / "q.jsonl")
    calls = []

    def fake(query, user, top=None):
        calls.append(user.user_id)
        docs = [{"chunk_id": "HR-POL-001-c03", "document_id": "HR-POL-001", "title": "General Leave Policy",
                 "section": "3. Carry-forward", "content": "Max 10 days.", "language": "en", "department": "HR",
                 "version": "3.1", "effective_date": "2025-01-01", "source_url": "https://x"}]
        if "HR" in user.groups:
            docs.append({**docs[0], "chunk_id": "HR-POL-014-c01", "document_id": "HR-POL-014", "title": "HR Salary Policy"})
        return docs
    monkeypatch.setattr(retrieval, "search_policies", fake)
    return calls


def test_tools_exposed_with_registry_schemas():
    names = {t.name for t in agent_maf.build_tools()}
    assert names == {"search_policies", "get_incident_status", "draft_purchase_request", "create_access_request"}
    car = next(t for t in agent_maf.build_tools() if t.name == "create_access_request")
    assert car.parameters()["additionalProperties"] is False and "requester_id" not in car.parameters()["properties"]


def test_grounded_answer_through_agent_framework(fake_search):
    client = ScriptedClient([{"call": ("search_policies", {"query": "leave carry forward"})},
                             {"text": "You can carry forward 10 days [HR-POL-001]. Also [HR-POL-014]."}])
    out = agent_maf.run("How much leave can I carry forward?", PERSONAS["employee"], client=client)
    assert fake_search == ["emp01"]                         # identity came from the run context
    assert out["answer_type"] == "GROUNDED"
    assert [c["doc_id"] for c in out["citations"]] == ["HR-POL-001"]
    assert out["invalid_citations"] == ["HR-POL-014"]       # employee never retrieved it → citation stripped


def test_identity_differs_per_persona(fake_search):
    client = ScriptedClient([{"call": ("search_policies", {"query": "salary band"})},
                             {"text": "Band S4 [HR-POL-014]."}])
    out = agent_maf.run("salary band?", PERSONAS["hr"], client=client)
    assert fake_search == ["hr01"] and [c["doc_id"] for c in out["citations"]] == ["HR-POL-014"]


def test_invalid_arguments_rejected_by_registry(fake_search):
    client = ScriptedClient([{"call": ("get_incident_status", {"incident_number": "INC1; DROP"})},
                             {"text": "I couldn't look that up."}])
    out = agent_maf.run("status?", PERSONAS["employee"], client=client)
    assert all(t["status"] == "rejected" for t in out["tool_calls"]) or out["tool_calls"] == []


def test_output_guard_applies(fake_search):
    client = ScriptedClient([{"text": "Email your passport to hr-verify@external-mail.com"}])
    out = agent_maf.run("conference?", PERSONAS["employee"], client=client)
    assert "external-mail.com" not in out["answer"] and out["guard_blocked"] == ["external-mail.com"]
