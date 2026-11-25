"""Check which records each tool returns and which records each incident cites.

Every incident folder mixes the evidence that matters with decoys: other requests'
errors, non-failure events, probes just outside the time window, probes of the
wrong service and extra routes. These tests pin down exactly what each tool
returns, what the diagnosis cites, and how the agent loop reacts when a scripted
model takes the right path, finishes too early or picks the wrong outcome.
"""

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from llm_agent import investigate
from report_builder import diagnose
from tools import GATEWAY, INVENTORY, ORDER, Evidence, utc_time

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests/fixtures"
WINDOW = 300
ROUTE = "POST /orders"


def ids(records):
    return [record.get("event_id") or record.get("check_id") for record in records]


def decision(action, **arguments):
    return json.dumps({"action": action, "reasoning": "Follow the saved evidence.", **arguments})


def read(service):
    return decision("read_logs", service=service)


def inspect(service, route=ROUTE):
    return decision("inspect_dependencies", service=service, route=route)


def probe(service, incident_time):
    return decision("check_endpoint_status", service=service, incident_time=incident_time)


def finish(outcome):
    return decision("finish", outcome=outcome)


# What each tool must return for each incident, and what the diagnosis must cite.
# "logs" lists the expected read_logs result for every service, in time order.
# "probes" is (service, incident_time, expected check IDs) for check_endpoint_status.
INCIDENTS = {
    "invalid_request": {
        "folder": ROOT / "incidents/invalid_request",
        "request_id": "request-100",
        "logs": {GATEWAY: ["E10", "E15", "E11"], ORDER: [], INVENTORY: []},
        "recent_errors": ["E11"],
        "routes": {
            (GATEWAY, ROUTE): ORDER,
            (GATEWAY, "POST /payments"): "Payment Service",
            (ORDER, "GET /orders"): "Order Database",
            (INVENTORY, ROUTE): None,
        },
        "probes": None,
        "outcome": "invalid_request",
        "citations": ["E11"],
        "next_record": None,
        "trace": [("read_logs", GATEWAY)],
        "warnings": False,
        "script": [read(GATEWAY), finish("invalid_request")],
    },
    "service_outage": {
        "folder": ROOT / "incidents/service_outage",
        "request_id": "request-200",
        "logs": {GATEWAY: ["E20", "E27"], ORDER: ["E23", "E21"], INVENTORY: ["E22"]},
        "recent_errors": ["E21", "E22"],
        "routes": {(GATEWAY, ROUTE): ORDER, (ORDER, ROUTE): INVENTORY},
        "probes": (INVENTORY, "2026-11-23T14:00:30Z", ["HC17", "HC18", "HC22"]),
        "outcome": "inventory_failure",
        "citations": ["HC17", "E22", "E21", "HC18"],
        "next_record": None,
        "trace": [
            ("read_logs", GATEWAY), ("read_logs", ORDER), ("inspect_dependencies", ORDER),
            ("read_logs", INVENTORY), ("check_endpoint_status", INVENTORY),
        ],
        "warnings": False,
        "script": [
            read(GATEWAY), read(ORDER), inspect(ORDER), read(INVENTORY),
            probe(INVENTORY, "2026-11-23T14:00:30Z"), finish("inventory_failure"),
        ],
    },
    "insufficient_evidence": {
        "folder": ROOT / "incidents/insufficient_evidence",
        "request_id": "request-123",
        "logs": {GATEWAY: ["E30"], ORDER: ["E32", "E31"], INVENTORY: []},
        "recent_errors": ["E31"],
        "routes": {(ORDER, ROUTE): INVENTORY},
        "probes": (INVENTORY, "2026-11-23T14:00:30Z", ["HC36"]),
        "outcome": "insufficient_evidence",
        "citations": ["E31"],
        "next_record": "An Inventory Service handler event for request request-123.",
        "trace": [
            ("read_logs", GATEWAY), ("read_logs", ORDER), ("inspect_dependencies", ORDER),
            ("read_logs", INVENTORY), ("check_endpoint_status", INVENTORY),
        ],
        "warnings": False,
        "script": [
            read(GATEWAY), read(ORDER), inspect(ORDER), read(INVENTORY),
            probe(INVENTORY, "2026-11-23T14:00:30Z"), finish("insufficient_evidence"),
        ],
    },
    "outage_evidence_removed": {
        "folder": FIXTURES / "outage_evidence_removed",
        "request_id": "request-200",
        "logs": {GATEWAY: ["E20", "E27"], ORDER: ["E23", "E21"], INVENTORY: []},
        "recent_errors": ["E21"],
        "routes": {(ORDER, ROUTE): INVENTORY},
        "probes": (INVENTORY, "2026-11-23T14:00:30Z", ["HC22"]),
        "outcome": "insufficient_evidence",
        "citations": ["E21"],
        "next_record": "An Inventory Service handler event for request request-200.",
        "trace": [
            ("read_logs", GATEWAY), ("read_logs", ORDER), ("inspect_dependencies", ORDER),
            ("read_logs", INVENTORY), ("check_endpoint_status", INVENTORY),
        ],
        "warnings": False,
        "script": [
            read(GATEWAY), read(ORDER), inspect(ORDER), read(INVENTORY),
            probe(INVENTORY, "2026-11-23T14:00:30Z"), finish("insufficient_evidence"),
        ],
    },
    "gateway_order_timeout": {
        "folder": FIXTURES / "gateway_order_timeout",
        "request_id": "request-400",
        "logs": {GATEWAY: ["E40", "E41"], ORDER: [], INVENTORY: []},
        "recent_errors": ["E41"],
        "routes": {(GATEWAY, ROUTE): ORDER},
        "probes": (ORDER, "2026-11-23T15:00:30Z", ["HC40"]),
        "outcome": "insufficient_evidence",
        "citations": ["E41"],
        "next_record": "An Order Service event for request request-400 showing how it handled the request.",
        "trace": [("read_logs", GATEWAY), ("inspect_dependencies", GATEWAY), ("read_logs", ORDER)],
        "warnings": False,
        "script": [read(GATEWAY), inspect(GATEWAY), read(ORDER), finish("insufficient_evidence")],
    },
    "full_chain_outage": {
        "folder": FIXTURES / "full_chain_outage",
        "request_id": "request-500",
        "logs": {GATEWAY: ["E53", "E51"], ORDER: ["E50"], INVENTORY: ["E52"]},
        "recent_errors": ["E51", "E50", "E52"],
        "routes": {(GATEWAY, ROUTE): ORDER, (ORDER, ROUTE): INVENTORY},
        "probes": (INVENTORY, "2026-11-23T16:00:30Z", ["HC50"]),
        "outcome": "inventory_failure",
        "citations": ["E52", "HC50", "E50", "E51"],
        "next_record": None,
        "trace": [
            ("read_logs", GATEWAY), ("inspect_dependencies", GATEWAY), ("read_logs", ORDER),
            ("inspect_dependencies", ORDER), ("read_logs", INVENTORY), ("check_endpoint_status", INVENTORY),
        ],
        "warnings": False,
        "script": [
            read(GATEWAY), inspect(GATEWAY), read(ORDER), inspect(ORDER), read(INVENTORY),
            probe(INVENTORY, "2026-11-23T16:00:30Z"), finish("inventory_failure"),
        ],
    },
    "window_boundary": {
        "folder": FIXTURES / "window_boundary",
        "request_id": "request-700",
        "logs": {GATEWAY: ["E72"], ORDER: ["E70"], INVENTORY: ["E71"]},
        "recent_errors": ["E70", "E71"],
        "routes": {(ORDER, ROUTE): INVENTORY},
        "probes": (INVENTORY, "2026-11-23T17:00:00Z", ["HC70", "HC71"]),
        "outcome": "inventory_failure",
        "citations": ["HC70", "E71", "E70", "HC71"],
        "next_record": None,
        "trace": [
            ("read_logs", GATEWAY), ("read_logs", ORDER), ("inspect_dependencies", ORDER),
            ("read_logs", INVENTORY), ("check_endpoint_status", INVENTORY),
        ],
        "warnings": False,
        "script": [
            read(GATEWAY), read(ORDER), inspect(ORDER), read(INVENTORY),
            probe(INVENTORY, "2026-11-23T17:00:00Z"), finish("inventory_failure"),
        ],
    },
    "route_mismatch": {
        "folder": FIXTURES / "route_mismatch",
        "request_id": "request-600",
        "logs": {GATEWAY: [], ORDER: ["E60"], INVENTORY: ["E61"]},
        "recent_errors": ["E60", "E61"],
        "routes": {(ORDER, ROUTE): "Warehouse Service"},
        "probes": (INVENTORY, "2026-11-23T18:00:30Z", []),
        "outcome": "inventory_failure",
        "citations": ["E61", "E60"],
        "next_record": None,
        "trace": [
            ("read_logs", GATEWAY), ("read_logs", ORDER), ("inspect_dependencies", ORDER),
            ("read_logs", INVENTORY), ("check_endpoint_status", INVENTORY),
        ],
        "warnings": True,
        "script": [
            read(GATEWAY), read(ORDER), inspect(ORDER), read(INVENTORY),
            probe(INVENTORY, "2026-11-23T18:00:30Z"), finish("inventory_failure"),
        ],
    },
    "noisy_logs": {
        "folder": FIXTURES / "noisy_logs",
        "request_id": "request-800",
        "logs": {GATEWAY: ["E80"], ORDER: [], INVENTORY: []},
        "recent_errors": ["E80"],
        "routes": {(GATEWAY, ROUTE): ORDER},
        "probes": None,
        "outcome": "invalid_request",
        "citations": ["E80"],
        "next_record": None,
        "trace": [("read_logs", GATEWAY)],
        "warnings": True,
        "script": [read(GATEWAY), finish("invalid_request")],
    },
}


