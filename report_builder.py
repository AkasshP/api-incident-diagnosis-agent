"""Build evidence-grounded reports and the rule-based reference diagnosis."""

from tools import GATEWAY, INVENTORY, ORDER, observation_key, service_key, utc_time


def latest_event(events, event_type, downstream=None):
    return next(
        (
            row for row in reversed(events)
            if row["event_type"] == event_type
            and (
                downstream is None
                or service_key(row.get("details", {}).get("downstream_service")) == service_key(downstream)
            )
        ),
        None,
    )


def diagnose(evidence, request_id, window_seconds):
    timeline = []

    def cite(record, claim):
        timestamp = record.get("timestamp") or record["checked_at"]
        evidence_id = record.get("event_id") or record["check_id"]
        timeline.append({"timestamp": timestamp, "claim": claim, "evidence_id": evidence_id})

    def report(outcome, summary, **details):
        timeline.sort(key=lambda entry: utc_time(entry["timestamp"]))
        return {
            "request_id": request_id,
            "outcome": outcome,
            "summary": summary,
            **details,
            "timeline": timeline,
            "citations": [entry["evidence_id"] for entry in timeline],
            "tool_trace": evidence.trace,
            "warnings": evidence.warnings,
        }

    def insufficient(summary, next_record):
        return report("insufficient_evidence", summary, next_record=next_record)

    def follow_timeout(service, downstream, route):
        expected = evidence.inspect_dependencies(service, route)
        if expected and service_key(expected) != service_key(downstream):
            evidence.warnings.append(
                f"{service} recorded a call to {downstream}, but the route map lists {expected}."
            )
        return evidence.read_logs(request_id, downstream)

    gateway_events = evidence.read_logs(request_id, GATEWAY)
    validation = next(
        (
            row for row in reversed(gateway_events)
            if row["event_type"] == "validation_error"
            and isinstance(row.get("details", {}).get("missing_field"), str)
            and row["details"]["missing_field"].strip()
        ),
        None,
    )
    if validation:
        field = validation["details"]["missing_field"]
        claim = f"API Gateway rejected the request because {field} was missing."
        cite(validation, claim)
        return report("invalid_request", claim, invalid_field=field)

    gateway_timeout = latest_event(gateway_events, "downstream_timeout", ORDER)
    route = "POST /orders"
    if gateway_timeout:
        gateway_claim = "API Gateway timed out waiting for Order Service."
        cite(gateway_timeout, gateway_claim)
        route = gateway_timeout["details"].get("route") or route
        order_events = follow_timeout(GATEWAY, ORDER, route)
    else:
        order_events = evidence.read_logs(request_id, ORDER)

    order_timeout = latest_event(order_events, "downstream_timeout", INVENTORY)
    if not order_timeout:
        if gateway_timeout:
            return insufficient(
                gateway_claim,
                f"An Order Service event for request {request_id} showing how it handled the request.",
            )
        return insufficient(
            "The available request events do not establish where the request failed.",
            f"A Gateway validation or downstream call event for request {request_id}.",
        )

    order_claim = "Order Service timed out waiting for Inventory Service."
    cite(order_timeout, order_claim)
    inventory_events = follow_timeout(
        ORDER, INVENTORY, order_timeout["details"].get("route") or route
    )
    handler_error = latest_event(inventory_events, "handler_error")
    if handler_error:
        cite(handler_error, "Inventory Service recorded a handler error for this request.")

    observations = evidence.check_endpoint_status(INVENTORY, order_timeout["timestamp"], window_seconds)
    for check in observations:
        if check["result"].casefold() == "timeout":
            cite(check, f"The {check['path']} probe timed out from {check['observed_from']}.")

    if not handler_error:
        return report(
            "insufficient_evidence",
            order_claim,
            next_record=f"An Inventory Service handler event for request {request_id}.",
            missing_observation="inventory_handler_entry",
        )
    return report(
        "inventory_failure",
        "Inventory Service is the likely failure location. The underlying cause is undetermined.",
        likely_failure_location=INVENTORY,
    )


class Observations:
    """Expose only completed tool results to the report builder."""

    def __init__(self):
        self.results = {}
        self.missing = {}
        self.trace = []
        self.warnings = []

    def lookup(self, action, arguments, fallback):
        key = observation_key(action, arguments)
        if key not in self.results:
            self.missing[key] = {"action": action, "arguments": arguments}
        return self.results.get(key, fallback)

    def read_logs(self, request_id, service):
        return self.lookup("read_logs", {"service": service}, [])

    def inspect_dependencies(self, service, route):
        if not isinstance(route, str):
            self.warnings.append(f"Invalid route in the {service} event.")
            return None
        return self.lookup("inspect_dependencies", {"service": service, "route": route}, None)

    def check_endpoint_status(self, service, incident_time, window_seconds):
        return self.lookup("check_endpoint_status", {"service": service, "incident_time": incident_time}, [])

    def build_report(self, request_id, window_seconds):
        self.missing.clear()
        self.warnings.clear()
        return diagnose(self, request_id, window_seconds)
