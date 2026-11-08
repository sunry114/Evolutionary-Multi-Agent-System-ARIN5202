"""CLI parses requests; A/B/C application gateway owns task behavior."""
import argparse
import getpass
import json
import sys
from pathlib import Path

from evo import __version__
from evo.adapters.llm.credentials import save_key
from evo.interfaces.gateway import ApplicationUnavailable, load_application


def parser():
    root = argparse.ArgumentParser(description="Evolutionary multi-agent platform")
    root.add_argument("--version", action="version", version=__version__)
    root.add_argument("--state", default=".evo")
    root.add_argument("--demo", action="store_true", help="Volatile UI preview; no agents execute")
    commands = root.add_subparsers(dest="command")
    for name in ("run", "group"):
        command = commands.add_parser(name)
        command.add_argument("prompt" if name == "run" else "message")
    command = commands.add_parser("dm")
    command.add_argument("agent")
    command.add_argument("message")
    for name in ("trace", "metrics"):
        commands.add_parser(name).add_argument("task_id")
    commands.add_parser("memory").add_argument("agent")
    command = commands.add_parser("eval")
    command.add_argument("dataset")
    command.add_argument("--output", required=True)
    command.add_argument("--no-memory", action="store_true")
    command = commands.add_parser("review")
    command.add_argument("task_id")
    command.add_argument("decision", choices=("accepted", "changes_requested"))
    command.add_argument("--feedback", default="")
    command = commands.add_parser("revision")
    command.add_argument("task_id")
    command.add_argument("prompt")
    command = commands.add_parser("answer")
    command.add_argument("request_id")
    choice = command.add_mutually_exclusive_group(required=True)
    choice.add_argument("--approve", action="store_true")
    choice.add_argument("--deny", action="store_true")
    choice.add_argument("--text")
    command = commands.add_parser("web")
    command.add_argument("--port", type=int, default=8768)
    command = commands.add_parser("setup-key", aliases=["setup-hkgai-key"])
    command.add_argument("--provider", choices=("hkgai", "deepseek"), default="hkgai")
    return root


def main(argv=None, application=None):
    root = parser()
    args = root.parse_args(argv)
    if not args.command:
        root.print_help()
        return 0
    try:
        if args.command in ("setup-key", "setup-hkgai-key"):
            path = save_key(args.provider, getpass.getpass("API key (hidden): "))
            print("Saved private credential at " + str(path))
            return 0
        app = application if application is not None else load_application(Path(args.state), args.demo)
        if args.command == "web":
            from evo.interfaces.web.app import serve
            if not 0 <= args.port <= 65535:
                raise ValueError("Invalid port")
            serve(app, args.port)
            return 0
        payload = {k: v for k, v in vars(args).items() if k not in ("command", "demo", "state")}
        if args.command == "answer":
            payload = {"request_id": args.request_id}
            payload.update({"answer": args.text} if args.text is not None else {"approved": args.approve})
        result = app.dispatch(args.command, payload)
        from evo.interfaces.web.app import redact
        print(json.dumps(redact(result), ensure_ascii=False, indent=2))
        return 1 if result.get("status") in ("failed", "blocked", "unavailable") or result.get("passed") is False else 0
    except (ApplicationUnavailable, ValueError, OSError) as error:
        # Backend exception text can contain request data and secrets.
        print(str(error) if isinstance(error, ApplicationUnavailable) else "Request failed; check input and application state", file=sys.stderr)
        return 2
    except Exception:
        print("Application operation failed", file=sys.stderr)
        return 2
