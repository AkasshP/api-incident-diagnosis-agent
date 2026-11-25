"""Send a user-supplied HTTP request and retain a bounded response preview."""

import json
from dataclasses import dataclass
from http.client import HTTPConnection, HTTPSConnection
from time import perf_counter
from urllib.parse import urlsplit

METHODS = ("POST", "GET", "PUT", "PATCH", "DELETE")
MAX_RESPONSE_BYTES = 65536


@dataclass
class Response:
    method: str
    url: str
    status: int
    latency_ms: float
    request_id: str
    text: str
    truncated: bool


def send_request(url, method, body):
    url = url.strip()
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password:
        raise ValueError("Enter an http:// or https:// API URL without embedded credentials.")
    if parts.fragment:
        raise ValueError("Remove the URL fragment before sending the request.")
    if method not in METHODS:
        raise ValueError("Choose a supported HTTP method.")
    target = parts.path or "/"
    if parts.query:
        target += f"?{parts.query}"
    client = HTTPSConnection if parts.scheme == "https" else HTTPConnection
    headers = {"Accept": "application/json"}
    payload = None
    if body.strip() and method != "GET":
        payload = json.dumps(json.loads(body), allow_nan=False).encode("utf-8")
        headers["Content-Type"] = "application/json"

    started = perf_counter()
    with_connection = client(parts.hostname, parts.port, timeout=10)
    try:
        with_connection.request(method, target, body=payload, headers=headers)
        response = with_connection.getresponse()
        status = response.status
        request_id = response.getheader("X-Request-ID") or ""
        raw = response.read(MAX_RESPONSE_BYTES + 1)
    finally:
        with_connection.close()
    text = raw[:MAX_RESPONSE_BYTES].decode("utf-8", errors="replace")
    if not request_id:
        try:
            parsed = json.loads(text)
            value = parsed.get("request_id") if isinstance(parsed, dict) else None
            request_id = value if isinstance(value, str) else ""
        except ValueError:
            pass
    return Response(method, url, status, (perf_counter() - started) * 1000, request_id, text,
                    len(raw) > MAX_RESPONSE_BYTES)
