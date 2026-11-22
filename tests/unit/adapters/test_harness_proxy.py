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
                self.send_header("Content-Type", getattr(self.server, "content_type", "application/json"))
                self.send_header("Location", "https://unapproved.example/")
                self.send_header("Set-Cookie", "private=value")
                self.end_headers()
                body = getattr(self.server, "raw_reply", None)
                if body is None:
                    body = json.dumps(getattr(self.server, "reply", {"ok": True})).encode()
                self.wfile.write(body)

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

    def test_later_stream_is_complete_after_crossing_eight_mib_across_requests(self):
        self.upstream.content_type = "text/event-stream"
        self.upstream.raw_reply = b"data: " + b"x" * (5 * 1024 * 1024) + b"\n\nevent: message_stop\n\n"
        for _ in range(2):
            status, _, body = self.request()
            self.assertEqual(status, 200)
            self.assertTrue(body == self.upstream.raw_reply)

    def test_single_response_over_limit_does_not_affect_next_response(self):
        self.upstream.raw_reply = b"x" * (8 * 1024 * 1024 + 1)
        self.assertEqual(self.request()[0], 502)
        self.upstream.raw_reply = b'{"ok":true}'
        self.assertEqual(self.request()[2], self.upstream.raw_reply)

    def test_empty_success_response_returns_gateway_error(self):
        self.upstream.raw_reply = b""
        for kind in ("application/json", "text/event-stream"):
            self.upstream.content_type = kind
            self.assertEqual(self.request()[0], 502)


class HKGAIProxyTests(unittest.TestCase):
    request = ProxyTests.request

    def setUp(self):
        from types import SimpleNamespace
        ProxyTests.setUp(self)
        self.proxy.__exit__(None, None, None)
        self.path.unlink()
        model = SimpleNamespace(provider="hkgai", model="permitted",
                                endpoint=f"http://127.0.0.1:{self.upstream.server_port}/v1")
        self.proxy = ModelProxy(self.path, model, "fixture-credential", timeout=1)
        self.proxy.__enter__()
        self.addCleanup(self.proxy.__exit__, None, None, None)
        self.upstream.reply = {"id": "fixture", "choices": [{"finish_reason": "stop",
            "message": {"content": "hello"}}], "usage": {"prompt_tokens": 2, "completion_tokens": 1}}

    def test_chat_route_and_fixed_auth_with_stream_translation(self):
        status, headers, body = self.request("/v1/messages?beta=true", body={"model": "attacker",
            "messages": [{"role": "user", "content": "hi"}], "stream": True})
        self.assertEqual(status, 200)
        sent = self.received[0]
        self.assertEqual(sent["path"], "/v1/chat/completions")
        self.assertEqual(sent["body"]["model"], "permitted")
        self.assertFalse(sent["body"]["stream"])
        self.assertEqual(sent["headers"]["Authorization"], "Bearer fixture-credential")
        self.assertNotIn("x-api-key", sent["headers"])
        self.assertEqual(headers["Content-Type"], "text/event-stream")
        self.assertIn(b"event: message_stop", body)

    def test_json_result_and_unsupported_counting(self):
        status, _, body = self.request()
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["usage"], {"input_tokens": 2, "output_tokens": 1})
        self.assertEqual(self.request("/v1/messages/count_tokens")[0], 501)
        self.assertEqual(len(self.received), 1)

    def test_invalid_upstream_does_not_leak_or_fabricate_result(self):
        self.upstream.reply = {"private": "upstream-private-body"}
        status, _, body = self.request()
        self.assertEqual(status, 502)
        self.assertNotIn(b"upstream-private-body", body)
        self.assertNotIn(b"fixture-credential", body)

    def test_unsupported_input_never_reaches_upstream(self):
        status, _, _ = self.request(body={"messages": [{"role": "user", "content": [{"type": "image"}]}]})
        self.assertEqual(status, 502)
        self.assertEqual(self.received, [])

    def test_translated_responses_have_independent_size_limits(self):
        text = "x" * (3 * 1024 * 1024)
        self.upstream.reply["choices"][0]["message"]["content"] = text
        for _ in range(2):
            status, _, body = self.request()
            self.assertEqual(status, 200)
            self.assertTrue(json.loads(body)["content"][0]["text"] == text)
