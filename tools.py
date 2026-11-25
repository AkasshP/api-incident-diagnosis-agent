"""Read saved incident evidence from a local folder."""

import json
from datetime import datetime, timezone
from pathlib import Path


GATEWAY = "API Gateway"
ORDER = "Order Service"
INVENTORY = "Inventory Service"


def service_key(name):
    return str(name).strip().casefold().replace("-", " ").replace("_", " ")


def utc_time(value):
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return timestamp.astimezone(timezone.utc) if timestamp.tzinfo else None
    except (AttributeError, ValueError, OverflowError):
        return None


class Evidence:
    def __init__(self, folder: Path):
        self.folder = folder
        self.trace = []
        self.warnings = []
        self._files = {}

    def _read(self, filename):
        if filename in self._files:
            return self._files[filename]

        result = None
        try:
            with (self.folder / filename).open(encoding="utf-8") as source:
                if filename.endswith(".jsonl"):
                    result = []
                    for number, line in enumerate(source, 1):
                        if not line.strip():
                            continue
                        try:
                            row = json.loads(line)
                            if not isinstance(row, dict):
                                raise ValueError("expected an object")
                            result.append(row)
                        except ValueError as error:
                            self.warnings.append(f"{filename} line {number}: {error}")
                else:
                    result = json.load(source)
                    expected_type = dict if filename == "dependencies.json" else list
                    if not isinstance(result, expected_type):
                        raise ValueError(f"expected a JSON {expected_type.__name__}")
        except (OSError, ValueError) as error:
            self.warnings.append(f"{filename}: {error}")
            result = None
        self._files[filename] = result
        return result

    def read_logs(self, request_id, service):
        self.trace.append({"tool": "read_logs", "request_id": request_id, "service": service})
        return self._request_events(request_id, service)

    def read_recent_errors(self, request_id):
        self.trace.append({"tool": "read_recent_errors", "request_id": request_id})
        events = self._request_events(request_id)
        failures = {"validation_error", "downstream_timeout", "handler_error"}
        return [row for row in reversed(events) if row["event_type"] in failures][:5]

    def _request_events(self, request_id, service=None):
        matches = []
        for row in self._read("logs.jsonl") or []:
            if row.get("request_id") != request_id:
                continue
            if service is not None and service_key(row.get("service")) != service_key(service):
                continue
            if (
                not isinstance(row.get("event_id"), str)
                or not row["event_id"].strip()
                or not utc_time(row.get("timestamp"))
                or not isinstance(row.get("event_type"), str)
                or not isinstance(row.get("details", {}), dict)
            ):
                self.warnings.append(
                    f"logs.jsonl: invalid event for {row.get('service')} and request {request_id}"
                )
                continue
            matches.append(row)
        return sorted(matches, key=lambda row: utc_time(row["timestamp"]))

    def inspect_dependencies(self, service, route):
        self.trace.append({"tool": "inspect_dependencies", "service": service, "route": route})
        if not isinstance(route, str):
            self.warnings.append(f"Invalid route in the {service} event.")
            return None
        for name, routes in (self._read("dependencies.json") or {}).items():
            if service_key(name) == service_key(service) and isinstance(routes, dict):
                downstream = routes.get(route)
                return downstream if isinstance(downstream, str) and downstream.strip() else None
        return None

    def check_endpoint_status(self, service, incident_time, window_seconds):
        self.trace.append({
            "tool": "check_endpoint_status",
            "service": service,
            "incident_time": incident_time,
            "window_seconds": window_seconds,
        })
        reference = utc_time(incident_time)
        if reference is None:
            return []
        observations = []
        for row in self._read("endpoint_status.json") or []:
            if not isinstance(row, dict):
                self.warnings.append("endpoint_status.json: expected a check object")
                continue
            if service_key(row.get("target_service")) != service_key(service):
                continue
            checked_at = utc_time(row.get("checked_at"))
            if checked_at is None or any(
                not isinstance(row.get(field), str) or not row[field].strip()
                for field in ("check_id", "observed_from", "path", "result")
            ):
                self.warnings.append(f"endpoint_status.json: invalid check for {service}")
                continue
            if abs((checked_at - reference).total_seconds()) <= window_seconds:
                observations.append(row)
        return sorted(observations, key=lambda row: utc_time(row["checked_at"]))


SERVICES = (GATEWAY, ORDER, INVENTORY)
TOOL_ARGUMENTS = {
    "read_recent_errors": (),
    "read_logs": ("service",),
    "inspect_dependencies": ("service", "route"),
    "check_endpoint_status": ("service", "incident_time"),
}
TOOLS = tuple(TOOL_ARGUMENTS)

def observation_key(action, arguments):
    value = arguments.get("route")
    if action == "check_endpoint_status":
        value = utc_time(arguments["incident_time"])
    return action, arguments.get("service"), value


def run_tool(evidence, request_id, window_seconds, action, arguments, observations):
    key = observation_key(action, arguments)
    if key in observations.results:
        raise ValueError("This tool call already completed. Use its earlier result.")
    commands = {
        "read_recent_errors": evidence.read_recent_errors,
        "read_logs": evidence.read_logs,
        "inspect_dependencies": evidence.inspect_dependencies,
        "check_endpoint_status": evidence.check_endpoint_status,
    }
    tool_args = dict(arguments)
    if action in ("read_logs", "read_recent_errors"):
        tool_args["request_id"] = request_id
    elif action == "check_endpoint_status":
        tool_args["window_seconds"] = window_seconds
    warning_offset = len(evidence.warnings)
    data = commands[action](**tool_args)
    observations.results[key] = data
    result = {"data": data}
    if warnings := evidence.warnings[warning_offset:]:
        result["warnings"] = warnings
    return result
