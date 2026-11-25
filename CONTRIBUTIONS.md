# Contributions

## Akassh Paramasivan

- Implemented the [CLI and diagnostic report](agent.py), including evidence-driven investigation and next-record requests.
- Implemented [read-only evidence tools](evidence.py) for request logs, dependencies, and time-filtered health observations.
- Documented the [CLI and evidence formats](README.md#run-locally) and verified the diagnosis paths with temporary evidence.
- Added [recent-request selection and offline telemetry replay](demo.py), including candidate scoring and tie reporting. Verified both commands with temporary scenarios.

## Ramya Karuthappandian

My part was the evidence the agent investigates and the tests that show whether it can be trusted. I later added the language-model agent, so a model makes the decisions instead of fixed rules.

- **Built the incident cases** in [incidents/](incidents/). There are three demo cases where the correct answer is known in advance: an invalid request (400, missing `item_id`, `E11`), an Inventory outage (504, `E21`, `E22`, `HC17`, `HC18`), and a timeout with too little evidence to say where it failed (504, `E31`). Each case also contains decoys: another request's events, a stale health check, and a check of the wrong service. If the agent cites a decoy, its filtering is broken.
- **Created the evidence-removal test** in [incidents/outage-evidence-removed/](incidents/outage-evidence-removed/). It is the outage case with `E22`, `HC17` and `HC18` removed. A trustworthy agent should become less certain when its proof disappears, and this case checks that it changes to "insufficient evidence" while still citing `E21`.
- **Wrote the replay scenarios** for the Telemetry Advisor in [replay/](replay/). They are two situations that look identical in the logs: the request never reached Inventory, or it reached Inventory but no response came back. The true answers are kept in a separate file so the agent never sees them.
- **Built the evaluation** in [evaluate.py](evaluate.py). It holds the expected outcome, citations and tool calls for every case and checks the agent against them. The results are in [evaluation/results.md](evaluation/results.md), where all 9 checks pass.
- **Added the language-model agent** in [llm_agent.py](llm_agent.py). It uses a free local Ollama model (`qwen2.5:7b`). The model chooses which tool to call at each step and when to stop, and explains each choice. The program adds guardrails: a conclusion is rejected if the evidence does not support it, if a service was skipped, or if the summary guesses a cause. Citations come only from records the model actually saw. Akassh's rule-based agent then checks the model's answer independently.
- **Recorded what went wrong during testing.** The model made up incomplete citations, treated the dependency map as proof, changed a correct answer after seeing health checks, and guessed a root cause. Every check passed once while the output still had mistakes. Each problem and its fix is written up in [evaluation/results.md](evaluation/results.md).
- **Drew the [architecture diagram](architecture-diagram.png)** showing the decision loop, the guardrails, the report and the verification step.

