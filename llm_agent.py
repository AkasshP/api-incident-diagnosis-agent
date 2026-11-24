"""Let a local Ollama model investigate a failed request, then verify its report against the evidence.

The model decides which tool to call at each step, when to stop, and what the
outcome is. The program only runs the tools it asks for and returns their
results. Two guardrails keep the report grounded:

1. A conclusion is accepted only if the records the model has seen support it.
   Otherwise the model is told what is missing and keeps investigating.
2. Citations are taken from the records the model actually saw, never typed by
   the model, so every cited ID is real.

Finally, the report is compared with the independent rule-based diagnosis in agent.py.
"""

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

from agent import GATEWAY, INVENTORY, ORDER, diagnose, nonnegative_seconds
from evidence import Evidence, service_key, utc_time

OLLAMA_URL = "http://localhost:11434/api/chat"
DEFAULT_MODEL = "qwen2.5:7b"
MAX_STEPS = 12
SERVICES = [GATEWAY, ORDER, INVENTORY]
TOOLS = ["read_logs", "inspect_dependencies", "check_endpoint_status"]
OUTCOMES = ["invalid_request", "inventory_failure", "insufficient_evidence"]
SUPPORTING_EVENTS = {"validation_error", "downstream_timeout", "handler_error"}
# Phrases that claim why something failed. The evidence only shows where it failed.
CAUSE_CLAIM = re.compile(
    r"\b(due to|because|caused by|as a result of|owing to|result(?:ed|ing)? from|root cause|unhealthy|overload\w*)\b",
    re.IGNORECASE,
)

SYSTEM_PROMPT = f"""You are an incident investigator for a small web API. A client request failed
and you must find where it failed using only saved evidence. You cannot change or restart anything.

System: a client sends POST /orders to the API Gateway, which validates the request and calls the
Order Service, which calls the Inventory Service to reserve stock. Service names are exactly:
{", ".join(SERVICES)}.

Tools (the request ID is filled in for you):
- read_logs(service): this request's log events from one service.
- inspect_dependencies(service, route): which downstream service that service calls for a route.
- check_endpoint_status(service, incident_time): health checks of a service near a timestamp.
  Use the timestamp of the timeout you are investigating as incident_time.
The dependency map only shows which service would be called. Only a service's own logs show what
actually happened to this request, so follow the request hop by hop through each service's logs.
Only call a tool when its result could change your conclusion. When you have enough evidence, or no
tool can help further, call finish with outcome and summary, plus next_record when the outcome is
insufficient_evidence. The evidence IDs are attached to your report automatically.

Event types: validation_error (details.missing_field), downstream_timeout (details.downstream_service,
details.route), handler_error (the service failed while handling this request), request_received.

How to decide the outcome:
- invalid_request: the API Gateway logged a validation_error with a missing_field.
- inventory_failure: the Inventory Service logged a handler_error for this request. You must have read
  the Inventory Service logs. Health checks are extra support: they never prove a failure on their own,
  and they never cancel a handler_error you have already seen.
- insufficient_evidence: anything else. The summary states only what was observed, for example
  "Order Service timed out waiting for Inventory Service.", and does not name a failure location.
  Set next_record to a sentence describing a record you have NOT seen that would resolve it: which
  service, which kind of event, and the request ID. Never put an existing evidence ID in next_record.
Base the outcome on all of "Evidence seen so far", not only the latest tool result.
Say where the request failed, never why. Do not guess a root cause. The summary states only what the
evidence shows, for example "Inventory Service is the likely failure location. The underlying cause is
undetermined." For timeouts and failures, never use words such as "due to", "because", "caused by" or "unhealthy" in
the summary. An invalid request may name the missing field as the reason.

Reply with exactly one JSON object per turn: your short reasoning, then the action and its arguments."""

STEP_SCHEMA = {
    "type": "object",
    "properties": {
        "reasoning": {"type": "string"},
        "action": {"type": "string", "enum": TOOLS + ["finish"]},
        "service": {"type": "string"},
        "route": {"type": "string"},
        "incident_time": {"type": "string"},
        "outcome": {"type": "string", "enum": OUTCOMES},
        "summary": {"type": "string"},
        "next_record": {"type": "string"},
    },
    "required": ["reasoning", "action"],
}


