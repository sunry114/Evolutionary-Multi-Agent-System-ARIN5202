"""Bound both pipes and stdin, enforce deadlines, kill the entire process group."""

import os
import selectors
import signal
import subprocess
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class ProcessOutcome:
    exit_code: int
    reason: str = ""


def capture(process, prompt, *, timeout, limit, stop, on_line):
    deadline = time.monotonic() + timeout
    incoming = bytearray()
    written = total = 0
    reason = ""
    data = prompt.encode("utf-8")
    try:
        with selectors.DefaultSelector() as selector:
            for stream, name, mode in ((process.stdin, "stdin", selectors.EVENT_WRITE),
                                        (process.stdout, "stdout", selectors.EVENT_READ),
                                        (process.stderr, "stderr", selectors.EVENT_READ)):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, mode, name)
            while selector.get_map():
                reason = stop()
                if reason:
                    break
                if time.monotonic() >= deadline:
                    reason = "timeout"
                    break
                for key, _ in selector.select(0.05):
                    stream, name = key.fileobj, key.data
                    if name == "stdin":
                        try:
                            written += os.write(stream.fileno(), data[written:written + 8192])
                        except BrokenPipeError:
                            written = len(data)
                        if written >= len(data):
                            selector.unregister(stream)
                            stream.close()
                        continue
                    chunk = os.read(stream.fileno(), 8192)
                    if not chunk:
                        selector.unregister(stream)
                        if name == "stdout" and incoming:
                            on_line(bytes(incoming))
                            incoming.clear()
                        continue
                    total += len(chunk)
                    if total > limit:
                        reason = "output_limit"
                        break
                    if name == "stderr":
                        continue  # Drain, count, and discard; never log raw diagnostics.
                    incoming.extend(chunk)
                    if len(incoming) > 1024 * 1024:
                        reason = "protocol_error"
                        break
                    while b"\n" in incoming:
                        line, _, rest = incoming.partition(b"\n")
                        incoming[:] = rest
                        if line.strip():
                            on_line(bytes(line))
                if reason:
                    break
            if not reason:
                try:
                    process.wait(timeout=max(0.001, deadline - time.monotonic()))
                except subprocess.TimeoutExpired:
                    reason = "timeout"
    finally:
        # Also terminate descendants that inherited pipes or outlived their parent.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()
        for stream in (process.stdin, process.stdout, process.stderr):
            if not stream.closed:
                stream.close()
    return ProcessOutcome(process.returncode, reason)
