"""Version-aware repetition control; never infer task completion from activity."""

import hashlib
import json
from dataclasses import asdict, is_dataclass


TRACKED = frozenset({"list_files", "read_file", "search_files", "check_python",
                     "write_file", "edit_file", "run_python", "list_artifacts"})


class ProgressGuard:
    def __init__(self, record, workspace, artifact_loader=None):
        self.record = record
        self.workspace = workspace
        self.artifact_loader = artifact_loader
        self.state = record.setdefault("progress_state", {
            "version": None, "counts": {}, "consecutive_suppressed": 0, "total_suppressed": 0})
        self.enabled = False

    def refresh(self):
        try:
            version = self.workspace.input_digest()
        except (ValueError, OSError):
            # Observation must remain possible even if a workspace cannot yet
            # be fingerprinted (for example an unsafe execution input).
            self.enabled = False
            self.state.update(version=None, counts={}, consecutive_suppressed=0)
            return
        self.enabled = True
        if version != self.state["version"]:
            self.state.update(version=version, counts={}, consecutive_suppressed=0)

    @staticmethod
    def key(name, arguments):
        args = dict(arguments)
        if name in ("list_files", "search_files"):
            args.setdefault("path", ".")
        if name in ("read_file", "write_file", "edit_file"):
            args.setdefault("purpose", "document")
        if name == "read_file":
            args.setdefault("start_line", 1)
            args.setdefault("max_lines", None)
        if name == "search_files":
            args.setdefault("max_results", 20)
        if name == "write_file":
            args.setdefault("append", False)
            args.setdefault("overwrite", True)
        return hashlib.sha256(json.dumps([name, args], sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    def repeated(self, name, arguments):
        key = self.observation_key(name, arguments)
        return self.enabled and name in TRACKED and key is not None and self.state["counts"].get(key, 0) >= 2

    def observation_key(self, name, arguments):
        key = self.key(name, arguments)
        if name == "list_artifacts":
            if self.artifact_loader is None:
                return None
            try:
                values = [asdict(item) if is_dataclass(item) else item for item in self.artifact_loader()]
                key += hashlib.sha256(json.dumps(values, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            except (ValueError, TypeError, OSError):
                return None
        return key

    def note(self, name, arguments, outcome):
        self.refresh()
        self.state["consecutive_suppressed"] = 0
        if self.enabled and name in TRACKED:
            key = self.observation_key(name, arguments)
            if key is None:
                return
            count = self.state["counts"].get(key, 0) + 1
            self.state["counts"][key] = max(count, 2) if outcome.get("replayed") else count

    def suppress(self):
        self.state["consecutive_suppressed"] += 1
        self.state["total_suppressed"] += 1
        return self.state["consecutive_suppressed"] >= 6

    def summary(self, acceptance, artifacts, remaining_turns):
        paths = {item["path"] for item in artifacts}
        return {"unchanged_input_version": self.state["version"],
                "repetition_guard_enabled": self.enabled,
                "suppressed_repetitions": self.state["total_suppressed"],
                "remaining_model_turns": remaining_turns,
                "required_delivery": acceptance,
                "delivery_recorded": bool(acceptance and acceptance["path"] in paths),
                "next_step": "Use observed results to implement, repair or test. When assigned work is complete, "
                             "produce the required delivery and finish. Report a specific blocker if unable to proceed. "
                             "Do not repeatedly list or reread unchanged inputs."}