class ToolSelectionTests(unittest.TestCase):
    """Each tool returns only the records that belong to the request it was asked about."""

    def test_read_logs_returns_only_this_request_and_service_in_time_order(self):
        for name, case in INCIDENTS.items():
            for service, expected in case["logs"].items():
                with self.subTest(incident=name, service=service):
                    events = Evidence(case["folder"]).read_logs(case["request_id"], service)
                    self.assertEqual(ids(events), expected)
                    for event in events:
                        self.assertEqual(event["request_id"], case["request_id"])
                        self.assertEqual(event["service"], service)
                    times = [utc_time(event["timestamp"]) for event in events]
                    self.assertEqual(times, sorted(times))

    def test_recent_errors_are_failures_for_this_request_newest_first(self):
        for name, case in INCIDENTS.items():
            with self.subTest(incident=name):
                errors = Evidence(case["folder"]).read_recent_errors(case["request_id"])
                self.assertEqual(ids(errors), case["recent_errors"])
                for event in errors:
                    self.assertEqual(event["request_id"], case["request_id"])
                    self.assertIn(event["event_type"], {"validation_error", "downstream_timeout", "handler_error"})

    def test_inspect_dependencies_selects_the_route_asked_for(self):
        for name, case in INCIDENTS.items():
            for (service, route), expected in case["routes"].items():
                with self.subTest(incident=name, service=service, route=route):
                    self.assertEqual(Evidence(case["folder"]).inspect_dependencies(service, route), expected)

    def test_endpoint_status_selects_target_service_inside_window(self):
        for name, case in INCIDENTS.items():
            if case["probes"] is None:
                continue
            service, incident_time, expected = case["probes"]
            with self.subTest(incident=name):
                checks = Evidence(case["folder"]).check_endpoint_status(service, incident_time, WINDOW)
                self.assertEqual(ids(checks), expected)
                reference = utc_time(incident_time)
                for check in checks:
                    self.assertEqual(check["target_service"], service)
                    self.assertLessEqual(abs((utc_time(check["checked_at"]) - reference).total_seconds()), WINDOW)

    def test_window_edges_are_inclusive_and_one_second_past_is_excluded(self):
        evidence = Evidence(FIXTURES / "window_boundary")
        checks = ids(evidence.check_endpoint_status(INVENTORY, "2026-11-23T17:00:00Z", WINDOW))
        self.assertEqual(checks, ["HC70", "HC71"])
        self.assertNotIn("HC72", checks)
        self.assertNotIn("HC73", checks)
        self.assertEqual(ids(evidence.check_endpoint_status(INVENTORY, "2026-11-23T17:00:00Z", 0)), [])

    def test_service_names_match_regardless_of_case_dash_or_underscore(self):
        evidence = Evidence(ROOT / "incidents/service_outage")
        for spelling in ("Inventory Service", "inventory service", "inventory-service", "INVENTORY_SERVICE"):
            with self.subTest(spelling=spelling):
                self.assertEqual(ids(evidence.read_logs("request-200", spelling)), ["E22"])

    def test_unknown_service_route_or_request_returns_nothing(self):
        evidence = Evidence(ROOT / "incidents/service_outage")
        self.assertEqual(evidence.read_logs("request-999", ORDER), [])
        self.assertEqual(evidence.read_recent_errors("request-999"), [])
        self.assertIsNone(evidence.inspect_dependencies("Payment Service", ROUTE))
        self.assertIsNone(evidence.inspect_dependencies(ORDER, "DELETE /orders"))
        self.assertEqual(evidence.check_endpoint_status("Payment Service", "2026-11-23T14:00:30Z", WINDOW), [])
        self.assertEqual(evidence.check_endpoint_status(INVENTORY, "not a time", WINDOW), [])

    def test_recent_errors_keep_only_the_five_newest(self):
        with TemporaryDirectory() as temp:
            rows = [
                {"event_id": f"E{n}", "timestamp": f"2026-11-23T14:00:{n:02d}Z", "request_id": "request-1",
                 "service": ORDER, "event_type": "downstream_timeout", "details": {}}
                for n in range(1, 8)
            ] + [
                {"event_id": "E90", "timestamp": "2026-11-23T14:00:59Z", "request_id": "request-1",
                 "service": ORDER, "event_type": "order_created", "details": {}},
                {"event_id": "E91", "timestamp": "2026-11-23T14:00:58Z", "request_id": "request-2",
                 "service": ORDER, "event_type": "handler_error", "details": {}},
            ]
            Path(temp, "logs.jsonl").write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
            self.assertEqual(ids(Evidence(Path(temp)).read_recent_errors("request-1")), ["E7", "E6", "E5", "E4", "E3"])

    def test_invalid_lines_are_skipped_with_warnings(self):
        evidence = Evidence(FIXTURES / "noisy_logs")
        self.assertEqual(ids(evidence.read_logs("request-800", GATEWAY)), ["E80"])
        self.assertEqual(len(evidence.warnings), 3)
        self.assertTrue(any("line 2" in warning for warning in evidence.warnings))
        self.assertTrue(any("expected an object" in warning for warning in evidence.warnings))
        self.assertTrue(any("invalid event" in warning for warning in evidence.warnings))


