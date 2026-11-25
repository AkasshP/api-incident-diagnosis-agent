"""Run the agent on the saved incidents and compare its reports with expected results.

Usage: python3 evaluate.py            # rule-based agent and replay
       python3 evaluate.py --llm      # also the Ollama agent (optionally --llm MODEL)
"""

import argparse
import json
import sys
from pathlib import Path

from support.demo import replay
from llm_agent import DEFAULT_MODEL, DEFAULT_WINDOW_SECONDS, investigate
from report_builder import diagnose
from tools import GATEWAY as GW, INVENTORY as INV, ORDER, Evidence

ROOT = Path(__file__).parent

# Each case: folder, request ID, and what a correct report must contain.
CASES = [
    {
        "name": "Invalid request (HTTP 400)",
        "folder": "incidents/invalid_request",
        "request_id": "request-100",
        "outcome": "invalid_request",
        "citations": ["E11"],
        "fields": {"invalid_field": "item_id"},
        "tool_calls": [("read_logs", GW)],
    },
    {
        "name": "Inventory outage (HTTP 504)",
        "folder": "incidents/service_outage",
        "request_id": "request-200",
        "outcome": "inventory_failure",
        "citations": ["HC17", "E22", "E21", "HC18"],
        "fields": {"likely_failure_location": INV},
        "tool_calls": [
            ("read_logs", GW), ("read_logs", ORDER), ("inspect_dependencies", ORDER),
            ("read_logs", INV), ("check_endpoint_status", INV),
        ],
    },
    {
        "name": "Insufficient evidence (HTTP 504)",
        "folder": "incidents/insufficient_evidence",
        "request_id": "request-123",
        "outcome": "insufficient_evidence",
        "citations": ["E31"],
        "fields": {
            "summary": "Order Service timed out waiting for Inventory Service.",
            "next_record": "An Inventory Service handler event for request request-123.",
            "missing_observation": "inventory_handler_entry",
        },
        "tool_calls": [
            ("read_logs", GW), ("read_logs", ORDER), ("inspect_dependencies", ORDER),
            ("read_logs", INV), ("check_endpoint_status", INV),
        ],
    },
    {
        "name": "Evidence removal (outage minus E22, HC17, HC18)",
        "folder": "tests/fixtures/outage_evidence_removed",
        "request_id": "request-200",
        "outcome": "insufficient_evidence",
        "citations": ["E21"],
        "fields": {"missing_observation": "inventory_handler_entry"},
        "tool_calls": [
            ("read_logs", GW), ("read_logs", ORDER), ("inspect_dependencies", ORDER),
            ("read_logs", INV), ("check_endpoint_status", INV),
        ],
    },
]

EXPECTED_REPLAY = {
    "baseline_indistinguishable_pairs": 1,
    "scores": {"handler_entered": 1, "gateway_timeout": 0, "readiness_check": 0},
    "recommended_events": ["handler_entered"],
    "top_score_ties": [],
}


def check_case(case):
    report = diagnose(Evidence(ROOT / case["folder"]), case["request_id"], DEFAULT_WINDOW_SECONDS)
    problems = []
    if report["outcome"] != case["outcome"]:
        problems.append(f"outcome {report['outcome']!r}, expected {case['outcome']!r}")
    if report["citations"] != case["citations"]:
        problems.append(f"citations {report['citations']}, expected {case['citations']}")
    for key, value in case["fields"].items():
        if report.get(key) != value:
            problems.append(f"{key} {report.get(key)!r}, expected {value!r}")
    calls = [(call["tool"], call.get("service")) for call in report["tool_trace"]]
    if calls != case["tool_calls"]:
        problems.append(f"tool calls {calls}, expected {case['tool_calls']}")
    if report["warnings"]:
        problems.append(f"unexpected warnings {report['warnings']}")
    return report, problems


