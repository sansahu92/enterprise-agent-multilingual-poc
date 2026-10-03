"""Query analytics store (PoC: JSON-lines file; production: governed store feeding Power BI).

Every query is logged, PII-redacted and pseudonymised. Full answers and document text are not stored.
"""
import datetime
import json
import pathlib

from poc.safety import redact_pii

STORE = pathlib.Path(__file__).resolve().parent.parent / "analytics" / "queries.jsonl"


def log_query(user, request_id: str, question: str, result: dict) -> None:
    STORE.parent.mkdir(exist_ok=True)
    record = {
        "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "request_id": request_id,
        "user": user.pseudonym,
        "groups": list(user.groups),
        "query_redacted": redact_pii(question),
        "answer_type": result.get("answer_type"),
        "answered": result.get("answer_type") == "GROUNDED",
        "tools": [t["tool"] for t in result.get("tool_calls", [])],
        "cited_doc_ids": result.get("citations", []),
        "guard_blocked": bool(result.get("guard_blocked")),
    }
    with STORE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
