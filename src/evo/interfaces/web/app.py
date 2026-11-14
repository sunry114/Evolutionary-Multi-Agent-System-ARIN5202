"""Loopback-only server. Backend side effects remain behind ApplicationGateway."""
import json
import os
import secrets
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from evo.interfaces.gateway import ApplicationUnavailable

MAX_BODY = 65536
OPERATIONS = {"run", "dm", "group", "review", "revision", "answer", "eval", "trace", "metrics", "memory", "task", "tasks"}


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def reject_constant(value):
    raise ValueError("Non-finite JSON number")


def redact(value):
    sensitive = ("api_key", "apikey", "authorization", "token", "password", "secret", "credential")
    if isinstance(value, dict):
        return {str(k): ("[redacted]" if any(s in str(k).lower() for s in sensitive) else redact(v))
                for k, v in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, str):
        for name in ("HKGAI_API_KEY", "DEEPSEEK_API_KEY", "GITHUB_TOKEN"):
            key = os.environ.get(name, "")
            if key:
                value = value.replace(key, "[redacted]")
    return value


def make_server(application, port=8768):
    token = secrets.token_urlsafe(32)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # No payload, URL or credential logging.

        def reply(self, status, data, html=False):
            encoded = data.encode("utf-8") if html else json.dumps(redact(data), ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8" if html else "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy",
                             "default-src 'none'; script-src 'nonce-" + token +
                             "'; style-src 'nonce-" + token + "'; connect-src 'self'; "
                             "frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            self.end_headers()
            self.wfile.write(encoded)

        def valid_host(self):
            return self.headers.get_all("Host", []) == [self.server.authority]

        def do_GET(self):
            if not self.valid_host():
                return self.reply(403, {"error": "Invalid Host"})
            if self.path == "/":
                content = files("evo.interfaces.web").joinpath("assets/index.html").read_text(encoding="utf-8")
                return self.reply(200, content.replace("__TOKEN__", token), html=True)
            if self.path == "/api/state":
                try:
                    return self.reply(200, application.snapshot())
                except Exception:
                    return self.reply(500, {"error": "Unable to read application state"})
            return self.reply(404, {"error": "Not found"})

        def do_POST(self):
            origin = "http://" + self.server.authority
            csrf = self.headers.get_all("X-CSRF-Token", [])
            if (not self.valid_host() or self.headers.get_all("Origin", []) != [origin]
                    or len(csrf) != 1 or not csrf[0].isascii()
                    or not secrets.compare_digest(csrf[0], token)):
                return self.reply(403, {"error": "Invalid Host, Origin or CSRF token"})
            content_types = self.headers.get_all("Content-Type", [])
            if len(content_types) != 1 or content_types[0].split(";")[0].strip().lower() != "application/json":
                return self.reply(415, {"error": "JSON content type required"})
            if self.headers.get_all("Transfer-Encoding") is not None:
                return self.reply(400, {"error": "Transfer encoding not supported"})
            try:
                lengths = self.headers.get_all("Content-Length", [])
                if len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdigit():
                    raise ValueError()
                length = int(lengths[0])
                if not 0 < length <= MAX_BODY:
                    return self.reply(413, {"error": "Invalid body size"})
                body = self.rfile.read(length)
                if len(body) != length:
                    raise ValueError()
                payload = json.loads(body.decode("utf-8"), object_pairs_hook=unique_object,
                                     parse_constant=reject_constant)
                if not isinstance(payload, dict):
                    raise ValueError()
            except (TimeoutError, OSError):
                return self.reply(408, {"error": "Request body read timed out"})
            except (ValueError, UnicodeError, RecursionError):
                return self.reply(400, {"error": "JSON object required"})
            operation = self.path.removeprefix("/api/") if self.path.startswith("/api/") else ""
            if operation not in OPERATIONS:
                return self.reply(404, {"error": "Unknown operation"})
            try:
                result = application.dispatch(operation, payload)
                return self.reply(200, result)
            except ApplicationUnavailable:
                return self.reply(503, {"error": "Agent execution backend not connected; this mode supports stored tasks, trace, review and revision."})
            except (ValueError, KeyError):
                return self.reply(409, {"error": "Invalid request or state conflict"})
            except Exception:
                return self.reply(500, {"error": "Application operation failed"})

    class Server(ThreadingHTTPServer):
        daemon_threads = True
        def get_request(self):
            connection, address = super().get_request()
            connection.settimeout(10)
            return connection, address

    server = Server(("127.0.0.1", port), Handler)
    server.authority = "127.0.0.1:" + str(server.server_port)
    return server


def serve(application, port=8768):
    with make_server(application, port) as server:
        print("Open http://" + server.authority, flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
