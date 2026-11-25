"""Local demo HTTP endpoint that writes request-scoped incident evidence."""

import json
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from uuid import UUID, uuid4

from tools import GATEWAY, INVENTORY, ORDER

ROUTE = "POST /orders"
SCENARIOS = {
    "healthy": "Healthy Inventory",
    "outage": "Inventory timeout with handler evidence",
    "missing": "Inventory timeout with missing evidence",
}


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def process_order(payload, root):
    request_id = str(uuid4())
    events = []
    checks = []

    def event(service, event_type, **details):
        events.append({
            "event_id": f"E{len(events) + 1}",
            "timestamp": timestamp(),
            "request_id": request_id,
            "service": service,
            "event_type": event_type,
            "details": {"route": ROUTE, **details},
        })

    scenario = payload["scenario"]
    event(GATEWAY, "request_received")
    if not payload["item_id"].strip():
        status = 400
        event(GATEWAY, "validation_error", missing_field="item_id", http_status=status)
        response = {"error": "item_id is required"}
    elif scenario == "healthy":
        status = 201
        event(INVENTORY, "reservation_completed")
        response = {"item_id": payload["item_id"], "quantity": payload["quantity"], "status": "reserved"}
    else:
        status = 504
        if scenario == "outage":
            event(INVENTORY, "handler_error", error="stock reservation did not complete")
            checks.append({
                "check_id": "HC1",
                "checked_at": timestamp(),
                "target_service": INVENTORY,
                "observed_from": ORDER,
                "path": "/ready",
                "result": "timeout",
            })
        event(ORDER, "downstream_timeout", downstream_service=INVENTORY, http_status=status)
        response = {"error": "Order Service timed out waiting for Inventory Service"}

    bundle = root / request_id
    bundle.mkdir()
    route_map = {GATEWAY: {ROUTE: ORDER}, ORDER: {ROUTE: INVENTORY}}
    (bundle / "logs.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in events), encoding="utf-8"
    )
    (bundle / "dependencies.json").write_text(json.dumps(route_map), encoding="utf-8")
    (bundle / "endpoint_status.json").write_text(json.dumps(checks), encoding="utf-8")
    return status, {"request_id": request_id, **response}


class OrderHandler(BaseHTTPRequestHandler):
    def setup(self):
        self.request.settimeout(5)
        super().setup()

    def do_POST(self):
        if self.path != "/orders":
            self.reply(404, {"error": "Use POST /orders"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 4096:
                raise ValueError("Expected a JSON body of up to 4096 bytes")
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError("Expected a JSON object")
            payload.setdefault("item_id", "")
            payload.setdefault("scenario", "healthy")
            if (
                not isinstance(payload["item_id"], str)
                or type(payload.get("quantity")) is not int
                or payload["quantity"] < 1
                or payload.get("scenario") not in tuple(SCENARIOS)
            ):
                raise ValueError("Expected an item_id string, positive integer quantity, and demo scenario")
        except (OSError, ValueError) as error:
            self.reply(400, {"error": str(error)})
            return

        self.reply(*process_order(payload, self.server.evidence_root))

    def reply(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass


class OrdersAPI:
    def __init__(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), OrderHandler)
        self.directory = TemporaryDirectory(prefix="orders-demo-")
        self.server.evidence_root = Path(self.directory.name)
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server.server_port}/orders"

    def bundle_for(self, request_id):
        try:
            bundle = self.server.evidence_root / str(UUID(request_id))
        except (TypeError, ValueError, AttributeError):
            return None
        return bundle if bundle.is_dir() else None

    def close(self):
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()
        self.directory.cleanup()
