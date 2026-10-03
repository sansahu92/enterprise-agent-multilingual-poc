"""Safety controls that do not depend on the model behaving.

1. Prompt Shields (optional): detects direct and indirect (document) prompt-injection attacks.
2. Output guard: redacts e-mail addresses outside approved domains (exfiltration control).
3. PII redaction: applied before query text is written to the analytics store.
"""
import re

import httpx

from poc import config

EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@([A-Za-z0-9.\-]+\.[A-Za-z]{2,})")
PHONE = re.compile(r"(?<!\w)(\+?\d[\d\s\-]{7,}\d)(?!\w)")
LONG_NUMBER = re.compile(r"\b\d{6,}\b")
EMIRATES_ID = re.compile(r"\b784[-\s]?\d{4}[-\s]?\d{7}[-\s]?\d\b")


def shield_prompt(user_prompt: str, documents: list[str]) -> dict:
    """Call Azure AI Content Safety Prompt Shields. Returns {'user_attack': bool, 'doc_attacks': [bool]}.

    Disabled (no detection) when CONTENT_SAFETY_ENDPOINT is not configured.
    """
    endpoint = config.env("CONTENT_SAFETY_ENDPOINT")
    if not endpoint:
        return {"enabled": False, "user_attack": False, "doc_attacks": [False] * len(documents)}
    url = endpoint.rstrip("/") + "/contentsafety/text:shieldPrompt?api-version=2024-09-01"
    headers = {"Content-Type": "application/json"}
    if config.AUTH_MODE == "key":
        headers["Ocp-Apim-Subscription-Key"] = config.env("CONTENT_SAFETY_KEY", required=True)
    else:
        token = config.credential().get_token("https://cognitiveservices.azure.com/.default").token
        headers["Authorization"] = f"Bearer {token}"
    body = {"userPrompt": user_prompt[:10000], "documents": [d[:10000] for d in documents]}
    r = httpx.post(url, json=body, headers=headers, timeout=10)
    r.raise_for_status()
    data = r.json()
    return {
        "enabled": True,
        "user_attack": bool(data.get("userPromptAnalysis", {}).get("attackDetected")),
        "doc_attacks": [bool(d.get("attackDetected")) for d in data.get("documentsAnalysis", [])],
    }


def guard_output(text: str) -> tuple[str, list[str]]:
    """Redact e-mail addresses whose domain is not approved. Returns (safe_text, blocked_domains)."""
    blocked = []

    def repl(m):
        domain = m.group(1).lower()
        if any(domain == d or domain.endswith("." + d) for d in config.APPROVED_EMAIL_DOMAINS):
            return m.group(0)
        blocked.append(domain)
        return "[external address removed]"

    return EMAIL.sub(repl, text), blocked


def redact_pii(text: str) -> str:
    """Lightweight PII redaction for the analytics store (production: Azure AI Language PII detection)."""
    text = EMIRATES_ID.sub("[ID]", text)
    text = EMAIL.sub("[EMAIL]", text)
    text = PHONE.sub("[PHONE]", text)
    text = LONG_NUMBER.sub("[NUMBER]", text)
    return text
