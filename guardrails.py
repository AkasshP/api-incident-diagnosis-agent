"""Validate incident inputs, model actions, tool arguments, and proposed finishes."""

import argparse
import json
from pathlib import Path

from tools import SERVICES, TOOLS, service_key, utc_time


def nonnegative_seconds(value):
    seconds = int(value)
    if seconds < 0:
        raise argparse.ArgumentTypeError("window must be nonnegative")
    return seconds


def validate_input(folder, request_id, model, window_seconds):
    folder = Path(folder)
    if not folder.is_dir():
        raise ValueError(f"Incident folder does not exist or is not a directory: {folder}")
    if not isinstance(request_id, str) or not request_id.strip():
        raise ValueError("Request ID must be nonempty.")
    if not isinstance(model, str) or not model.strip():
        raise ValueError("Ollama model tag must be nonempty.")
    if type(window_seconds) is not int or window_seconds < 0:
        raise ValueError("Health window must be a nonnegative integer.")
    return folder


def parse_step(reply):
    step = json.loads(reply)
    if not isinstance(step, dict) or step.get("action") not in (*TOOLS, "finish"):
        raise ValueError("Choose a valid tool or finish.")
    reason = step.get("reasoning")
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("Give a short reason for the action.")
    return step


def validate_tool(action, arguments, observations):
    if action == "read_recent_errors":
        return
    service = arguments.get("service")
    if service not in SERVICES:
        raise ValueError(f"service must be one of {', '.join(SERVICES)}.")
    if action == "inspect_dependencies":
        route = arguments.get("route")
        if not isinstance(route, str) or not route.strip():
            raise ValueError("Supply a nonempty route such as POST /orders.")
    if action == "check_endpoint_status":
        incident_time = utc_time(arguments.get("incident_time"))
        if incident_time is None:
            raise ValueError("Supply a timezone-aware incident_time from the timeout log.")
        if not any(
            event["event_type"] == "downstream_timeout"
            and service_key(event.get("details", {}).get("downstream_service")) == service_key(service)
            and utc_time(event["timestamp"]) == incident_time
            for (tool, _, _), records in observations.results.items()
            if tool == "read_logs"
            for event in records
        ):
            raise ValueError("Read a timeout log for this downstream service and timestamp first.")


def check_finish(outcome, observations, request_id, window_seconds):
    report = observations.build_report(request_id, window_seconds)
    if observations.missing:
        return report, {"required_checks": list(observations.missing.values())}
    if outcome != report["outcome"]:
        return report, {"supported_outcome": report["outcome"]}
    return report, None
