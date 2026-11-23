"""Diagnose a failed request using saved incident evidence."""

import argparse
import json
import sys
from pathlib import Path

from demo import advisor_parser, recent_parser, replay, select_recent
from evidence import Evidence, service_key, utc_time

GATEWAY = "API Gateway"
ORDER = "Order Service"
INVENTORY = "Inventory Service"


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


def nonnegative_seconds(value):
    seconds = int(value)
    if seconds < 0:
        raise argparse.ArgumentTypeError("window must be nonnegative")
    return seconds


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("incident_folder", type=Path)
    parser.add_argument("request_id")
    parser.add_argument("--health-window-seconds", type=nonnegative_seconds, default=300)
    arguments = sys.argv[1:]
    command = arguments[0] if arguments else None
    if command in ("recent", "advisor"):
        parser = recent_parser() if command == "recent" else advisor_parser()
        parser.add_argument("--health-window-seconds", type=nonnegative_seconds, default=300)
        args = parser.parse_args(arguments[1:])
    else:
        parser.epilog = "Additional commands: recent --help and advisor --help."
        args = parser.parse_args()
    try:
        if command == "recent":
            selected = select_recent(args.manifest, args.select)
            if selected is None:
                return
            folder, request_id = selected
        else:
            folder, request_id = args.incident_folder, args.request_id
        result = diagnose(Evidence(folder), request_id, args.health_window_seconds)
        if command == "advisor":
            if result.get("missing_observation") != args.missing_observation:
                parser.error("advisor requires an unresolved Order-to-Inventory timeout")
            result = {"diagnosis": result, "advisor": replay(args.replay_file, args.missing_observation)}
        print(json.dumps(result, indent=2))
    except (OSError, ValueError, EOFError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
