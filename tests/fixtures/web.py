"""Offline DNS/HTTP fixtures; no listener, network access or TLS bypass."""

import io
import socket
from contextlib import contextmanager
from email.message import Message


def dns(host, timeout):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]


class Response:
    def __init__(self, body=b"hello", *, status=200, headers=None):
        self.status = status
        self.headers = Message()
        for name, value in (headers if headers is not None else [("Content-Type", "text/plain")]):
            self.headers[name] = value
        self.body = io.BytesIO(body)
        self.closed = False

    def read1(self, size):
        return self.body.read(size)


class Transport:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    @contextmanager
    def __call__(self, url, target, deadline):
        self.calls.append((url, target, deadline))
        response = self.responses.pop(0)
        try:
            yield response
        finally:
            response.closed = True
