"""Compatibility harness for C10 actions; C11 owns the future model/DAG loop."""

import time

from .runtime import ManagedHarness


class BaselineHarnessAdapter(ManagedHarness):
    adapter_name = "baseline"

    def _execute(self, control, request):
        if not request.actions:
            return "failed", "", "baseline_requires_explicit_actions", None
        deadline = time.monotonic() + self.config.timeout
        for index in range(control.record["baseline_index"], len(request.actions)):
            if control.stop.is_set():
                return "cancelled", "", "cancelled", None
            if time.monotonic() > deadline:
                return "timed_out", "", "timeout", None
            action = request.actions[index]
            result = self._call(control, request, action.name, dict(action.arguments))
            if result["status"] != "success":
                waiting = result["status"] in ("waiting_human", "waiting_dependency")
                return result["status"], "", "" if waiting else "platform_action_stopped", None
            control.record["baseline_index"] = index + 1
            self._save(control)
            if time.monotonic() > deadline:
                return "timed_out", "", "timeout", None
        return "success", "Explicit baseline actions completed; goal verification remains with C11.", "", 0
