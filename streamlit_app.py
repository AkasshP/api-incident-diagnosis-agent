"""Trace an HTTP request and its evidence-based investigation in Streamlit."""

import json
from html import escape
from http import HTTPStatus
from http.client import HTTPException
from pathlib import Path

import streamlit as st

from dashboard.http_client import METHODS, send_request
from dashboard.orders_api import OrdersAPI, SCENARIOS
from dashboard.views import show_http, show_investigation, step_label
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
