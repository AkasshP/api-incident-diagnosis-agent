"""Render HTTP responses, investigation traces, and cited evidence."""

import json

import streamlit as st


def show_http(response):
    status, elapsed = st.columns(2)
    status.metric("HTTP status", response.status)
    elapsed.metric("Response time", f"{response.latency_ms:.1f} ms")
    body, request = st.tabs(["Response body", "Request details"])
    with body:
        try:
            parsed = json.loads(response.text)
        except ValueError:
            st.code(response.text or "(empty)", language="text")
        else:
            st.json(parsed, expanded=2)
        if response.truncated:
            st.caption("Response preview limited to 64 KiB.")
    with request:
        st.code(f"{response.method} {response.url}", language="text")
        if response.request_id:
            st.caption("Request ID")
            st.code(response.request_id, language="text")


def step_label(step):
    label = step["action"].replace("_", " ").capitalize()
    service = step.get("arguments", {}).get("service")
    if service:
        label += f" · {service}"
    if "rejected" in step:
        label = "Finish rejected"
    if "error" in step.get("result", {}):
        label += " · error"
    if step.get("retry"):
        label += " · retrying"
    return f"Step {step['step']} · {label}"


def show_decision(step):
    st.subheader(step_label(step), anchor=False)
    if step.get("reasoning"):
        st.write(step["reasoning"])
    if "result" in step:
        output, inputs = st.tabs(["Tool result", "Arguments"])
        with output:
            st.json(step["result"], expanded=2)
        with inputs:
            st.json(step["arguments"], expanded=2)
    if "rejected" in step:
        st.warning("The guardrails asked the model to continue.")
        st.json(step["rejected"], expanded=2)
    if step.get("error"):
        st.error(step["error"])
    if step.get("outcome"):
        st.caption(f"Proposed outcome · {step['outcome']}")


def show_evidence(report):
    if not report["timeline"]:
        st.info("The agent has no evidence records to cite for this request.")
        return
    rows = [
        {"Evidence ID": row["evidence_id"], "Observation": row["claim"], "Time": row["timestamp"]}
        for row in report["timeline"]
    ]
    st.dataframe(rows, hide_index=True, use_container_width=True)


def show_trace(report, response, folder, index):
    trace, details = st.columns([1, 2], gap="medium")
    selected_key = f"trace_selection_{index}"
    nodes = [
        ("API response", None),
        ("Input validation", None),
        *((step_label(step), step) for step in report["decisions"]),
        ("Evidence-grounded report", None),
        ("Independent verification", None),
    ]
    selected = st.session_state.get(selected_key, next(
        (i for i, (_, step) in enumerate(nodes) if step and "result" in step), len(nodes) - 1
    ))
    with trace:
        with st.container(height=410, border=True, key="trace_nodes"):
            for position, (label, _) in enumerate(nodes):
                st.button(
                    label, key=f"node_{index}_{position}", use_container_width=True,
                    type="primary" if position == selected else "secondary",
                    on_click=st.session_state.__setitem__, args=(selected_key, position),
                )
                if position < len(nodes) - 1:
                    st.html('<div class="trace-edge" aria-hidden="true"></div>')
    with details:
        with st.container(border=True):
            label, step = nodes[selected]
            if step:
                show_decision(step)
            else:
                st.subheader(label, anchor=False)
                if label == "API response":
                    show_http(response)
                elif label == "Input validation":
                    st.caption("The request passed validation before the first Ollama call.")
                    st.json({"incident_folder": str(folder), "request_id": report["request_id"], "model": report["model"]})
                elif label == "Evidence-grounded report":
                    st.write(report["summary"])
                    show_evidence(report)
                else:
                    st.json(report["verification"], expanded=2)


def show_investigation(report, response, folder, index):
    verification = report["verification"]
    verified = verification["verified"]
    with st.container(border=True, key="diagnosis_panel"):
        st.subheader("Diagnosis", anchor=False)
        st.caption(f"{report['outcome'].replace('_', ' ').capitalize()} · {report['request_id']}")
        st.write(report["summary"])
        if report.get("next_record"):
            st.warning(f"Next record needed · {report['next_record']}")
        calls, retries, checked = st.columns(3)
        calls.metric("Tool calls", len(report["tool_trace"]))
        retries.metric("Model retries", sum(bool(step.get("retry")) for step in report["decisions"]))
        checked.metric("Verification", "Verified" if verified else "Unverified")
        if verification["problems"] or report["warnings"]:
            with st.expander("Verification issues", expanded=not verified):
                for problem in verification["problems"]:
                    st.error(problem)
                for warning in report["warnings"]:
                    st.warning(warning)

    trace_tab, evidence_tab = st.tabs(["Investigation trace", "Cited evidence"])
    with trace_tab:
        show_trace(report, response, folder, index)
    with evidence_tab:
        show_evidence(report)
