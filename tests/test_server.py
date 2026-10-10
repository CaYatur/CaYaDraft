import json
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from tempfile import TemporaryDirectory

from cayadraft.server import WebhookApp, make_server
from cayadraft.signature import sign
from cayadraft.store import DeliveryStore


class Recorder:
    def __init__(self) -> None:
        self.events: list[str] = []
        self.ready = threading.Event()

    def handle(self, event, payload) -> None:
        self.events.append(event)
        self.ready.set()


class ServerTests(unittest.TestCase):
    def test_ping_signature_and_duplicate(self) -> None:
        with TemporaryDirectory() as tmp:
            recorder = Recorder()
            app = WebhookApp(recorder, "topsecret", "/webhook", DeliveryStore(Path(tmp) / "seen.json"))
            httpd = make_server("127.0.0.1", 0, app)
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            host, port = httpd.server_address[:2]
            try:
                health = urllib.request.urlopen(f"http://{host}:{port}/health", timeout=3)
                self.assertEqual(health.status, 200)
                ping = _post(host, port, "ping", {"zen": "ok"}, "topsecret", "d1")
                self.assertEqual(ping.status, 200)
                denied = _post(host, port, "issues", {"action": "opened"}, "wrong", "d2")
                self.assertEqual(denied.status, 401)
                accepted = _post(host, port, "issues", {"action": "opened"}, "topsecret", "d3")
                self.assertEqual(accepted.status, 202)
                self.assertTrue(recorder.ready.wait(3))
                again = _post(host, port, "issues", {"action": "opened"}, "topsecret", "d3")
                self.assertEqual(again.status, 202)
                self.assertEqual(recorder.events, ["issues"])
            finally:
                httpd.shutdown()
                httpd.server_close()

    def test_public_prefix(self) -> None:
        with TemporaryDirectory() as tmp:
            recorder = Recorder()
            app = WebhookApp(
                recorder,
                "topsecret",
                "/webhook",
                DeliveryStore(Path(tmp) / "seen.json"),
                prefix="/CaYaDraftBot",
            )
            httpd = make_server("127.0.0.1", 0, app)
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            host, port = httpd.server_address[:2]
            try:
                health = urllib.request.urlopen(f"http://{host}:{port}/CaYaDraftBot/health", timeout=3)
                self.assertEqual(health.status, 200)
                ping = _post(host, port, "ping", {"zen": "ok"}, "topsecret", "p1", "/CaYaDraftBot/webhook")
                self.assertEqual(ping.status, 200)
                root = _post(host, port, "ping", {"zen": "ok"}, "topsecret", "p2", "/CaYaDraftBot")
                self.assertEqual(root.status, 200)
                stripped = _post(host, port, "ping", {"zen": "ok"}, "topsecret", "p3", "/")
                self.assertEqual(stripped.status, 200)
            finally:
                httpd.shutdown()
                httpd.server_close()


def _post(host, port, event, payload, secret, delivery, path="/webhook"):
    raw = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        f"http://{host}:{port}{path}",
        data=raw,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Content-Length": str(len(raw)),
            "X-GitHub-Event": event,
            "X-GitHub-Delivery": delivery,
            "X-Hub-Signature-256": sign(secret, raw),
        },
    )
    try:
        return urllib.request.urlopen(request, timeout=3)
    except urllib.error.HTTPError as exc:
        return exc


if __name__ == "__main__":
    unittest.main()
