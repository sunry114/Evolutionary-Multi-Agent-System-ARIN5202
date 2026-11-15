"""Trusted container entrypoint and MCP relay. Standard library only.

Mounted read-only; no platform database, credentials or agent workspace is mounted.
"""

import json
import os
import select
import socket
import socketserver
import subprocess
import sys
import threading


def relay(left, right):
    try:
        while True:
            readable, _, _ = select.select((left, right), (), (), 1)
            for source in readable:
                data = source.recv(16384)
                if not data:
                    return
                (right if source is left else left).sendall(data)
    finally:
        right.close()


def main():
    if sys.argv[1] == "mcp":
        with socket.socket(socket.AF_UNIX) as channel:
            channel.connect("/broker/mcp.sock")
            reader = channel.makefile("rb")
            try:
                for line in sys.stdin.buffer:
                    if len(line) > 2 * 1024 * 1024:
                        return 1
                    channel.sendall(line)
                    # Notifications have no response.
                    if "id" in json.loads(line):
                        response = reader.readline(2 * 1024 * 1024 + 1)
                        if not response or len(response) > 2 * 1024 * 1024:
                            return 1
                        sys.stdout.buffer.write(response)
                        sys.stdout.buffer.flush()
            finally:
                reader.close()
        return 0

    class Handler(socketserver.BaseRequestHandler):
        def handle(self):
            channel = socket.socket(socket.AF_UNIX)
            channel.connect("/broker/model.sock")
            relay(self.request, channel)

    class Server(socketserver.ThreadingTCPServer):
        daemon_threads = True
        allow_reuse_address = True

    server = Server(("127.0.0.1", 18080), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    version = subprocess.run(["/opt/claude", "--version"], capture_output=True, timeout=15)
    if version.stdout.decode().strip() != os.environ["EVO_CLI_VERSION"] + " (Claude Code)":
        return 78
    # Keep the relay thread alive while the CLI inherits the prompt on stdin.
    try:
        return subprocess.call(["/opt/claude"] + sys.argv[2:])
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    raise SystemExit(main())
