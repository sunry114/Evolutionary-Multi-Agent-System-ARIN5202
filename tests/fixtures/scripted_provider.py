# tests/fixtures/scripted_provider.py

class ScriptedProvider:
    """Deterministic offline LLM replacement for characterization tests."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def complete_json(self, system, user, max_tokens=2000):
        self.calls.append(
            {
                "system": system,
                "user": user,
                "max_tokens": max_tokens,
            }
        )
        if not self.replies:
            raise AssertionError("Unexpected provider call")
        return self.replies.pop(0)

    def assert_exhausted(self):
        if self.replies:
            raise AssertionError(
                f"{len(self.replies)} scripted replies were not consumed"
            )