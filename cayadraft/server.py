"""Local webhook server. GitHub gets a fast response; Ollama work runs afterward."""

from __future__ import annotations

import json
import logging
import queue
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from cayadraft.signature import verify_signature
from cayadraft.store import DeliveryStore

log = logging.getLogger("cayadraft.server")


class WebhookApp:
    def __init__(self, handler, secret: str, path: str, store: DeliveryStore, prefix: str = "") -> None:
        self.handler = handler
        self.secret = secret
        self.path = _slash(path)
        self.prefix = _slash(prefix).rstrip("/") if prefix.strip() else ""
        self.store = store
        self.queue: queue.Queue = queue.Queue(maxsize=32)
        self._worker = threading.Thread(target=self._loop, name="cayadraft-worker", daemon=True)
        self._worker.start()

    def accept(self, event: str, payload: dict, delivery: str) -> int:
        if delivery and self.store.seen(delivery):
            log.info("duplicate delivery %s", delivery)
            return 202
        if self.queue.full():
            return 503
        if delivery:
            self.store.mark(delivery)
        self.queue.put((event, payload, delivery))
        return 202

    def _loop(self) -> None:
        while True:
            event, payload, delivery = self.queue.get()
            try:
                self.handler.handle(event, payload)
            except Exception:
                log.exception("delivery %s failed", delivery or "?")
            finally:
                self.queue.task_done()


def _slash(path: str) -> str:
    value = (path or "").strip()
    if not value:
        return ""
    return value if value.startswith("/") else f"/{value}"


def _clean(path: str) -> str:
    value = urlparse(path).path or "/"
    if len(value) > 1:
        value = value.rstrip("/")
    return value


def is_health(path: str, prefix: str) -> bool:
    path = _clean(path)
    names = {"/health"}
    if prefix:
        names.add(f"{prefix}/health")
    return path in names


def is_webhook(path: str, prefix: str, explicit: str) -> bool:
    path = _clean(path)
    # "/" is the path left when the reverse proxy strips /CaYaDraftBot.
    names = {"/", "/webhook"}
    if explicit:
        names.add(_clean(explicit))
    if prefix:
        names.add(prefix)
        names.add(f"{prefix}/webhook")
    return path in names


def make_server(host: str, port: int, app: WebhookApp) -> ThreadingHTTPServer:
    secret = app.secret
    prefix = app.prefix
    explicit = app.path

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:  # noqa: N802
            if is_health(self.path, prefix):
                self._send(200, {"ok": True, "name": "CaYaDraft"})
                return
            self._send(404, {"ok": False})

        def do_POST(self) -> None:  # noqa: N802
            if not is_webhook(self.path, prefix, explicit):
                self.close_connection = True
                self._send(404, {"ok": False})
                return
            length = int(self.headers.get("Content-Length") or "0")
            if length < 0 or length > 2_000_000:
                self.close_connection = True
                self._send(413, {"ok": False})
                return
            body = self.rfile.read(length)
            if not verify_signature(secret, body, self.headers.get("X-Hub-Signature-256")):
                self._send(401, {"ok": False})
                return
            event = self.headers.get("X-GitHub-Event") or ""
            if event == "ping":
                self._send(200, {"ok": True})
                return
            try:
                payload = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._send(400, {"ok": False})
                return
            if not isinstance(payload, dict):
                self._send(400, {"ok": False})
                return
            status = app.accept(event, payload, self.headers.get("X-GitHub-Delivery") or "")
            self._send(status, {"ok": status == 202})

        def log_message(self, fmt: str, *args) -> None:
            log.info("%s", fmt % args)

        def _send(self, status: int, payload: dict) -> None:
            raw = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    return httpd
