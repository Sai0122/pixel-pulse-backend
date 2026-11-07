"""Run test_cases.json through the review engine and print a detection / false-alarm report.
Usage (from backend root):  python tests/run_eval.py            # rules-only
Writes tests/eval_results.json for the evaluation sheet."""
import json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
from app import rules

cases = json.loads((Path(__file__).parent / "test_cases.json").read_text())
out = []
for c in cases:
    found = rules.rule_findings(c["content"]) + rules.fact_findings(c["content"])
    actual = sorted({f["category"] for f in found})
    exp = set(c["expected"])
    out.append({**c, "actual": actual, "detected": len(exp & set(actual)), "false_alarms": len(set(actual) - exp)})
Path(__file__).with_name("eval_results.json").write_text(json.dumps(out, indent=1))
exp_n = sum(len(o["expected"]) for o in out); det = sum(o["detected"] for o in out)
fa = sum(o["false_alarms"] for o in out)
print(f"Detected {det}/{exp_n} expected issues ({det/exp_n:.0%}); false alarms: {fa}")
for o in out:
    print(o["id"], o["type"], "expected", o["expected"], "actual", o["actual"])
