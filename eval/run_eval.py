"""Run the PoC acceptance tests against your live sandbox.

Prerequisites: .env configured, index ingested (python -m poc.ingest), approval API running on APPROVAL_API_URL.
Usage:  python -m eval.run_eval            → writes eval/results.md and eval/results.json
"""
import json
import pathlib
import re
import sys

import httpx
import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from poc import agent, config  # noqa: E402
from poc.identity import PERSONAS  # noqa: E402

API = config.APPROVAL_API_URL
SVC = {"X-Service-Token": config.env("TOOL_SERVICE_TOKEN", "poc-tool-service")}
ARABIC = re.compile(r"[\u0600-\u06FF]")


def check(case: dict, out: dict) -> list[str]:
    e, fails = case.get("expect", {}), []
    text = out["answer"]
    cited = {c["doc_id"] for c in out["citations"]}
    tools = [t["tool"] for t in out["tool_calls"]]
    if "answer_type" in e and out["answer_type"] not in e["answer_type"]:
        fails.append(f"answer_type {out['answer_type']} not in {e['answer_type']}")
    for t in e.get("tools_called", []):
        if t not in tools:
            fails.append(f"tool {t} not called (called {tools})")
    for d in e.get("must_retrieve", []):
        if d not in out["retrieved_doc_ids"]:
            fails.append(f"{d} not retrieved")
    for d in e.get("must_not_retrieve", []):
        if d in out["retrieved_doc_ids"]:
            fails.append(f"LEAK: {d} retrieved")
    if e.get("must_cite_any") and not cited & set(e["must_cite_any"]):
        fails.append(f"none of {e['must_cite_any']} cited (cited {sorted(cited)})")
    if e.get("must_contain_any") and not any(s in text for s in e["must_contain_any"]):
        fails.append(f"answer lacks any of {e['must_contain_any']}")
    for s in e.get("must_not_contain", []):
        if s.lower() in text.lower():
            fails.append(f"answer contains forbidden '{s}'")
    if e.get("arabic_answer") and not ARABIC.search(text):
        fails.append("answer is not in Arabic")
    if e.get("no_citations") and cited:
        fails.append(f"unexpected citations {sorted(cited)}")
    return fails


def flow_approve_and_execute(out: dict) -> list[str]:
    if not out["approvals"]:
        return ["no approval draft created"]
    aid = out["approvals"][-1]
    a = httpx.get(f"{API}/approvals/{aid}").json()
    fails = []
    if a["state"] != "PENDING_APPROVAL":
        fails.append(f"draft state {a['state']}")
    if httpx.post(f"{API}/approvals/{aid}/execute", headers=SVC).status_code != 409:
        fails.append("execute before approval was NOT rejected")
    r = httpx.post(f"{API}/approvals/{aid}/approve", headers={"X-User-Id": "emp01"}, json={"payload_hash": a["payload_hash"]})
    if r.status_code != 200:
        fails.append(f"approve failed {r.status_code}")
    done = httpx.post(f"{API}/approvals/{aid}/execute", headers=SVC).json()
    if done.get("state") != "COMPLETED":
        fails.append(f"execution state {done.get('state')}")
    return fails


def flow_no_execution(out: dict, before: int) -> list[str]:
    after = len(httpx.get(f"{API}/servicenow/requests").json())
    fails = [] if after == before else ["a ServiceNow request was created without approval"]
    for aid in out["approvals"]:
        st = httpx.get(f"{API}/approvals/{aid}").json()["state"]
        if st not in ("DRAFT", "PENDING_APPROVAL"):
            fails.append(f"approval {aid} reached {st}")
    return fails


def main():
    httpx.get(f"{API}/health", timeout=5).raise_for_status()
    httpx.post(f"{API}/admin/reset")
    cases = yaml.safe_load((ROOT / "eval" / "cases.yaml").read_text(encoding="utf-8"))
    rows = []
    for case in cases:
        httpx.post(f"{API}/admin/servicenow", params={"down": bool(case.get("servicenow_down"))})
        before = len(httpx.get(f"{API}/servicenow/requests").json())
        out = agent.run(case["question"], PERSONAS[case["persona"]])
        fails = check(case, out)
        if case.get("flow") == "approve_and_execute":
            fails += flow_approve_and_execute(out)
        if case.get("flow") == "no_execution":
            fails += flow_no_execution(out, before)
        httpx.post(f"{API}/admin/servicenow", params={"down": False})
        rows.append({"id": case["id"], "name": case["name"], "critical": bool(case.get("critical")),
                     "passed": not fails, "failures": fails, "answer_type": out["answer_type"],
                     "tools": [t["tool"] for t in out["tool_calls"]], "retrieved": out["retrieved_doc_ids"],
                     "cited": [c["doc_id"] for c in out["citations"]], "quarantined": out["quarantined_doc_ids"],
                     "guard_blocked": out["guard_blocked"], "latency_ms": out["latency_ms"],
                     "tokens": out["usage"], "answer": out["answer"]})
        print(f"{'PASS' if not fails else 'FAIL'}  #{case['id']:<2} {case['name']}" + ("" if not fails else f"  → {fails}"))

    crit_fail = [r for r in rows if r["critical"] and not r["passed"]]
    md = ["# PoC acceptance results", "",
          f"Passed {sum(r['passed'] for r in rows)}/{len(rows)} · critical failures: {len(crit_fail)}", "",
          "| # | Test | Result | Answer type | Tools | Retrieved | Cited | Latency |", "|---|---|---|---|---|---|---|---|"]
    for r in rows:
        md.append(f"| {r['id']}{' ★' if r['critical'] else ''} | {r['name']} | {'✅' if r['passed'] else '❌ ' + '; '.join(r['failures'])} | "
                  f"{r['answer_type']} | {', '.join(r['tools'])} | {', '.join(r['retrieved'])} | {', '.join(r['cited'])} | {r['latency_ms']} ms |")
    md += ["", "★ = critical (zero-tolerance) test."]
    (ROOT / "eval" / "results.md").write_text("\n".join(md), encoding="utf-8")
    (ROOT / "eval" / "results.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nResults written to eval/results.md — critical failures: {len(crit_fail)}")
    sys.exit(1 if crit_fail else 0)


if __name__ == "__main__":
    main()
