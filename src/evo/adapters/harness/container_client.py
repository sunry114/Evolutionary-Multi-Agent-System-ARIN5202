"""Trusted container entrypoint and MCP relay. Standard library only.

Mounted read-only; no platform database, credentials or agent workspace is mounted.
"""

import base64
import json
import os
import select
import socket
import socketserver
import stat
import subprocess
import sys
import threading
from pathlib import Path


NATIVE_EXPORT_FILE_BYTES = 1024 * 1024
NATIVE_EXPORT_TOTAL_BYTES = 2 * 1024 * 1024
NATIVE_EXPORT_FILES = 32
MCP_RESPONSE_BYTES = 8 * 1024 * 1024


def export_artifacts(root, suffixes):
    """Emit bounded, regular UTF-8 files from the container's output directory."""
    parent = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    count = total = visited = 0
    try:
        for folder, directories, files, directory in os.fwalk(".", dir_fd=parent, follow_symlinks=False):
            visited += len(directories) + len(files)
            if visited > 1000 or len(Path(folder).parts) > 32:
                raise ValueError("Too many output entries")
            directories[:] = sorted(name for name in directories if not name.startswith("."))
            for name in sorted(files):
                if name.startswith(".") or Path(name).suffix.lower() not in suffixes:
                    continue
                info = os.stat(name, dir_fd=directory, follow_symlinks=False)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    continue
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
                with os.fdopen(fd, "rb") as stream:
                    current = os.fstat(stream.fileno())
                    if not stat.S_ISREG(current.st_mode) or current.st_nlink != 1:
                        raise ValueError("Output is not a regular file")
                    content = stream.read(NATIVE_EXPORT_FILE_BYTES + 1)
                count += 1
                total += len(content)
                if (count > NATIVE_EXPORT_FILES or total > NATIVE_EXPORT_TOTAL_BYTES
                        or len(content) > NATIVE_EXPORT_FILE_BYTES):
                    raise ValueError("Output exceeds the artifact limit")
                content.decode("utf-8")
                yield {"type": "evo_artifact", "path": (Path(folder) / name).as_posix(),
                       "content": base64.b64encode(content).decode("ascii")}
    finally:
        os.close(parent)


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
                        response = reader.readline(MCP_RESPONSE_BYTES + 1)
                        if not response or len(response) > MCP_RESPONSE_BYTES:
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
        suffixes = os.environ.get("EVO_ARTIFACT_SUFFIXES", "")
        output = "/workspace/output"
        if suffixes:
            os.mkdir(output)
        code = subprocess.call(["/opt/claude"] + sys.argv[2:])
        if code == 0 and suffixes:
            try:
                for artifact in export_artifacts(output, frozenset(suffixes.split(","))):
                    print(json.dumps(artifact), flush=True)
            except (OSError, ValueError, UnicodeError):
                return 79
        return code
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    raise SystemExit(main())
