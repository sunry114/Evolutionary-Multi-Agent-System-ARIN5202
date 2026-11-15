"""Offline browser-only provider, explicitly selected through a test factory."""
import re
from evo.interfaces.sqlite_application import SQLiteApplication


class FixtureProvider:
    def complete(self, messages, json_object=False):
        if "force-provider-error" in messages[-1]["content"]:
            raise RuntimeError("private-fixture-error")
        agent = re.search(r"You are ([^,]+),", messages[0]["content"]).group(1)
        return {"reply": "Fixture reply from " + agent}


def create_application(*, state):
    return SQLiteApplication(state, provider=FixtureProvider())
