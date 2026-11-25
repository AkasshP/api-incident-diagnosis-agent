"""Select a saved failed request or compare candidate telemetry in offline replay."""

import argparse
import json
import sys
from itertools import combinations
from pathlib import Path

from tools import utc_time


def read_object(path):
    with path.open(encoding="utf-8") as source:
        data = json.load(source)
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a JSON object")
    return data


def object_rows(document, key):
    rows = document.get(key)
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError(f"{key} must be an array of objects")
    return rows


def required_text(row, key):
    value = row.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be a nonempty string")
    return value


def recent_parser():
    parser = argparse.ArgumentParser(prog="python3 -m support.agent recent", description="Browse saved failed requests.")
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--select", type=int, metavar="ROW")
    return parser


def select_recent(manifest, selection):
    requests = object_rows(read_object(manifest), "requests")
    for row in requests:
        for key in ("route", "request_id", "incident_folder"):
            required_text(row, key)
        if utc_time(row.get("timestamp")) is None:
            raise ValueError("each request must have a timezone-aware timestamp")
        status = row.get("http_status")
        if type(status) is not int or not 400 <= status <= 599:
            raise ValueError("each request must have an HTTP status between 400 and 599")
    requests.sort(key=lambda row: utc_time(row["timestamp"]), reverse=True)

    if selection is None:
        print("Row  Route  HTTP status  Request ID")
        for index, row in enumerate(requests, 1):
            print(f"{index}    {row['route']}  {row['http_status']}  {row['request_id']}")
        if not requests or not sys.stdin.isatty():
            return None
        print("Select a row (Enter to exit): ", file=sys.stderr, end="", flush=True)
        entered = input().strip()
        if not entered:
            return None
        selection = int(entered)
    if not 1 <= selection <= len(requests):
        raise ValueError("selected row is outside the recent request list")
    row = requests[selection - 1]
    return manifest.parent / row["incident_folder"], row["request_id"]


def advisor_parser():
    parser = argparse.ArgumentParser(prog="python3 -m support.agent advisor", description="Compare candidate telemetry offline.")
    parser.add_argument("incident_folder", type=Path)
    parser.add_argument("request_id")
    parser.add_argument("replay_file", type=Path)
    parser.add_argument("--missing-observation", required=True, choices=["inventory_handler_entry"])
    parser.add_argument("--budget", required=True, type=int, choices=[1])
    return parser


def records_signature(records):
    if not isinstance(records, list) or any(not isinstance(record, dict) for record in records):
        raise ValueError("replay observations must be arrays of record objects")
    # Replay records are normalized observations. Their order is immaterial.
    return sorted(json.dumps(record, sort_keys=True, allow_nan=False) for record in records)


def replay(path, missing_observation):
    document = read_object(path)
    candidates = object_rows(document, "candidates")
    scenarios = object_rows(document, "scenarios")
    names = [required_text(candidate, "event_name") for candidate in candidates]
    ids = [required_text(scenario, "id") for scenario in scenarios]
    if not names or len(set(names)) != len(names):
        raise ValueError("replay needs at least one candidate with a unique event name")
    if len(ids) < 2 or len(set(ids)) != len(ids):
        raise ValueError("replay needs at least two scenarios with unique IDs")

    visible = {}
    projected = {}
    for scenario, scenario_id in zip(scenarios, ids):
        visible[scenario_id] = records_signature(scenario.get("visible_records"))
        records_by_candidate = scenario.get("candidate_records")
        if not isinstance(records_by_candidate, dict) or set(records_by_candidate) != set(names):
            raise ValueError(f"{scenario_id}: supply a projection for every candidate")
        projected[scenario_id] = {
            name: records_signature(records) for name, records in records_by_candidate.items()
        }

    baseline_pairs = [(left, right) for left, right in combinations(ids, 2) if visible[left] == visible[right]]
    scores = []
    for candidate, name in zip(candidates, names):
        separated = [
            [left, right] for left, right in baseline_pairs
            if projected[left][name] != projected[right][name]
        ]
        scores.append({
            "event_name": name,
            "instrumentation_location": required_text(candidate, "instrumentation_location"),
            "expected_benefit": required_text(candidate, "expected_benefit"),
            "score": len(separated),
            "separated_pairs": separated,
            "remaining_indistinguishable_pairs": len(baseline_pairs) - len(separated),
        })
    scores.sort(key=lambda candidate: candidate["score"], reverse=True)
    best_score = scores[0]["score"]
    best = [candidate["event_name"] for candidate in scores if candidate["score"] == best_score]
    return {
        "missing_observation": missing_observation,
        "event_budget": 1,
        "baseline_indistinguishable_pairs": len(baseline_pairs),
        "candidates": scores,
        "recommended_events": best if best_score else [],
        "top_score_ties": best if len(best) > 1 else [],
        "remaining_uncertainty": (
            "Replay cannot establish which scenario occurred in the original incident. "
            "The scores assume candidate records would be observed reliably."
        ),
    }
