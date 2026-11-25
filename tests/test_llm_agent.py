"""Regression checks for validation, the decision loop, and verification."""

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from tools import GATEWAY, INVENTORY, ORDER
from tools import Evidence
from llm_agent import investigate
from verifier import verify

ROOT = Path(__file__).resolve().parents[1]
INVALID = ROOT / "incidents/invalid_request"
OUTAGE = ROOT / "incidents/service_outage"
MISSING = ROOT / "incidents/insufficient_evidence"


def decision(action, **arguments):
    return json.dumps({"action": action, "reasoning": "Check saved incident evidence.", **arguments})


def read(service):
    return decision("read_logs", service=service)


def finish(outcome):
    return decision("finish", outcome=outcome)


class ArchitectureTests(unittest.TestCase):
    def investigate_with(self, folder, request_id, responses):
        with patch("llm_agent.ask_model", side_effect=responses):
            return investigate(folder, request_id, "test-model", 300)

    def test_invalid_input_does_not_call_model(self):
        with patch("llm_agent.ask_model") as model:
            for folder, request_id, tag, window in (
                (ROOT / "absent", "request-100", "test-model", 300),
                (INVALID, "  ", "test-model", 300),
                (INVALID, "request-100", "", 300),
                (INVALID, "request-100", "test-model", -1),
            ):
                with self.subTest(folder=folder, request_id=request_id, tag=tag, window=window):
                    with self.assertRaises(ValueError):
                        investigate(folder, request_id, tag, window)
            model.assert_not_called()

    def test_invalid_request_stops_at_gateway_and_grounds_summary(self):
        report = self.investigate_with(INVALID, "request-100", [
            read(GATEWAY), decision("finish", outcome="invalid_request", summary="Invented explanation"),
        ])
        self.assertTrue(report["verification"]["verified"])
        self.assertEqual(report["invalid_field"], "item_id")
        self.assertEqual(report["citations"], ["E11"])
        self.assertEqual([entry["service"] for entry in report["tool_trace"]], [GATEWAY])
        self.assertNotIn("Invented", report["summary"])

    def test_rejected_finish_then_model_selects_remaining_tools(self):
        timeout = next(
            row["timestamp"] for row in Evidence(OUTAGE).read_logs("request-200", ORDER)
            if row["event_type"] == "downstream_timeout"
        )
        report = self.investigate_with(OUTAGE, "request-200", [
            read(GATEWAY), read(ORDER), finish("inventory_failure"),
            decision("check_endpoint_status", service=INVENTORY, incident_time=timeout),
            read(INVENTORY), decision("inspect_dependencies", service=ORDER, route="POST /orders"),
            finish("inventory_failure"),
        ])
        self.assertTrue(report["verification"]["verified"])
        self.assertEqual(report["citations"], ["HC17", "E22", "E21", "HC18"])
        self.assertEqual(len(report["decisions"][2]["rejected"]["required_checks"]), 3)
        self.assertEqual(report["tool_trace"][2]["tool"], "check_endpoint_status")

    def test_missing_record_is_a_verified_outcome(self):
        timeout = next(
            row["timestamp"] for row in Evidence(MISSING).read_logs("request-123", ORDER)
            if row["event_type"] == "downstream_timeout"
        )
        report = self.investigate_with(MISSING, "request-123", [
            read(GATEWAY), read(ORDER), read(INVENTORY),
            decision("inspect_dependencies", service=ORDER, route="POST /orders"),
            decision("check_endpoint_status", service=INVENTORY, incident_time=timeout),
            finish("inventory_failure"), finish("insufficient_evidence"),
        ])
        self.assertTrue(report["verification"]["verified"])
        self.assertEqual(report["citations"], ["E31"])
        self.assertIn("Inventory Service", report["next_record"])
        self.assertIn("rejected", report["decisions"][-2])

    def test_bad_and_duplicate_tool_calls_do_not_touch_evidence(self):
        report = self.investigate_with(INVALID, "request-100", [
            decision("check_endpoint_status", service=INVENTORY, incident_time="invalid"),
            read(GATEWAY), read(GATEWAY), finish("invalid_request"),
        ])
        self.assertTrue(report["verification"]["verified"])
        self.assertEqual(len(report["tool_trace"]), 1)
        self.assertIn("error", report["decisions"][0]["result"])
        self.assertIn("already completed", report["decisions"][2]["result"]["error"])

    def test_recent_errors_stay_request_scoped_and_do_not_replace_service_logs(self):
        report = self.investigate_with(INVALID, "request-100", [
            decision("read_recent_errors"), finish("invalid_request"), read(GATEWAY),
            finish("invalid_request"),
        ])
        errors = report["decisions"][0]["result"]["data"]
        self.assertEqual([event["event_id"] for event in errors], ["E11"])
        self.assertIn("rejected", report["decisions"][1])
        self.assertTrue(report["verification"]["verified"])

    def test_model_retry_and_step_limits(self):
        report = self.investigate_with(INVALID, "request-100", [
            URLError("temporary"), read(GATEWAY), finish("invalid_request"),
        ])
        self.assertTrue(report["verification"]["verified"])
        self.assertTrue(report["decisions"][0]["retry"])
        with patch("llm_agent.MAX_STEPS", 2):
            report = self.investigate_with(INVALID, "request-100", ["bad", "bad"])
        self.assertFalse(report["verification"]["verified"])
        self.assertEqual(len(report["decisions"]), 2)
        self.assertIn("accepted finish", report["verification"]["problems"][0])

    def test_non_retryable_model_error_is_unverified(self):
        report = self.investigate_with(INVALID, "request-100", [
            HTTPError("http://localhost/", 404, "not found", {}, None),
        ])
        self.assertFalse(report["verification"]["verified"])
        self.assertFalse(report["decisions"][0]["retry"])
        self.assertIn("404", report["summary"])

    def test_verifier_detects_missing_file_and_altered_report(self):
        with TemporaryDirectory() as temp:
            report = self.investigate_with(Path(temp), "request-100", [
                read(GATEWAY), read(ORDER), finish("insufficient_evidence"),
            ])
        self.assertFalse(report["verification"]["verified"])
        self.assertIn("warnings", report["decisions"][0]["result"])
        report = self.investigate_with(INVALID, "request-100", [read(GATEWAY), finish("invalid_request")])
        report["citations"] = ["made-up"]
        self.assertFalse(verify(report, INVALID, "request-100", 300)["verified"])


if __name__ == "__main__":
    unittest.main()
