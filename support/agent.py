"""Diagnose a failed request using saved incident evidence."""

import argparse
import json
import sys
from pathlib import Path

from .demo import advisor_parser, recent_parser, replay, select_recent
from guardrails import nonnegative_seconds
from report_builder import diagnose
from tools import Evidence


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
