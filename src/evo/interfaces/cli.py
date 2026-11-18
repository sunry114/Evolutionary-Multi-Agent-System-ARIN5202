"""CLI parses requests; A/B/C application gateway owns task behavior."""
import argparse
import getpass
import json
import sys
import warnings
from pathlib import Path

from evo import __version__
from evo.adapters.llm.credentials import save_key
from evo.interfaces.gateway import ApplicationFailure, ApplicationUnavailable, load_application


def read_private_key(prompt):
    # getpass normally warns then falls back to an echoed stdin read. Refuse it.
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        return getpass.getpass(prompt)


def parser():
    root = argparse.ArgumentParser(description="Evolutionary multi-agent platform")
    root.add_argument("--version", action="version", version=__version__)
    root.add_argument("--state", default=".evo")
    root.add_argument("--demo", action="store_true", help="Volatile UI preview; no agents execute")
    commands = root.add_subparsers(dest="command")
    commands.add_parser("task").add_argument("task_id")
    commands.add_parser("tasks").add_argument("--limit", type=int, default=100)
    commands.add_parser("messages").add_argument("--agent")
    commands.add_parser("agents").add_argument("--file", required=True,
                                                help="JSON array of {name,specialty} roles")
    for name in ("run", "group"):
        command = commands.add_parser(name)
        command.add_argument("prompt" if name == "run" else "message")
        if name == "run":
            command.add_argument("--no-memory", action="store_true")
            command.add_argument("--expect-file")
            command.add_argument("--expect-text")
            command.add_argument("--checks-file", help="JSON object of s1..s5 artifact checks")
        else:
            command.add_argument("--execute", action="store_true", help="Start the discussed task")
            command.add_argument("--no-memory", action="store_true")
            command.add_argument("--expect-file")
            command.add_argument("--expect-text")
            command.add_argument("--checks-file")
    commands.add_parser("resume").add_argument("task_id")
    command = commands.add_parser("dm")
    command.add_argument("agent")
    command.add_argument("message")
    for name in ("trace", "metrics"):
        commands.add_parser(name).add_argument("task_id")
    command = commands.add_parser("memory")
    command.add_argument("agent")
    command.add_argument("--query", default="")
    command = commands.add_parser("prepare-midtest", help="Prepare private sequential scenario workspaces, without executing")
    command.add_argument("archive")
    command.add_argument("--output", required=True)
    command = commands.add_parser("audit-midtest", help="Audit private exported SQLite evidence without inventing scores")
    command.add_argument("archive")
    command.add_argument("--output", required=True)
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
    commands.add_parser("provider-info", help="Validate and display non-secret provider settings (offline)")
    command = commands.add_parser("check-provider", help="One explicit live API smoke check; may incur usage")
    command.add_argument("--live", action="store_true", required=True,
                         help="Explicitly allow one real API request")
    command.add_argument("--json-object", action="store_true")
    command.add_argument("--ask-key", action="store_true", help="Enter a key privately for this request only")
    return root


def main(argv=None, application=None):
    root = parser()
    args = root.parse_args(argv)
    if not args.command:
        root.print_help()
        return 0
    try:
        if args.command == "prepare-midtest":
            from evo.interfaces.midtest import prepare_midtest
            print(json.dumps(prepare_midtest(args.archive, args.output), ensure_ascii=False, indent=2))
            return 0
        if args.command == "audit-midtest":
            from evo.interfaces.midtest import audit_midtest
            print(json.dumps(audit_midtest(args.archive, args.output), ensure_ascii=False, indent=2))
            return 0
        if args.command in ("provider-info", "check-provider"):
            from evo.config import ProviderConfig
            from evo.adapters.llm.diagnostics import check_provider, provider_info
            config = ProviderConfig.from_environment()
            if args.command == "provider-info":
                print(json.dumps(provider_info(config), ensure_ascii=False, indent=2))
                return 0
            key = read_private_key("API key (hidden, not saved): ") if args.ask_key else None
            report = check_provider(config, json_object=args.json_object, api_key=key)
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0 if report["status"] == "success" else 1
        if args.command in ("setup-key", "setup-hkgai-key"):
            path = save_key(args.provider, read_private_key("API key (hidden): "))
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
        if args.command == "agents":
            payload = {"agents": json.loads(Path(args.file).read_text(encoding="utf-8"))}
        if args.command in ("run", "group") and (args.command == "run" or args.execute):
            if bool(args.expect_file) != bool(args.expect_text):
                raise ValueError("Run requires both expected file and text")
            if args.checks_file and args.expect_file:
                raise ValueError("Choose checks file or expected file and text")
            payload = ({"prompt": args.prompt} if args.command == "run"
                       else {"message": args.message, "execute": True})
            payload["no_memory"] = args.no_memory
            if args.checks_file:
                payload["checks"] = json.loads(Path(args.checks_file).read_text(encoding="utf-8"))
            elif args.expect_file:
                payload["checks"] = {"s1": {"path": args.expect_file,
                                            "contains": args.expect_text}}
        elif args.command == "group":
            if args.expect_file or args.expect_text or args.checks_file or args.no_memory:
                raise ValueError("Group execution flags require --execute")
            payload = {"message": args.message}
        if args.command == "answer":
            payload = {"request_id": args.request_id}
            payload.update({"answer": args.text} if args.text is not None else {"approved": args.approve})
        result = app.dispatch(args.command, payload)
        from evo.interfaces.web.app import redact
        print(json.dumps(redact(result), ensure_ascii=False, indent=2))
        # Reading a failed task is a successful query, not a failed execution.
        if args.command in ("task", "tasks", "trace", "messages"):
            return 0
        return 1 if result.get("status") in ("failed", "blocked", "unavailable") or result.get("passed") is False else 0
    except ApplicationFailure as error:
        print(str(error), file=sys.stderr)
        return 1
    except (ApplicationUnavailable, ValueError, OSError) as error:
        # Backend exception text can contain request data and secrets.
        print(str(error) if isinstance(error, ApplicationUnavailable) else "Request failed; check input and application state", file=sys.stderr)
        return 2
    except Exception:
        print("Application operation failed", file=sys.stderr)
        return 2
