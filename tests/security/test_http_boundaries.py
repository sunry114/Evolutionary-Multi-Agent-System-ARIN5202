import json
import socket
import threading
import unittest
from http.client import HTTPConnection

from evo.interfaces.web.app import MAX_BODY, make_server


class RecordingApplication:
    def __init__(self):
        self.calls = []

    def snapshot(self):
        return {"tasks": []}

    def dispatch(self, operation, payload):
        self.calls.append((operation, payload))
        return {"status": "success"}


class HttpBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.app = RecordingApplication()
        self.server = make_server(self.app, 0)
        original = self.server.get_request
        def short_timeout():
            connection, address = original()
            connection.settimeout(0.15)
            return connection, address
        self.server.get_request = short_timeout
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close)
        conn = HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        try:
            conn.request("GET", "/")
            response = conn.getresponse()
            import re
            self.token = re.search(r'name="csrf-token" content="([^"]+)"',
                                   response.read().decode()).group(1)
        finally:
            conn.close()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)

    def send(self, body=b'{}', *, replacements=None, extra=(), half_close=False):
        headers = {
            "Host": self.server.authority, "Origin": "http://" + self.server.authority,
            "X-CSRF-Token": self.token, "Content-Type": "application/json",
            "Content-Length": str(len(body)),
        }
        for name, value in (replacements or {}).items():
            if value is None:
                headers.pop(name, None)
            else:
                headers[name] = value
        wire = "POST /api/run HTTP/1.0\r\n" + "".join(
            k + ": " + v + "\r\n" for k, v in [*headers.items(), *extra]) + "\r\n"
        with socket.create_connection(("127.0.0.1", self.server.server_port), timeout=3) as connection:
            connection.sendall(wire.encode("latin-1") + body)
            if half_close:
                connection.shutdown(socket.SHUT_WR)
            response = bytearray()
            while True:
                block = connection.recv(65536)
                if not block:
                    break
                response.extend(block)
        head, payload = bytes(response).split(b"\r\n\r\n", 1)
        return int(head.split()[1]), json.loads(payload)

    def test_non_ascii_and_duplicate_security_headers(self):
        for replacements, extra in (
            ({"X-CSRF-Token": "\xe9"}, ()),
            ({}, (("Host", self.server.authority),)),
            ({}, (("Origin", "http://" + self.server.authority),)),
            ({}, (("X-CSRF-Token", self.token),)),
        ):
            with self.subTest(replacements=replacements, extra=extra):
                self.assertEqual(self.send(replacements=replacements, extra=extra)[0], 403)
        self.assertEqual(self.app.calls, [])

    def test_duplicate_content_type_and_content_length(self):
        self.assertEqual(self.send(extra=(("Content-Type", "application/json"),))[0], 415)
        self.assertEqual(self.send(extra=(("Content-Length", "2"),))[0], 400)
        self.assertEqual(self.app.calls, [])

    def test_invalid_or_missing_length(self):
        for value in (None, "abc", "+2", "-1", "2,2", "2.0"):
            with self.subTest(value=value):
                self.assertEqual(self.send(replacements={"Content-Length": value})[0], 400)
        self.assertEqual(self.app.calls, [])

    def test_body_size_boundaries(self):
        for length in (0, MAX_BODY + 1):
            self.assertEqual(self.send(replacements={"Content-Length": str(length)})[0], 413)
        self.assertEqual(self.app.calls, [])
        body = b'{"value":"' + b"x" * (MAX_BODY - 12) + b'"}'
        self.assertEqual(len(body), MAX_BODY)
        self.assertEqual(self.send(body)[0], 200)
        self.assertEqual(len(self.app.calls), 1)

    def test_transfer_encoding_even_empty_is_rejected(self):
        for value in ("chunked", ""):
            self.assertEqual(self.send(extra=(("Transfer-Encoding", value),))[0], 400)
        self.assertEqual(self.app.calls, [])

    def test_truncated_body_and_read_timeout(self):
        self.assertEqual(self.send(b'{', replacements={"Content-Length": "10"}, half_close=True)[0], 400)
        self.assertEqual(self.send(b'{', replacements={"Content-Length": "10"})[0], 408)
        self.assertEqual(self.app.calls, [])

    def test_invalid_utf8_deep_json_nonfinite_and_duplicate_keys(self):
        for body in (b'{"x":"\xff"}', b'{"x":NaN}', b'{"x":Infinity}',
                     b'{"task_id":"one","task_id":"two"}',
                     b'{"x":{"key":1,"key":2}}', b'[' * 2000 + b']' * 2000):
            with self.subTest(prefix=body[:40]):
                self.assertEqual(self.send(body)[0], 400)
        self.assertEqual(self.app.calls, [])

    def test_valid_utf8_object_with_charset_still_works(self):
        body = json.dumps({"message": "\u4f60\u597d"}, ensure_ascii=False).encode("utf-8")
        self.assertEqual(self.send(body, replacements={"Content-Type": "application/json; charset=utf-8"})[0], 200)
        self.assertEqual(self.app.calls, [("run", {"message": "\u4f60\u597d"})])