def ask_model(model, messages):
    body = json.dumps({
        "model": model,
        "messages": messages,
        "format": STEP_SCHEMA,
        "stream": False,
        "options": {"temperature": 0, "seed": 0},
    }).encode()
    request = urllib.request.Request(OLLAMA_URL, body, {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=300) as response:
            return json.load(response)["message"]["content"]
    except urllib.error.HTTPError as error:
        raise RuntimeError(f"Ollama returned {error.code}: {error.read().decode(errors='replace')}")
    except urllib.error.URLError as error:
        raise RuntimeError(
            f"Cannot reach Ollama at {OLLAMA_URL} ({error.reason}). "
            f"Start it with `ollama serve` and download the model with `ollama pull {model}`."
        )


def record_id(record):
    return record.get("event_id") or record["check_id"]


def describe(record):
    """One-line note about a record, kept in the model's evidence notebook."""
    if "event_id" in record:
        details = record.get("details", {})
        extra = details.get("missing_field") or details.get("downstream_service")
        return f"{record['event_id']}: {record['service']} {record['event_type']}" + (f" ({extra})" if extra else "")
    return (
        f"{record['check_id']}: {record['target_service']} {record['path']} check from "
        f"{record['observed_from']}, result {record['result']}"
    )


def is_supporting(record):
    if "event_id" in record:
        return record["event_type"] in SUPPORTING_EVENTS
    return record["result"].casefold() == "timeout"


def cause_claim(summary, outcome):
    """The phrase in the summary that claims a cause, or None.

    An invalid request is exempt: the Gateway's validation error records why it was rejected.
    """
    if outcome == "invalid_request":
        return None
    match = CAUSE_CLAIM.search(summary or "")
    return match.group(0) if match else None


LOCATION_CLAIM = re.compile(r"\b(failure location|failed (?:at|in|within)|timed out at)\b", re.IGNORECASE)


def next_record_problem(next_record, notebook):
    """Why next_record does not describe a missing record, or None."""
    text = next_record.strip()
    if text in notebook or re.fullmatch(r"[A-Z]+\d+", text):
        return f"next_record {text!r} is an evidence ID. Describe the missing record in a sentence."
    if not any(service_key(service) in service_key(text) for service in SERVICES):
        return "next_record must name the service whose record is missing."
    return None


def unsupported_reason(outcome, notebook, services_read, status_checks):
    """Why the investigation does not yet support this outcome, or None if it does."""
    events = [record for record in notebook.values() if "event_id" in record]
    if outcome != "invalid_request":
        unread = [service for service in (GATEWAY, ORDER) if service not in services_read]
        if unread:
            return f"read the logs of {unread} before concluding; the dependency map does not show what happened."
    for record in events:
        if record["event_type"] != "downstream_timeout":
            continue
        downstream = record.get("details", {}).get("downstream_service")
        if downstream in SERVICES and downstream not in services_read:
            return f"{record['event_id']} shows a timeout calling {downstream}, but you have not read its logs."
        if downstream == INVENTORY and (INVENTORY, record["timestamp"]) not in status_checks:
            return (
                f"{record['event_id']} shows a timeout calling {INVENTORY}; check its health status "
                f"at incident_time {record['timestamp']} before concluding."
            )
    if outcome == "invalid_request" and not any(
        record["event_type"] == "validation_error" and service_key(record["service"]) == service_key(GATEWAY)
        for record in events
    ):
        return "invalid_request needs an API Gateway validation_error, and none has been seen."
    if outcome == "inventory_failure":
        if INVENTORY not in services_read:
            return "inventory_failure needs the Inventory Service logs, which you have not read."
        if not any(
            record["event_type"] == "handler_error" and service_key(record["service"]) == service_key(INVENTORY)
            for record in events
        ):
            return "inventory_failure needs an Inventory Service handler_error, and none has been seen."
    return None


def run_tool(evidence, request_id, step, window_seconds):
    """Run the tool the model chose. Return (result, evidence records it exposed)."""
    action, service = step["action"], step.get("service")
    if service not in SERVICES:
        return {"error": f"service must be one of {SERVICES}"}, []
    if action == "read_logs":
        events = evidence.read_logs(request_id, service)
        return {"events": events}, events
    if action == "inspect_dependencies":
        if not step.get("route"):
            return {"error": "route is required, for example POST /orders"}, []
        return {"downstream_service": evidence.inspect_dependencies(service, step["route"])}, []
    if not step.get("incident_time"):
        return {"error": "incident_time is required, for example the timeout's timestamp"}, []
    checks = evidence.check_endpoint_status(service, step["incident_time"], window_seconds)
    return {"checks": checks}, checks


def investigate(folder, request_id, model, window_seconds, log=None):
    evidence = Evidence(folder)
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"Investigate failed request {request_id}. Choose your first action."},
    ]
    decisions, notebook, services_read, status_checks, final = [], {}, set(), set(), None

    def reject(number, step, problem):
        messages.append({"role": "user", "content": f"Finish rejected: {problem} Continue investigating or finish again."})
        decisions.append({"step": number, "action": "finish", "reasoning": step.get("reasoning"), "rejected": problem})
        if log:
            log(f"  rejected: {problem}")

    for number in range(1, MAX_STEPS + 1):
        reply = ask_model(model, messages)
        messages.append({"role": "assistant", "content": reply})
        try:
            step = json.loads(reply)
            if not isinstance(step, dict) or step.get("action") not in TOOLS + ["finish"]:
                raise ValueError("expected an object with a valid action")
        except ValueError as error:
            messages.append({"role": "user", "content": f"Invalid reply ({error}). Reply with one JSON action."})
            decisions.append({"step": number, "error": f"invalid model reply: {error}"})
            continue

        arguments = {key: step[key] for key in ("service", "route", "incident_time") if step.get(key)}
        if log:
            shown = ", ".join(f"{key}={value!r}" for key, value in arguments.items())
            log(f"Step {number}: {step['action']}({shown})\n  why: {step['reasoning']}")

        if step["action"] == "finish":
            missing = [key for key in ("outcome", "summary") if not step.get(key)]
            if step.get("outcome") == "insufficient_evidence" and not step.get("next_record"):
                missing.append("next_record")
            if missing:
                reject(number, step, f"the reply is missing {missing}.")
                continue
            cause = cause_claim(step["summary"], step["outcome"])
            if cause:
                reject(number, step, f"the summary claims a cause ({cause!r}). State where the request failed, not why.")
                continue
            if step["outcome"] == "insufficient_evidence":
                claim = LOCATION_CLAIM.search(step["summary"])
                if claim:
                    reject(number, step, f"the summary claims a failure location ({claim.group(0)!r}), "
                                         "but the outcome is insufficient_evidence. State only what was observed.")
                    continue
                problem = next_record_problem(step["next_record"], notebook)
                if problem:
                    reject(number, step, problem)
                    continue
            reason = unsupported_reason(step["outcome"], notebook, services_read, status_checks)
            if reason:
                reject(number, step, reason)
                continue
            final = step
            decisions.append({
                "step": number, "action": "finish", "reasoning": step["reasoning"], "outcome": step["outcome"],
            })
            if log:
                log(f"  outcome: {step['outcome']}")
            break

        result, records = run_tool(evidence, request_id, step, window_seconds)
        if step["action"] == "read_logs" and "error" not in result:
            services_read.add(step["service"])
        if step["action"] == "check_endpoint_status" and "error" not in result:
            status_checks.add((step["service"], step["incident_time"]))
        for record in records:
            notebook[record_id(record)] = record
        decisions.append({
            "step": number,
            "action": step["action"],
            "arguments": arguments,
            "reasoning": step["reasoning"],
            "result": result,
        })
        if log:
            log(f"  result: {json.dumps(result)[:300]}")
        notes = "\n".join(f"- {describe(record)}" for record in notebook.values()) or "- none yet"
        messages.append({
            "role": "user",
            "content": f"Tool result: {json.dumps(result)}\n\nEvidence seen so far:\n{notes}",
        })

    if final is None:
        final = {
            "outcome": "insufficient_evidence",
            "summary": f"The model did not reach a supported conclusion within {MAX_STEPS} steps.",
        }

    supporting = sorted(
        (record for record in notebook.values() if is_supporting(record)),
        key=lambda record: utc_time(record.get("timestamp") or record["checked_at"]),
    )
    report = {
        "request_id": request_id,
        "model": model,
        "outcome": final.get("outcome"),
        "summary": final.get("summary"),
        "citations": [record_id(record) for record in supporting],
        "evidence": [describe(record) for record in supporting],
        "decisions": decisions,
        "warnings": evidence.warnings,
    }
    if final.get("next_record"):
        report["next_record"] = final["next_record"]
    report["verification"] = verify(report, folder, request_id, window_seconds)
    return report


