"""Run the agent on the saved incidents and compare its reports with expected results."""

import sys
from pathlib import Path

from agent import diagnose
from demo import replay
from evidence import Evidence

ROOT = Path(__file__).parent
GW, ORDER, INV = "API Gateway", "Order Service", "Inventory Service"

# Each case: folder, request ID, and what a correct report must contain.
CASES = [
    {
        "name": "Invalid request (HTTP 400)",
        "folder": "incidents/invalid-request",
        "request_id": "request-100",
        "outcome": "invalid_request",
        "citations": ["E11"],
        "fields": {"invalid_field": "item_id"},
        "tool_calls": [("read_logs", GW)],
    },
    {
        "name": "Inventory outage (HTTP 504)",
        "folder": "incidents/inventory-outage",
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
        "folder": "incidents/missing-inventory",
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
        "folder": "incidents/outage-evidence-removed",
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
    report = diagnose(Evidence(ROOT / case["folder"]), case["request_id"], 300)
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
    result = replay(ROOT / "replay/replay.json", "inventory_handler_entry")
    scores = {c["event_name"]: c["score"] for c in result["candidates"]}
    problems = []
    for key in ("baseline_indistinguishable_pairs", "recommended_events", "top_score_ties"):
        if result[key] != EXPECTED_REPLAY[key]:
            problems.append(f"{key} {result[key]!r}, expected {EXPECTED_REPLAY[key]!r}")
    if scores != EXPECTED_REPLAY["scores"]:
        problems.append(f"scores {scores}, expected {EXPECTED_REPLAY['scores']}")
    return scores, problems


def main():
    lines = ["| Case | Expected | Actual | Citations | Result |", "|---|---|---|---|---|"]
    failures = 0
    for case in CASES:
        report, problems = check_case(case)
        failures += bool(problems)
        lines.append(
            f"| {case['name']} | {case['outcome']} | {report['outcome']} | "
            f"{', '.join(report['citations'])} | {'FAIL' if problems else 'PASS'} |"
        )
        lines.extend(f"|  ↳ {problem} | | | | |" for problem in problems)

    scores, problems = check_replay()
    failures += bool(problems)
    score_text = ", ".join(f"{name} {score}" for name, score in scores.items())
    lines.append(
        f"| Advisor replay | handler_entered recommended | {score_text} | — | "
        f"{'FAIL' if problems else 'PASS'} |"
    )
    lines.extend(f"|  ↳ {problem} | | | | |" for problem in problems)

    print("\n".join(lines))
    print(f"\n{len(CASES) + 1 - failures}/{len(CASES) + 1} checks passed.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
