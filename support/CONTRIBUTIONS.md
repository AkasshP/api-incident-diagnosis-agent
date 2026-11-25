# Contributions

## Akassh Paramasivan

- Implemented the [CLI and diagnostic report](agent.py), including evidence-driven investigation and next-record requests.
- Implemented [read-only evidence tools](../tools.py) for request logs, dependencies, and time-filtered health observations.
- Documented the [CLI and evidence formats](../README.md#1-set-up-the-project) and verified the diagnosis paths with temporary evidence.
- Added [recent-request selection and offline telemetry replay](demo.py), including candidate scoring and tie reporting. Verified both commands with temporary scenarios.
- Hardened [the investigation loop](../llm_agent.py) and added [regression checks](../tests/test_llm_agent.py) for validation, retries, strict finish checks, and independent verification.
- Separated evidence tools, guardrails, report building, and verification into modules. Added input validation, bounded model retries, and finish checks that require supporting observations. [Guardrails](../guardrails.py) · [Report builder](../report_builder.py) · [Verifier](../verifier.py)
- Built the Streamlit dashboard and local order simulator. The dashboard supports demo and custom HTTP requests, shows request history, and lets students inspect tool arguments, results, cited evidence, and verification status. [Dashboard](../streamlit_app.py) · [Demo API](../dashboard/orders_api.py)
- Verified the saved invalid-request and insufficient-evidence cases with `qwen3:8b`. The invalid request stopped at Gateway and cited `E11`. The insufficient-evidence case cited `E31` and asked for the matching Inventory handler record. Added a walkthrough for students to reproduce and explain the results. [Saved incident walkthrough](../README.md#2-run-the-three-saved-incidents) · [Evaluation results](../results/evaluation_results.json)

## Ramya Karuthappandian

My part was the evidence the agent investigates and the tests that show whether it can be trusted. I later added the language-model agent, so a model makes the decisions instead of fixed rules.

- **Built the incident cases** in [incidents/](../incidents/). There are three demo cases where the correct answer is known in advance: an invalid request (400, missing `item_id`, `E11`), an Inventory outage (504, `E21`, `E22`, `HC17`, `HC18`), and a timeout with too little evidence to say where it failed (504, `E31`). Each case also contains decoys: another request's events, a stale health check, and a check of the wrong service. If the agent cites a decoy, its filtering is broken.
- **Created the evidence-removal test** in [tests/fixtures/outage_evidence_removed/](../tests/fixtures/outage_evidence_removed/). It is the outage case with `E22`, `HC17` and `HC18` removed. A trustworthy agent should become less certain when its proof disappears, and this case checks that it changes to "insufficient evidence" while still citing `E21`.
- **Wrote the replay scenarios** for the Telemetry Advisor in [replay/](replay/). They are two situations that look identical in the logs: the request never reached Inventory, or it reached Inventory but no response came back. The true answers are kept in a separate file so the agent never sees them.
- **Built the evaluation** in [evaluate.py](../evaluate.py). It holds the expected outcome, citations and tool calls for every case and checks the agent against them. The results are in [results/history.md](../results/history.md), where all 9 checks pass.
- **Added the language-model agent** in [llm_agent.py](../llm_agent.py). The original implementation used the local Ollama model `qwen3:8b`. The model chooses which tool to call at each step and when to stop, and explains each choice. The original guardrails checked the observed evidence, service coverage, and unsupported cause claims. Citations come only from records the model actually saw. Akassh's rule-based agent then checks the model's answer independently.
- **Recorded what went wrong during testing.** The model made up incomplete citations, treated the dependency map as proof, changed a correct answer after seeing health checks, and guessed a root cause. Every check passed once while the output still had mistakes. Each problem and its fix is written up in [results/history.md](../results/history.md).
- **Drew the original architecture diagram** showing the decision loop, the guardrails, the report and the verification step.
