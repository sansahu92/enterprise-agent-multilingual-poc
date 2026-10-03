"""Demo UI.  Run:  streamlit run poc/ui.py

Shows: persona switch (simulated Entra identity), answer type labels, citations, the approval panel
(the only place a write can be approved) and a 'behind the scenes' view of retrieval and tool calls.
"""
import pathlib
import sys

import httpx
import streamlit as st

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from poc import agent, config  # noqa: E402
from poc.identity import PERSONAS  # noqa: E402

API = config.APPROVAL_API_URL
SVC = {"X-Service-Token": config.env("TOOL_SERVICE_TOKEN", "poc-tool-service")}
LABELS = {
    "GROUNDED": ("✅ Grounded in approved policy", "green"),
    "SYSTEM_DATA": ("🔎 Live data from enterprise system", "blue"),
    "ACTION_DRAFT": ("📝 Draft created — needs your approval", "orange"),
    "NO_APPROVED_EVIDENCE": ("⚠️ No approved guidance found", "orange"),
    "GENERAL_GUIDANCE": ("ℹ️ General guidance — not Authority policy", "gray"),
    "BLOCKED": ("⛔ Request blocked by safety controls", "red"),
}

st.set_page_config(page_title="Enterprise Assistant — PoC", page_icon="🏛️", layout="wide")

# ───────────────────────── sidebar: identity + demo controls
with st.sidebar:
    st.header("Signed in as")
    persona = st.selectbox("Persona (simulates Entra ID)", list(PERSONAS), format_func=lambda p: PERSONAS[p].display_name)
    user = PERSONAS[persona]
    st.caption("Token groups: " + ", ".join(user.groups))
    if st.session_state.get("persona") != persona:
        st.session_state.persona = persona
        st.session_state.history = []
    st.divider()
    st.subheader("Demo controls")
    try:
        down = httpx.get(f"{API}/health", timeout=3).json().get("servicenow_down", False)
        new = st.toggle("Simulate ServiceNow outage", value=down)
        if new != down:
            httpx.post(f"{API}/admin/servicenow", params={"down": new}, timeout=3)
    except httpx.HTTPError:
        st.error("Approval API not reachable — start it with: uvicorn poc.approval_api:app --port 8000")
    if st.button("Clear conversation"):
        st.session_state.history = []
    st.caption("PoC sandbox — validates architecture assumptions. Not production.")

st.title("🏛️ Enterprise Assistant — PoC")
left, right = st.columns([2, 1])

# ───────────────────────── chat
with left:
    for turn in st.session_state.history:
        with st.chat_message(turn["role"]):
            if turn["role"] == "assistant":
                label, colour = LABELS.get(turn["meta"]["answer_type"], ("", "gray"))
                st.markdown(f":{colour}[**{label}**]")
            st.markdown(turn["content"])
            if turn["role"] == "assistant":
                meta = turn["meta"]
                for c in meta["citations"]:
                    st.caption(f"📄 {c['doc_id']} · {c['title']} · §{c['section']} · v{c['version']}")
                with st.expander("Behind the scenes"):
                    st.json({k: meta[k] for k in ("request_id", "retrieved_doc_ids", "quarantined_doc_ids", "tool_calls",
                                                  "invalid_citations", "guard_blocked", "usage", "latency_ms")})

    question = st.chat_input("Ask about a policy, a ticket, or request access… (English or العربية)")
    if question:
        history = [{"role": t["role"], "content": t["content"]} for t in st.session_state.history][-8:]
        with st.spinner("Thinking…"):
            result = agent.run(question, user, history=history)
        st.session_state.history += [{"role": "user", "content": question},
                                     {"role": "assistant", "content": result["answer"], "meta": result}]
        st.rerun()

# ───────────────────────── approval panel (the only place a write can be approved)
with right:
    st.subheader("Approval panel")
    st.caption("Writes execute only after you approve the exact payload below. The assistant cannot approve for you.")
    try:
        pending = httpx.get(f"{API}/approvals", params={"requester_id": user.user_id}, timeout=5).json()
    except httpx.HTTPError:
        pending = []
    if not pending:
        st.info("No requests.")
    for a in pending:
        with st.container(border=True):
            st.markdown(f"**{a['id']}** · `{a['state']}`")
            st.json(a["payload"])
            st.caption(f"payload hash {a['payload_hash'][:16]}…")
            if a["state"] == "PENDING_APPROVAL":
                c1, c2 = st.columns(2)
                if c1.button("Approve & submit", key="ok" + a["id"], type="primary"):
                    r = httpx.post(f"{API}/approvals/{a['id']}/approve", headers={"X-User-Id": user.user_id},
                                   json={"payload_hash": a["payload_hash"]}, timeout=5)
                    if r.status_code == 200:
                        e = httpx.post(f"{API}/approvals/{a['id']}/execute", headers=SVC, timeout=10)
                        st.toast("Submitted" if e.status_code == 200 else f"Execution failed: {e.text}")
                    else:
                        st.toast(f"Approval refused: {r.text}")
                    st.rerun()
                if c2.button("Reject", key="no" + a["id"]):
                    httpx.post(f"{API}/approvals/{a['id']}/reject", headers={"X-User-Id": user.user_id}, timeout=5)
                    st.rerun()
            elif a["state"] == "COMPLETED" and a.get("result"):
                st.success(f"ServiceNow request {a['result'].get('servicenow_request')} created")
            elif a["state"] == "FAILED":
                st.error("Execution failed — nothing was created. Try again later.")
