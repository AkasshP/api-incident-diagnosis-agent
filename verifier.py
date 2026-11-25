"""Verify the report with a fresh read of the incident bundle."""

from report_builder import diagnose
from tools import Evidence


def verify(report, folder, request_id, window_seconds):
    """Check the report using a fresh read of the incident bundle."""
    reference = diagnose(Evidence(folder), request_id, window_seconds)
    decisions = report.get("decisions", [])
    finish = decisions[-1] if decisions else {}
    completed = finish.get("action") == "finish" and "rejected" not in finish
    problems = []
    if not completed or finish.get("outcome") != report["outcome"]:
        problems.append(f"The investigation ended without an accepted finish: {report['summary']}")
    for field in ("request_id", "outcome", "summary", "invalid_field", "likely_failure_location", "next_record",
                  "missing_observation", "citations", "timeline"):
        if report.get(field) != reference.get(field):
            problems.append(f"{field} differs from the independent evidence check.")
    problems.extend(f"Evidence warning: {warning}" for warning in reference["warnings"])
    return {
        "verified": not problems,
        "rule_based_outcome": reference["outcome"],
        "rule_based_citations": reference["citations"],
        "problems": problems,
    }