def verify(report, folder, request_id, window_seconds):
    """Compare the model's report with the independent rule-based diagnosis."""
    reference = diagnose(Evidence(folder), request_id, window_seconds)
    problems = []
    if report["outcome"] != reference["outcome"]:
        problems.append(f"outcome {report['outcome']!r}, rule-based check says {reference['outcome']!r}")
    cause = cause_claim(report["summary"], report["outcome"])
    if cause:
        problems.append(f"summary claims a cause the evidence cannot show ({cause!r})")
    if reference.get("next_record"):
        wanted = next(s for s in SERVICES if service_key(s) in service_key(reference["next_record"]))
        if service_key(wanted) not in service_key(report.get("next_record", "")):
            problems.append(
                f"next_record {report.get('next_record')!r} does not ask for a {wanted} record "
                f"(rule-based check asks for: {reference['next_record']!r})"
            )
    if report["outcome"] == "insufficient_evidence" and LOCATION_CLAIM.search(report["summary"] or ""):
        problems.append("summary names a failure location although the evidence is insufficient")
    cited, expected = set(report["citations"]), set(reference["citations"])
    if cited != expected:
        missing, extra = sorted(expected - cited), sorted(cited - expected)
        problems.append(f"citations differ from rule-based check (missing {missing}, extra {extra})")
    return {
        "verified": not problems,
        "rule_based_outcome": reference["outcome"],
        "rule_based_citations": reference["citations"],
        "problems": problems,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("incident_folder", type=Path)
    parser.add_argument("request_id")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--health-window-seconds", type=nonnegative_seconds, default=300)
    parser.add_argument("--quiet", action="store_true", help="do not print each decision as it happens")
    args = parser.parse_args()
    log = None if args.quiet else (lambda line: print(line, file=sys.stderr, flush=True))
    try:
        report = investigate(args.incident_folder, args.request_id, args.model, args.health_window_seconds, log)
    except RuntimeError as error:
        parser.exit(1, f"error: {error}\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
