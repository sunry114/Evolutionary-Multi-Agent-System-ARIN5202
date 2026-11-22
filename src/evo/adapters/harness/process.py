"""Read bounded process output and enforce process deadlines."""

import os
import selectors
import signal
import subprocess
import time
from dataclasses import dataclass

from .output import TOOL_PREVIEW_BYTES


@dataclass(frozen=True)
class ProcessOutcome:
    exit_code: int
    reason: str = ""
    stderr: str = ""


def capture(process, prompt, *, timeout, max_event_bytes, stop, on_line, keep_stderr=False):
    deadline = time.monotonic() + timeout
    incoming = bytearray()
    stderr = bytearray()
    stderr_truncated = False
    written = 0
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
                        if name == "stdout" and incoming and not incoming.isspace():
                            on_line(bytes(incoming))
                        continue
                    if name == "stderr":
                        if keep_stderr:
                            stderr.extend(chunk)
                            if len(stderr) > TOOL_PREVIEW_BYTES:
                                del stderr[:-TOOL_PREVIEW_BYTES]
                                stderr_truncated = True
                        continue
                    parts = chunk.split(b"\n")
                    for index, part in enumerate(parts):
                        if len(incoming) + len(part) > max_event_bytes:
                            reason = "event_size_limit"
                            break
                        incoming.extend(part)
                        if index < len(parts) - 1:
                            if incoming and not incoming.isspace():
                                on_line(bytes(incoming))
                            incoming.clear()
                    if reason:
                        break
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
    diagnostic = stderr.decode("utf-8", errors="replace")
    if stderr_truncated:
        diagnostic = "[Earlier stderr truncated]\n" + diagnostic
    return ProcessOutcome(process.returncode, reason, diagnostic)
