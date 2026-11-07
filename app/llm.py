import json, os, re
import httpx

API_KEY = lambda: os.getenv("ANTHROPIC_API_KEY", "")
MODEL = lambda: os.getenv("LLM_MODEL", "claude-sonnet-5-5")

SYSTEM = """You are a brand compliance reviewer. Given brand rules/facts and a piece of content,
find violations. Return ONLY a JSON object: {"summary": str, "findings": [{"rule_id": str,
"category": str, "severity": "critical|warning|suggestion", "evidence": exact quote from content,
"explanation": str, "suggested_fix": str}]}. Only report genuine issues supported by the provided rules."""

def llm_review(text, context_docs):
    if not API_KEY():
        return None
    ctx = "\n".join(f'[{d["id"]}] {d["text"]}' for d in context_docs)
    try:
        r = httpx.post("https://api.anthropic.com/v1/messages", timeout=60,
            headers={"x-api-key": API_KEY(), "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": MODEL(), "max_tokens": 1500, "system": SYSTEM,
                  "messages": [{"role": "user", "content": f"BRAND CONTEXT:\n{ctx}\n\nCONTENT:\n{text}"}]})
        r.raise_for_status()
        raw = "".join(b.get("text", "") for b in r.json()["content"])
        data = json.loads(re.sub(r"```json|```", "", raw).strip())
        for f in data.get("findings", []):
            f["source"] = "llm"
            if f.get("severity") not in ("critical", "warning", "suggestion"): f["severity"] = "suggestion"
        return data
    except Exception as e:
        print("LLM error:", e)
        return None
