"""A fixed-upstream reverse proxy over a private Unix socket, never an open proxy."""

import http.client
import json
import socketserver
import ssl
import threading
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlsplit


class ModelProxy:
    def __init__(self, path, model, credential, *, timeout=30, max_bytes=8 * 1024 * 1024):
        proxy = self
        self.endpoint = urlsplit(model.endpoint)
        self.requests = 0
        self.bytes = 0
        self.status_codes = {}
        self.failures = 0
        self.lock = threading.Lock()
        self.connections = set()

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *args):
                pass  # Never log payloads, auth headers or endpoint details.

            def do_POST(self):
                connection = None
                try:
                    self.connection.settimeout(timeout)
                    incoming = urlsplit(self.path)
                    if (incoming.path not in ("/v1/messages", "/v1/messages/count_tokens")
                            or incoming.query not in ("", "beta=true") or incoming.scheme or incoming.netloc):
                        self.send_error(403, "Model route denied")
                        return
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= 2 * 1024 * 1024 or self.headers.get("Transfer-Encoding"):
                        self.send_error(400, "Invalid model request")
                        return
                    body = self.rfile.read(length)
                    payload = json.loads(body)
                    if not isinstance(payload, dict):
                        raise ValueError()
                    payload["model"] = model.model
                    body = json.dumps(payload).encode()
                    with proxy.lock:
                        proxy.requests += 1
                        if proxy.requests > 64:
                            raise ValueError()
                    endpoint = proxy.endpoint
                    if endpoint.scheme == "https":
                        connection = http.client.HTTPSConnection(endpoint.hostname, endpoint.port or 443,
                                                                  timeout=timeout, context=ssl.create_default_context())
                    else:
                        connection = http.client.HTTPConnection(endpoint.hostname, endpoint.port or 80, timeout=timeout)
                    with proxy.lock:
                        proxy.connections.add(connection)
                    route = incoming.path
                    if endpoint.path.rstrip("/").endswith("/v1"):
                        route = route[3:]
                    target = endpoint.path.rstrip("/") + route
                    if incoming.query:
                        target += "?" + incoming.query
                    headers = {"Content-Type": "application/json", "x-api-key": credential,
                               "Authorization": "Bearer " + credential, "anthropic-version": "2023-06-01"}
                    beta = self.headers.get("anthropic-beta", "")
                    if beta and len(beta) < 2048:
                        headers["anthropic-beta"] = beta
                    connection.request("POST", target, body, headers)
                    response = connection.getresponse()
                    with proxy.lock:
                        proxy.status_codes[str(response.status)] = proxy.status_codes.get(str(response.status), 0) + 1
                    # Do not follow or relay Location/Set-Cookie/credentials.
                    if 300 <= response.status < 400:
                        self.send_error(502, "Model redirect denied")
                        return
                    self.send_response(response.status)
                    self.send_header("Content-Type", response.getheader("Content-Type", "application/json"))
                    self.end_headers()
                    while True:
                        data = response.read1(8192)
                        if not data:
                            break
                        with proxy.lock:
                            proxy.bytes += len(data)
                            if proxy.bytes > max_bytes:
                                return
                        self.wfile.write(data)
                        self.wfile.flush()
                except Exception:
                    # Details can contain auth headers or provider response bodies.
                    with proxy.lock:
                        proxy.failures += 1
                    try:
                        self.send_error(502, "Model request failed")
                    except OSError:
                        pass
                finally:
                    if connection:
                        with proxy.lock:
                            proxy.connections.discard(connection)
                        connection.close()

        class Server(socketserver.ThreadingUnixStreamServer):
            daemon_threads = True
            block_on_close = False
            slots = threading.BoundedSemaphore(4)

            def process_request(self, request, client_address):
                if not self.slots.acquire(blocking=False):
                    self.shutdown_request(request)
                    return
                try:
                    super().process_request(request, client_address)
                except BaseException:
                    self.slots.release()
                    raise

            def process_request_thread(self, request, client_address):
                try:
                    super().process_request_thread(request, client_address)
                finally:
                    self.slots.release()

            def handle_error(self, request, client_address):
                pass

        self.server = Server(str(path), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)

    def summary(self):
        with self.lock:
            return {"requests": self.requests, "status_codes": dict(self.status_codes),
                    "failures": self.failures}

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.server.shutdown()
        self.server.server_close()
        with self.lock:
            for connection in self.connections:
                connection.close()
        self.thread.join(timeout=1)
