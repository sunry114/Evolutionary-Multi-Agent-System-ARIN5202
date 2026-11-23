"""Run a local Playwright check and reap its browser and optional server."""

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

from evo.adapters.harness.process import terminate_group


def playwright_module():
    configured = os.environ.get("EVO_PLAYWRIGHT_MODULE")
    if configured:
        return str(Path(configured).expanduser().resolve())
    local = Path(__file__).resolve().parents[4] / "tests/browser/node_modules/playwright"
    return str(local) if local.is_dir() else "playwright"


def project_path(value, root):
    path = (root / value).resolve()
    if path == root or root not in path.parents:
        raise ValueError("Paths must be inside the current project directory")
    return path


def check(target, screenshot, selector, server):
    root = Path.cwd().resolve()
    parts = urlsplit(target)
    if parts.scheme:
        if (parts.scheme not in ("http", "https") or parts.hostname not in {"127.0.0.1", "::1", "localhost"}
                or parts.username is not None or parts.password is not None):
            raise ValueError("Use a loopback HTTP URL or a local HTML file")
    else:
        local_file = project_path(target, root)
        if not local_file.is_file() or local_file.suffix.lower() not in {".html", ".htm"}:
            raise ValueError("Target is not a local HTML file")
        target = local_file.as_uri()
    if server and not parts.scheme:
        raise ValueError("A server requires a loopback HTTP URL")
    output = project_path(screenshot, root) if screenshot else None
    if output and (output.suffix.lower() != ".png" or not output.parent.is_dir()):
        raise ValueError("Use a .png path in an existing project directory")
    node = shutil.which("node")
    if not node:
        raise ValueError("Node.js is required for browser checks")
    env = {key: os.environ[key] for key in ("PATH", "HOME", "NODE_PATH", "PLAYWRIGHT_BROWSERS_PATH")
           if key in os.environ}
    with tempfile.TemporaryDirectory(prefix="evo-browser-") as temp:
        image = Path(temp) / "check.png" if output else None
        command = json.dumps({"target": target, "screenshot": str(image) if image else None, "selector": selector,
                              "server": server, "module": playwright_module()})
        with tempfile.TemporaryFile() as log:
            process = subprocess.Popen([node, str(Path(__file__).with_suffix(".cjs")), command],
                                       stdin=subprocess.DEVNULL, stdout=log, stderr=log, env=env,
                                       start_new_session=True)
            try:
                try:
                    code = process.wait(timeout=22)
                except subprocess.TimeoutExpired:
                    raise ValueError("Browser check exceeded 22 seconds") from None
            finally:
                if terminate_group(process) is None:
                    raise ValueError("Browser check process did not exit after termination")
            log.seek(0)
            result = log.read(16384).decode("utf-8", errors="replace").strip()
        if code != 0:
            raise ValueError(result or f"Browser check exited with status {code}")
        if image:
            with image.open("rb") as stream:
                if stream.read(8) != b"\x89PNG\r\n\x1a\n":
                    raise ValueError("Browser check did not produce a valid PNG")
            shutil.copyfile(image, output)
        return json.loads(result) | ({"screenshot": str(output)} if output else {})


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     epilog="To own a server, append --serve followed by its command and arguments.")
    parser.add_argument("target")
    parser.add_argument("--screenshot")
    parser.add_argument("--selector", help="CSS selector expected to be visible")
    parser.add_argument("--serve", nargs=argparse.REMAINDER, default=[])
    args = parser.parse_args()
    signal.signal(signal.SIGTERM, signal.default_int_handler)
    try:
        print(json.dumps(check(args.target, args.screenshot, args.selector, args.serve), ensure_ascii=False))
    except KeyboardInterrupt:
        return 130
    except (OSError, ValueError) as error:
        print(f"Browser check failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