class IncidentDiagnosisTests(unittest.TestCase):
    """The rule-based diagnosis for each incident picks the right records and nothing else."""

    def test_each_incident_outcome_citations_and_tool_order(self):
        for name, case in INCIDENTS.items():
            with self.subTest(incident=name):
                report = diagnose(Evidence(case["folder"]), case["request_id"], WINDOW)
                self.assertEqual(report["outcome"], case["outcome"])
                self.assertEqual(report["citations"], case["citations"])
                self.assertEqual(report.get("next_record"), case["next_record"])
                self.assertEqual([(step["tool"], step.get("service")) for step in report["tool_trace"]], case["trace"])
                self.assertEqual(bool(report["warnings"]), case["warnings"])

    def test_every_citation_belongs_to_the_request_or_its_probe_window(self):
        for name, case in INCIDENTS.items():
            with self.subTest(incident=name):
                evidence = Evidence(case["folder"])
                report = diagnose(evidence, case["request_id"], WINDOW)
                own_events = {
                    event_id
                    for expected in case["logs"].values()
                    for event_id in expected
                }
                probes = set(case["probes"][2]) if case["probes"] else set()
                for cited in report["citations"]:
                    self.assertIn(cited, own_events | probes)

    def test_decoys_are_never_cited(self):
        decoys = {
            "invalid_request": {"E10", "E12", "E13", "E14", "E15", "HC10", "HC11", "HC12"},
            "service_outage": {"E20", "E23", "E25", "E26", "E27", "E29", "HC16", "HC19", "HC20", "HC21", "HC22"},
            "insufficient_evidence": {"E30", "E32", "E33", "E39", "HC35", "HC36", "HC37", "HC38"},
            "outage_evidence_removed": {"E20", "E23", "E25", "E26", "E27", "E29", "HC16", "HC19", "HC20", "HC21", "HC22"},
            "gateway_order_timeout": {"E40", "E42", "E43", "HC40", "HC41"},
            "full_chain_outage": {"E53", "E54", "HC51"},
            "window_boundary": {"E72", "HC72", "HC73"},
        }
        for name, ignored in decoys.items():
            case = INCIDENTS[name]
            with self.subTest(incident=name):
                report = diagnose(Evidence(case["folder"]), case["request_id"], WINDOW)
                self.assertEqual(set(report["citations"]) & ignored, set())

    def test_route_mismatch_is_reported(self):
        report = diagnose(Evidence(FIXTURES / "route_mismatch"), "request-600", WINDOW)
        self.assertEqual(
            report["warnings"],
            ["Order Service recorded a call to Inventory Service, but the route map lists Warehouse Service."],
        )

    def test_request_with_no_events_cites_nothing(self):
        report = diagnose(Evidence(ROOT / "incidents/service_outage"), "request-999", WINDOW)
        self.assertEqual(report["outcome"], "insufficient_evidence")
        self.assertEqual(report["citations"], [])
        self.assertIn("Gateway", report["next_record"])


