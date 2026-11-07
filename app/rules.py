"""Deterministic checks: regex brand rules + approved-fact verification."""
import re
from .retrieval import GUIDE

def _sentences(text):
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", text) if s.strip()]

def rule_findings(text):
    out = []
    for s in _sentences(text):
        for r in GUIDE["rules"]:
            for p in r["patterns"]:
                if re.search(p, s, re.IGNORECASE if r["id"] != "R5" and r["id"] != "R6" else 0):
                    out.append({"rule_id": r["id"], "category": r["category"], "severity": r["severity"],
                                "evidence": s, "explanation": r["text"], "suggested_fix": r["suggestion"],
                                "source": "rules"})
                    break
    return out

FACT_PATTERNS = {
    "F1": r"(\d+)[- ]day (free )?trial",
    "F2": r"\$\s?(\d+(?:\.\d+)?)\s*(?:per user|/user|a user)",
    "F3": r"founded in (\d{4})",
    "F4": r"(\d+(?:\.\d+)?)\s*%\s*uptime",
}

def fact_findings(text):
    out = []
    for s in _sentences(text):
        for f in GUIDE["facts"]:
            m = re.search(FACT_PATTERNS[f["id"]], s, re.IGNORECASE)
            if m and float(m.group(1)) != float(f["number"]):
                out.append({"rule_id": f["id"], "category": "Facts", "severity": "critical",
                            "evidence": s, "explanation": f'Conflicts with approved fact: {f["text"]}',
                            "suggested_fix": f'Correct the figure. Approved fact: {f["text"]}',
                            "source": "facts"})
    return out