def check_replay():
    result = replay(ROOT / "support/replay/replay.json", "inventory_handler_entry")
    scores = {c["event_name"]: c["score"] for c in result["candidates"]}
    problems = []
    for key in ("baseline_indistinguishable_pairs", "recommended_events", "top_score_ties"):
        if result[key] != EXPECTED_REPLAY[key]:
            problems.append(f"{key} {result[key]!r}, expected {EXPECTED_REPLAY[key]!r}")
    if scores != EXPECTED_REPLAY["scores"]:
        problems.append(f"scores {scores}, expected {EXPECTED_REPLAY['scores']}")
    return scores, problems


def check_llm_case(case, model):
    report = investigate(ROOT / case["folder"], case["request_id"], model, DEFAULT_WINDOW_SECONDS)
    problems = list(report["verification"]["problems"])
    if report["outcome"] != case["outcome"]:
        problems.append(f"outcome {report['outcome']!r}, expected {case['outcome']!r}")
    if set(report["citations"]) != set(case["citations"]):
        problems.append(f"citations {report['citations']}, expected {case['citations']}")
    expected_calls = set(case["tool_calls"])
    used = {(step["tool"], step["service"]) for step in report["tool_trace"] if "service" in step}
    if expected_calls - used:
        problems.append(f"missing tool calls: {sorted(expected_calls - used)}")
    extra_services = {service for _, service in used} - {service for _, service in expected_calls}
    if extra_services:
        problems.append(f"looked at services it did not need: {sorted(extra_services)}")
    for key, value in case["fields"].items():
        if report.get(key) != value:
            problems.append(f"{key} {report.get(key)!r}, expected {value!r}")
    return report, problems


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--llm", nargs="?", const=DEFAULT_MODEL, metavar="MODEL",
                        help="also evaluate the Ollama agent")
    parser.add_argument("--json-output", type=Path, help="write structured evaluation results")
    args = parser.parse_args()
    llm_model = args.llm
    lines = ["| Case | Expected | Actual | Citations | Result |", "|---|---|---|---|---|"]
    results = []

    def save_case(case, report, problems, model=None):
        results.append({
            "case": case["name"],
            "mode": "ollama" if model else "rules",
            "model": model,
            "expected": case["outcome"],
            "actual": report["outcome"],
            "citations": report["citations"],
            "tool_calls": len(report["tool_trace"]),
            "passed": not problems,
            "problems": problems,
        })

    for case in CASES:
        report, problems = check_case(case)
        save_case(case, report, problems)
        lines.append(
            f"| {case['name']} | {case['outcome']} | {report['outcome']} | "
            f"{', '.join(report['citations'])} | {'FAIL' if problems else 'PASS'} |"
        )
        lines.extend(f"|  ↳ {problem} | | | | |" for problem in problems)

    scores, problems = check_replay()
    results.append({
        "case": "Advisor replay",
        "mode": "replay",
        "scores": scores,
        "passed": not problems,
        "problems": problems,
    })
    score_text = ", ".join(f"{name} {score}" for name, score in scores.items())
    lines.append(
        f"| Advisor replay | handler_entered recommended | {score_text} | — | "
        f"{'FAIL' if problems else 'PASS'} |"
    )
    lines.extend(f"|  ↳ {problem} | | | | |" for problem in problems)

    if llm_model:
        for case in CASES:
            report, problems = check_llm_case(case, llm_model)
            save_case(case, report, problems, llm_model)
            steps = len(report["tool_trace"])
            lines.append(
                f"| LLM ({llm_model}): {case['name']} | {case['outcome']} | {report['outcome']} | "
                f"{', '.join(report['citations'])} ({steps} tool calls) | {'FAIL' if problems else 'PASS'} |"
            )
            lines.extend(f"|  ↳ {problem} | | | | |" for problem in problems)

    print("\n".join(lines))
    passed = sum(case["passed"] for case in results)
    total = len(results)
    print(f"\n{passed}/{total} checks passed.")
    if args.json_output:
        args.json_output.parent.mkdir(parents=True, exist_ok=True)
        payload = {"passed": passed, "total": total, "cases": results}
        args.json_output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