class AgentLoopSelectionTests(unittest.TestCase):
    """The agent loop, driven by scripted model replies, returns the right records at each step."""

    def run_script(self, case, script):
        with patch("llm_agent.ask_model", side_effect=script):
            return investigate(case["folder"], case["request_id"], "test-model", WINDOW)

    def test_correct_path_reaches_expected_report_for_every_incident(self):
        for name, case in INCIDENTS.items():
            with self.subTest(incident=name):
                report = self.run_script(case, case["script"])
                final = report["decisions"][-1]
                self.assertEqual(final["action"], "finish")
                self.assertNotIn("rejected", final)
                self.assertEqual(report["outcome"], case["outcome"])
                self.assertEqual(report["citations"], case["citations"])
                self.assertEqual(report.get("next_record"), case["next_record"])
                self.assertEqual(report["verification"]["verified"], not case["warnings"])
                if case["warnings"]:
                    self.assertTrue(any("Evidence warning" in problem for problem in report["verification"]["problems"]))

    def test_each_tool_step_returns_exactly_the_expected_records(self):
        for name, case in INCIDENTS.items():
            with self.subTest(incident=name):
                report = self.run_script(case, case["script"])
                for step in report["decisions"]:
                    if step["action"] == "read_logs":
                        events = step["result"]["data"]
                        self.assertEqual(ids(events), case["logs"][step["arguments"]["service"]])
                        self.assertTrue(all(event["request_id"] == case["request_id"] for event in events))
                    elif step["action"] == "inspect_dependencies":
                        key = (step["arguments"]["service"], step["arguments"]["route"])
                        self.assertEqual(step["result"]["data"], case["routes"][key])
                    elif step["action"] == "check_endpoint_status":
                        self.assertEqual(ids(step["result"]["data"]), case["probes"][2])

    def test_early_finish_lists_the_specific_checks_still_needed(self):
        expectations = [
            ("service_outage", [read(GATEWAY)], {("read_logs", ORDER)}),
            ("service_outage", [read(GATEWAY), read(ORDER)],
             {("inspect_dependencies", ORDER), ("read_logs", INVENTORY), ("check_endpoint_status", INVENTORY)}),
            ("insufficient_evidence", [read(GATEWAY), read(ORDER), read(INVENTORY)],
             {("inspect_dependencies", ORDER), ("check_endpoint_status", INVENTORY)}),
            ("gateway_order_timeout", [read(GATEWAY)], {("inspect_dependencies", GATEWAY), ("read_logs", ORDER)}),
            ("full_chain_outage", [read(GATEWAY), inspect(GATEWAY), read(ORDER)],
             {("inspect_dependencies", ORDER), ("read_logs", INVENTORY), ("check_endpoint_status", INVENTORY)}),
        ]
        for name, path, needed in expectations:
            case = INCIDENTS[name]
            with self.subTest(incident=name, steps=len(path)):
                report = self.run_script(case, path + [finish(case["outcome"])] + case["script"])
                rejection = report["decisions"][len(path)]["rejected"]
                required = {(check["action"], check["arguments"]["service"]) for check in rejection["required_checks"]}
                self.assertEqual(required, needed)

    def test_wrong_outcome_is_rejected_with_the_supported_one(self):
        cases = [
            ("invalid_request", "inventory_failure", "invalid_request"),
            ("service_outage", "insufficient_evidence", "inventory_failure"),
            ("insufficient_evidence", "inventory_failure", "insufficient_evidence"),
            ("outage_evidence_removed", "inventory_failure", "insufficient_evidence"),
            ("gateway_order_timeout", "inventory_failure", "insufficient_evidence"),
            ("full_chain_outage", "insufficient_evidence", "inventory_failure"),
        ]
        for name, wrong, supported in cases:
            case = INCIDENTS[name]
            with self.subTest(incident=name, wrong=wrong):
                script = case["script"][:-1] + [finish(wrong), finish(case["outcome"])]
                report = self.run_script(case, script)
                self.assertEqual(report["decisions"][-2]["rejected"], {"supported_outcome": supported})
                self.assertEqual(report["outcome"], case["outcome"])

    def test_health_check_needs_an_observed_timeout_for_that_service_and_time(self):
        case = INCIDENTS["service_outage"]
        script = [
            read(GATEWAY), read(ORDER),
            probe(INVENTORY, "2026-11-23T14:00:20Z"),
            probe(ORDER, "2026-11-23T14:00:30Z"),
        ] + case["script"][2:]
        report = self.run_script(case, script)
        for step in report["decisions"][2:4]:
            self.assertIn("Read a timeout log", step["result"]["error"])
        self.assertTrue(report["verification"]["verified"])

    def test_reading_extra_services_adds_no_citations(self):
        case = INCIDENTS["invalid_request"]
        report = self.run_script(case, [read(GATEWAY), read(ORDER), read(INVENTORY), finish("invalid_request")])
        self.assertEqual(report["citations"], ["E11"])
        self.assertTrue(report["verification"]["verified"])
        for step in report["decisions"][1:3]:
            self.assertEqual(step["result"]["data"], [])

    def test_unknown_request_finishes_with_no_citations(self):
        case = dict(INCIDENTS["service_outage"], request_id="request-999")
        report = self.run_script(case, [read(GATEWAY), read(ORDER), finish("insufficient_evidence")])
        self.assertEqual(report["outcome"], "insufficient_evidence")
        self.assertEqual(report["citations"], [])
        self.assertTrue(report["verification"]["verified"])


if __name__ == "__main__":
    unittest.main()
