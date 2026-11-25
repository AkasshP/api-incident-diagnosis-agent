"""Trace an HTTP request and its evidence-based investigation in Streamlit."""

import json
from html import escape
from http import HTTPStatus
from http.client import HTTPException
from pathlib import Path

import streamlit as st

from dashboard.http_client import METHODS, send_request
from dashboard.orders_api import OrdersAPI, SCENARIOS
from llm_agent import DEFAULT_MODEL, DEFAULT_WINDOW_SECONDS, investigate

PRESETS = {
    "Inventory outage": {"item_id": "item-100", "quantity": 1, "scenario": "outage"},
    "Missing Inventory evidence": {"item_id": "item-100", "quantity": 1, "scenario": "missing"},
    "Invalid request": {"quantity": 1},
    SCENARIOS["healthy"]: {"item_id": "item-100", "quantity": 1},
}
STYLES = Path(__file__).with_name("dashboard") / "console.css"


@st.cache_resource
def demo_api():
    return OrdersAPI()


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


def request_form(api_url):
    demo_mode = st.radio("Request source", ("Demo API", "Custom endpoint"), horizontal=True) == "Demo API"
    preset = st.selectbox("Demo payload", tuple(PRESETS)) if demo_mode else None
    with st.form("demo_request" if demo_mode else "custom_request", border=False):
        if demo_mode:
            url, method = api_url, "POST"
            st.code("POST /orders", language="text")
        else:
            method_col, url_col = st.columns([1, 3])
            method = method_col.selectbox("Method", METHODS)
            url = url_col.text_input("API URL", placeholder="https://api.example.com/orders")
        body = st.text_area(
            "JSON body", json.dumps(PRESETS[preset], indent=2) if preset else "",
            key=f"body_{preset or 'custom'}", height=145,
        )
        sent = st.form_submit_button("Send request", type="primary", use_container_width=True)
    return sent, url, method, body


def investigate_response(item, index):
    response = item["response"]
    folder = item["folder"]
    request_id = response.request_id
    if folder is None or not request_id:
        with st.expander("Attach request evidence", expanded=True):
            folder_text = st.text_input("Incident folder path", key=f"folder_{index}")
            folder = Path(folder_text).expanduser() if folder_text.strip() else None
            request_id = st.text_input("Request ID to investigate", value=request_id, key=f"request_id_{index}")
    if not st.button("Investigate", type="primary", disabled=folder is None or not request_id.strip()):
        return

    item.pop("report", None)
    st.session_state.pop(f"trace_selection_{index}", None)
    progress = st.status("Validating investigation input…", expanded=True)
    completed_tools = 0

    def show_progress(entry):
        nonlocal completed_tools
        step = json.loads(entry)
        if "result" in step and "error" not in step["result"]:
            completed_tools += 1
        unit = "tool call" if completed_tools == 1 else "tool calls"
        progress.update(label=f"Investigating · {completed_tools} {unit}")
        progress.write(step_label(step))

    try:
        item["report"] = investigate(folder, request_id, DEFAULT_MODEL, DEFAULT_WINDOW_SECONDS, log=show_progress)
    except (OSError, ValueError) as error:
        progress.update(label="Investigation failed", state="error")
        st.error(str(error))
    else:
        item["investigated_folder"] = folder
        progress.update(label="Investigation complete", state="complete", expanded=False)


def main():
    st.set_page_config(page_title="API Incident Console", layout="wide")
    st.html(STYLES)
    st.html(f"""
        <header class="console-header">
            <div>
                <h1>API Incident Console</h1>
                <p>Send a request and follow the evidence behind its diagnosis.</p>
            </div>
            <span class="console-model">Ollama · {escape(DEFAULT_MODEL)}</span>
        </header>
    """)
    try:
        api = demo_api()
    except OSError as error:
        st.error(f"Could not start the local demo API: {error}")
        return

    history = st.session_state.setdefault("history", [])
    request_panel, response_panel = st.columns([1, 1], gap="medium")
    with request_panel, st.container(border=True, key="request_panel"):
        st.subheader("API request", anchor=False)
        sent, url, method, body = request_form(api.url)
        if sent:
            try:
                response = send_request(url, method, body)
            except (HTTPException, OSError, ValueError) as error:
                st.error(f"Request failed: {error}")
            else:
                bundle = api.bundle_for(response.request_id) if response.url == api.url else None
                history.append({"response": response, "folder": bundle})
                st.session_state["selected_request"] = len(history) - 1

    with response_panel, st.container(border=True, key="response_panel"):
        st.subheader("API response", anchor=False)
        if not history:
            st.info("Send a request to see its status, timing, and response body here.")
            return

        def response_label(index):
            response = history[index]["response"]
            try:
                phrase = f" {HTTPStatus(response.status).phrase}"
            except ValueError:
                phrase = ""
            return f"{response.method} · {response.status}{phrase} · {response.request_id or response.url}"

        index = st.selectbox("Request history", range(len(history) - 1, -1, -1),
                             format_func=response_label, key="selected_request")
        item = history[index]
        response = item["response"]
        show_http(response)
        if response.status >= 400:
            investigate_response(item, index)

    if "report" in item:
        show_investigation(item["report"], response, item["investigated_folder"], index)


if __name__ == "__main__":
    main()
