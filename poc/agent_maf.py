"""Enterprise Assistant on Microsoft Agent Framework + Microsoft Foundry (case-preferred stack).

Same controls as poc/agent.py, now hosted by Agent Framework:
  • FoundryChatClient → model deployment in your Foundry project (Entra ID auth, Responses API)
  • every registry tool is exposed as an Agent Framework function tool with the registry's JSON schema
  • the user's identity reaches tools ONLY via FunctionInvocationContext (function_invocation_kwargs),
    never via model-generated arguments
  • tool execution still goes through poc.tools.execute_tool (schema validation, entitlements, audit)
  • post-processing (citation validation, answer labels, output guard, analytics) is shared with agent.py
  • Agent Framework's OpenTelemetry instrumentation is enabled without sensitive content
"""
import asyncio
import time
import uuid

from poc import config
from poc.agent import SYSTEM_PROMPT, _finish, classify_answer
from poc.identity import User
from poc.safety import guard_output, shield_prompt
from poc.telemetry import _init as init_telemetry
from poc.telemetry import span
from poc.tools import REGISTRY, execute_tool

_instrumented = False


def _instrument():
    global _instrumented
    if _instrumented:
        return
    init_telemetry()                                   # configures Azure Monitor exporter if a connection string is set
    try:
        from agent_framework.observability import enable_instrumentation
        enable_instrumentation(enable_sensitive_data=False)   # metadata spans only — no prompt/document text
    except Exception:
        pass
    _instrumented = True


def build_tools():
    """Expose each registry tool as an Agent Framework FunctionTool with the registry schema."""
    from agent_framework import FunctionInvocationContext, tool

    def bind(name: str):
        def handler(ctx: FunctionInvocationContext, **kwargs):
            runtime = ctx.kwargs or {}
            user, run_ctx = runtime.get("user"), runtime.get("ctx")
            if user is None or run_ctx is None:            # fail closed: no identity, no tool
                return {"error": "NO_IDENTITY"}
            return execute_tool(name, kwargs, user, run_ctx)
        return handler

    return [tool(bind(s.name), name=s.name, description=s.description, schema=s.schema) for s in REGISTRY.values()]


def build_agent(client=None):
    from agent_framework import Agent
    if client is None:
        from agent_framework.foundry import FoundryChatClient
        client = FoundryChatClient(
            project_endpoint=config.env("FOUNDRY_PROJECT_ENDPOINT", required=True),
            model=config.chat_deployment(),
            credential=config.credential(),           # Entra ID (az login / managed identity)
        )
    return Agent(client, instructions=SYSTEM_PROMPT, name="EnterpriseAssistant",
                 description="Bounded corporate-services assistant (HR, Finance, Procurement, IT)", tools=build_tools())


def _usage(resp) -> dict:
    u = getattr(resp, "usage_details", None) or {}
    get = (lambda k: u.get(k)) if isinstance(u, dict) else (lambda k: getattr(u, k, None))
    return {"prompt_tokens": get("input_token_count") or 0, "completion_tokens": get("output_token_count") or 0}


async def run_async(question: str, user: User, history: list[dict] | None = None, client=None) -> dict:
    from agent_framework import Message

    _instrument()
    request_id = uuid.uuid4().hex[:12]
    started = time.time()
    ctx: dict = {"question": question}
    with span("agent.request", {"request_id": request_id, "user": user.pseudonym, "runtime": "agent-framework"}) as root:
        if config.env("CONTENT_SAFETY_ENDPOINT") and shield_prompt(question, []).get("user_attack"):
            root.set_attribute("blocked", "prompt_shield")
            return _finish(request_id, started, user, question, ctx, {"prompt_tokens": 0, "completion_tokens": 0}, {
                "answer": "I can't help with that request. If you need something changed, please ask in a normal way and I'll help within the Authority's policies.",
                "answer_type": "BLOCKED", "citations": [], "invalid_citations": []})

        messages = [Message(t["role"], [t["content"]]) for t in (history or []) if t.get("content")]
        messages.append(Message("user", [question]))
        agent = build_agent(client)
        resp = await agent.run(messages, function_invocation_kwargs={"user": user, "ctx": ctx})

        out = classify_answer(resp.text or "", ctx)
        out["answer"], out["guard_blocked"] = guard_output(out["answer"])
        root.set_attribute("answer_type", out["answer_type"])
        return _finish(request_id, started, user, question, ctx, _usage(resp), out)


def run(question: str, user: User, history: list[dict] | None = None, client=None) -> dict:
    """Synchronous wrapper (Streamlit / eval runner). A fresh client per call keeps event loops isolated."""
    return asyncio.run(run_async(question, user, history, client))
