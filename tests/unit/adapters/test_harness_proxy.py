import http.client
import json
import socket
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from evo.adapters.harness.model_proxy import ModelProxy
from evo.config import HarnessModelConfig


class ProxyTests(unittest.TestCase):
    def setUp(self):
        self.received = []
        received = self.received

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                received.append({"path": self.path, "headers": dict(self.headers),
                                 "body": json.loads(self.rfile.read(int(self.headers["Content-Length"])))})
                status = 302 if self.server.redirect else 200
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Location", "https://unapproved.example/")
                self.send_header("Set-Cookie", "private=value")
                self.end_headers()
                self.wfile.write(b'{"ok":true}')

        self.upstream = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.upstream.redirect = False
        thread = threading.Thread(target=self.upstream.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 1)
        self.addCleanup(self.upstream.server_close)
        self.addCleanup(self.upstream.shutdown)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "model.sock"
        self.model = HarnessModelConfig(provider="development", development=True, model="allowed-model",
                                        endpoint=f"http://127.0.0.1:{self.upstream.server_port}/anthropic")
        self.proxy = ModelProxy(self.path, self.model, "fixture-credential", timeout=1)
        self.proxy.__enter__()
        self.addCleanup(self.proxy.__exit__, None, None, None)

    def request(self, path="/v1/messages", *, body=None):
        body = json.dumps(body or {"model": "unapproved-model", "messages": []}).encode()
        with socket.socket(socket.AF_UNIX) as channel:
            channel.connect(str(self.path))
            channel.sendall((f"POST {path} HTTP/1.0\r\nHost: localhost\r\nContent-Length: {len(body)}\r\n"
                             "Authorization: Bearer attacker\r\nx-api-key: attacker\r\n\r\n").encode() + body)
            response = http.client.HTTPResponse(channel)
            response.begin()
            content = response.read()
            headers = dict(response.getheaders())
            status = response.status
            response.close()
            return status, headers, content

    def test_only_configured_upstream_model_and_host_owned_auth(self):
        status, headers, _ = self.request("/v1/messages?beta=true")
        self.assertEqual(status, 200)
        self.assertEqual(self.received[0]["path"], "/anthropic/v1/messages?beta=true")
        self.assertEqual(self.received[0]["body"]["model"], "allowed-model")
        self.assertEqual(self.received[0]["headers"]["Authorization"], "Bearer fixture-credential")
        self.assertEqual(self.received[0]["headers"]["x-api-key"], "fixture-credential")
        self.assertNotIn("Set-Cookie", headers)
        self.assertNotIn("Location", headers)

    def test_unknown_routes_absolute_urls_and_query_injection_never_reach_upstream(self):
        for path in ("/admin", "/v1/messages?redirect=other", "http://other.example/v1/messages", "/v1/../admin"):
            with self.subTest(path=path):
                self.assertEqual(self.request(path)[0], 403)
        self.assertEqual(self.received, [])

    def test_redirect_is_denied_without_forwarding_auth_or_location(self):
        self.upstream.redirect = True
        status, headers, body = self.request()
        self.assertEqual(status, 502)
        self.assertNotIn("Location", headers)
        self.assertNotIn(b"fixture-credential", body)
        self.assertEqual(len(self.received), 1)

    def test_count_tokens_uses_the_same_scope(self):
        self.assertEqual(self.request("/v1/messages/count_tokens")[0], 200)
        self.assertEqual(self.received[0]["path"], "/anthropic/v1/messages/count_tokens")

    def test_request_budget_prevents_an_unbounded_cli_loop(self):
        self.proxy.requests = 64
        self.assertEqual(self.request()[0], 502)
        self.assertEqual(self.received, [])
