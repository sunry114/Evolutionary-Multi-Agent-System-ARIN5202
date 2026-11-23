"""Host stdio MCP client for one run's private Unix socket."""

import json
import socket
import sys


MCP_RESPONSE_BYTES = 8 * 1024 * 1024


def main(path):
    with socket.socket(socket.AF_UNIX) as channel:
        channel.connect(path)
        with channel.makefile("rb") as responses:
            for line in sys.stdin.buffer:
                if len(line) > 2 * 1024 * 1024:
                    return 1
                channel.sendall(line)
                if "id" not in json.loads(line):
                    continue
                response = responses.readline(MCP_RESPONSE_BYTES + 1)
                if not response or len(response) > MCP_RESPONSE_BYTES:
                    return 1
                sys.stdout.buffer.write(response)
                sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1]))
