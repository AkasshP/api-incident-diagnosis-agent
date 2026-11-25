# API Incident Diagnosis Agent

A Python agent that investigates failed order requests from saved logs and simulated service data. It follows a request ID through Gateway, Order, and Inventory, cites the records that support its conclusion, and names the next record to look for when evidence is missing. Run the agent from the terminal or follow its decisions in the Streamlit dashboard.

Built by Ramya Karuthappandian and Akassh Paramasivan as a four-week project.

## Student walkthrough

Start with the saved fixtures to verify the three required cases. Use Streamlit to show how the same investigation looks with a clickable trace.

### Verify the saved incidents

With Ollama running and `qwen3:8b` installed, use:

```sh
python3 llm_agent.py incidents/invalid_request request-100
python3 llm_agent.py incidents/service_outage request-200
python3 llm_agent.py incidents/insufficient_evidence request-123
```

Check the outcome and citations against the [expected reports](#what-the-diagnosis-looks-like). In each report, inspect `tool_trace` for the reads and `verification.verified` for the independent evidence check.

### Show the same cases in Streamlit

1. Follow the [dashboard setup](#open-the-streamlit-dashboard). Choose **Invalid request**, click **Send request**, then click **Investigate**. Expect HTTP 400, a Gateway log lookup, and a verified diagnosis that names the missing `item_id`.
2. Switch to **Inventory outage** and send another request. Expect HTTP 504. Click a tool node in the trace and check that the Order logs show a timeout and the Inventory logs show a handler error for the same request. The report should identify Inventory as the likely failure location.
3. Choose **Missing Inventory evidence** and repeat. Expect HTTP 504 and `insufficient_evidence`. The report should ask for an Inventory handler record. **Verified** here means the verifier confirmed what the available evidence supports, including that the failure location is still unknown.

The dashboard creates a fresh request ID and evidence records each time. The evaluator uses the fixed IDs in the saved fixtures:

```sh
python3 evaluate.py --llm
python3 -m unittest discover -v
```

Expect **9/9 evaluation checks** and **9 regression tests** to pass.

### Follow one request through the code

1. **Send the request.** [`streamlit_app.py`](streamlit_app.py) calls [`dashboard/http_client.py`](dashboard/http_client.py). The local demo in [`dashboard/orders_api.py`](dashboard/orders_api.py) returns an HTTP response and saves its evidence under the same request ID.
2. **Decide what to read.** Start at `investigate()` in [`llm_agent.py`](llm_agent.py). It validates the input, asks Ollama for one action, runs that action, and gives the result back to the model. [`tools.py`](tools.py) reads the saved evidence. [`guardrails.py`](guardrails.py) rejects invalid tool arguments and finishes that need more evidence.
3. **Build and check the report.** [`report_builder.py`](report_builder.py) builds the timeline and cites records the agent actually read. [`verifier.py`](verifier.py) reads the saved bundle again and checks the outcome, citations, timeline, and next record. If the checks disagree, it returns `verified: false` with an explanation.

## How a request moves through the system

In the simulated system, API Gateway validates `POST /orders`, Order Service handles the request, and Inventory Service reserves stock. The three services are represented by saved evidence. The Streamlit demo can also generate fresh evidence by sending a request to a local simulator.

The agent starts with the Gateway logs. A validation error with a missing field is enough to explain the rejection. For a timeout, the agent checks the route map and follows the call recorded in the logs, looking for the same request ID in the downstream service. An Order-to-Inventory timeout also prompts a lookup for nearby readiness checks. Each readiness timeout describes what the probe observed from its own vantage point.

## What the diagnosis looks like

Each report includes a short timeline with evidence IDs and a trace of the files the agent consulted. These are the expected results for the saved fixtures.

| Demo | Expected report |
|---|---|
| Invalid request · HTTP 400 | Missing `item_id`. Cite Gateway validation event `E11` and finish without a downstream lookup. |
| Inventory outage · HTTP 504 | Inventory is the likely failure location. Cite Order's call timeout `E21` and Inventory's matching handler error `E22`. Cite readiness timeouts `HC17` and `HC18` as supporting observations. The underlying cause remains undetermined. |
| Insufficient evidence · HTTP 504 | Order timed out waiting for Inventory. Cite `E31` and request an Inventory-side event for the same request. |

## Run locally

Use Python 3.9 or later. The CLI prints a JSON report and uses only the standard library.

```sh
python3 -m support.agent path/to/incident request-123 --health-window-seconds 300
```

### Run the LLM agent

`llm_agent.py` uses a free local model through [Ollama](https://ollama.com). The model decides which tool to call at each step, when to stop, and what the outcome is. It prints each decision and its reason as it goes.

![LLM agent architecture: decision loop, guardrails, report builder, and independent verification](docs/diagrams/architecture.png)

```sh
ollama pull qwen3:8b
python3 llm_agent.py incidents/service_outage request-200
```

The diagram shows the investigation flow. The current model is `qwen3:8b`, as configured in `config.json`. The CLI validates the incident folder, request ID, model tag, and health window before contacting Ollama. Each decision records the selected action, arguments, reason, and tool result or error. Tool arguments can be corrected on the next turn. Repeated calls are sent back to the prior observation, transient model requests get up to two attempts per turn, and the investigation is limited to 12 decisions.

A finish is accepted once the observations cover the evidence needed for the proposed outcome. The report builder uses those observations to generate the summary, timeline, and citations. An independent rule-based check then rereads the incident folder and compares the report with the saved evidence. See `verification.verified` and `verification.problems`. An unverified report still prints as JSON and exits with status 1. The defaults are in `config.json`. Use `--model` to select another installed Ollama model.

### Open the Streamlit dashboard

With Ollama running and `qwen3:8b` installed, run:

```sh
python3 -m pip install -r requirements.txt
python3 -m streamlit run streamlit_app.py
```

Open [the local dashboard](http://127.0.0.1:8501). Choose **Demo API** for the sample scenarios or **Custom endpoint** to paste a URL and choose a method. Then click **Send request**. The console shows response status, latency, request ID, and body. It keeps request history for the current browser session.

The local demo saves an evidence bundle automatically. For your own endpoint, provide a local incident folder and the request ID. A request ID from the response header or JSON body is filled in when available. Click **Investigate** to run the same Ollama agent as the CLI. The trace shows how many evidence tools ran, which decisions were rejected or retried, what each tool returned, and whether independent verification passed. Click a trace node to see its details, or open **Cited evidence** to review the timeline.

### Run the evaluation

```sh
python3 evaluate.py        # rule-based agent and replay advisor
python3 evaluate.py --llm --json-output results/evaluation_results.json
python3 -m unittest discover -v
```

## Repository layout

| Path | Purpose |
|---|---|
| `llm_agent.py` | Ollama decision loop, retry limits, and CLI |
| `tools.py` | Read-only recent errors, request logs, dependencies, and health checks |
| `guardrails.py` | Input, tool argument, and finish validation |
| `report_builder.py` | Diagnosis, timeline, and citations from observed evidence |
| `verifier.py` | Independent comparison against a fresh read of the evidence |
| `config.json`, `requirements.txt` | Runtime defaults and Python dependency information |
| `incidents/` | Service outage, invalid request, and insufficient-evidence bundles |
| `evaluate.py`, `tests/`, `results/` | Evaluation, regression checks and evidence-removal fixture, and recorded results |
| `docs/diagrams/architecture.png` | Final architecture diagram |
| `streamlit_app.py`, `dashboard/` | Local dashboard, HTTP client, and demo endpoint |
| `support/` | Baseline CLI, recent-request manifest, Telemetry Advisor, and team contributions |

Point the CLI at a folder containing the evidence files below. Use timezone-aware ISO 8601 timestamps such as `2026-11-23T14:00:00Z`. By default, a health check can be up to five minutes before or after the Order timeout.

| Evidence | Format |
|---|---|
| `logs.jsonl` | One event per line with `event_id`, `timestamp`, `request_id`, `service`, `event_type`, and `details`. Use `API Gateway`, `Order Service`, and `Inventory Service` as service names. |
| `dependencies.json` | A JSON object mapping each calling service to its route-to-downstream map, for example `{"API Gateway":{"POST /orders":"Order Service"},"Order Service":{"POST /orders":"Inventory Service"}}`. |
| `endpoint_status.json` | An array of checks with `check_id`, `checked_at`, `target_service`, `observed_from`, `path`, and `result`. Set `result` to `timeout` for a timed-out probe. |

For Gateway validation, use `event_type: validation_error` with `details.missing_field`. For a Gateway or Order timeout, use `event_type: downstream_timeout` with `details.downstream_service` and `details.route`. For the matching Inventory handler error, use `event_type: handler_error`.

The report's `outcome` is `invalid_request`, `inventory_failure`, or `insufficient_evidence`. Each timeline entry names its evidence ID, and `tool_trace` records the calls in execution order. Unresolved reports include `next_record`. Read and parse failures appear in `warnings` alongside any diagnosis the available evidence supports.

### Browse recent failed requests

```sh
python3 -m support.agent recent support/recent.json
python3 -m support.agent recent support/recent.json --select 1
```

The first command lists rows newest first and prompts for a row in an interactive terminal. The second opens that row's evidence bundle directly and prints its diagnosis. Paths in `incident_folder` are relative to the manifest directory unless absolute. Supply the request's actual HTTP status in the manifest.

```json
{
  "requests": [
    {
      "timestamp": "2026-11-23T14:00:00Z",
      "route": "POST /orders",
      "http_status": 504,
      "request_id": "request-123",
      "incident_folder": "../incidents/insufficient_evidence"
    }
  ]
}
```

If the only matching record is the Order timeout, the diagnosis looks like this.

```json
{
  "outcome": "insufficient_evidence",
  "summary": "Order Service timed out waiting for Inventory Service.",
  "next_record": "An Inventory Service handler event for request request-123.",
  "citations": ["E31"]
}
```

### Optional offline replay

The Telemetry Advisor is an optional command in the rule-based CLI. It compares which single extra observation would distinguish two saved scenarios. In the included replay, `handler_entered` scores 1, while the Gateway timeout and readiness check each score 0.

```sh
python3 -m support.agent advisor incidents/insufficient_evidence request-123 support/replay/replay.json \
  --missing-observation inventory_handler_entry --budget 1
```

## Team contributions

- **Akassh Paramasivan** implemented the baseline CLI and evidence tools, hardened the agent's input validation, retry limits, finish checks, report generation, and verifier, and built the Streamlit dashboard with demo and custom requests, request history, a clickable investigation trace, and a cited evidence timeline.
- **Ramya Karuthappandian** built the saved incident bundles, evidence-removal fixture, offline replay data, and evaluator. She also implemented the first Ollama investigation loop and created the original architecture diagram.

See [individual contributions](support/CONTRIBUTIONS.md) and the [recorded evaluation](results/evaluation_results.json).
