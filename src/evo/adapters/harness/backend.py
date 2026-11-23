"""Load per-run configuration from the selected provider."""

import json
import os
import stat
from pathlib import Path

from evo.adapters.llm.credentials import validate_key
from evo.config import HarnessModelConfig
from evo.ports.harness import HarnessError


def local_claude_backend(path=None, *, development=False, evaluation=False):
    """Opt-in only; import endpoint/model/token, never hooks, permissions or plugins."""
    if development is not True or evaluation is not False:
        raise HarnessError("Local Claude configuration requires explicit development mode")
    path = Path(path) if path else Path.home() / ".claude" / "settings.json"

    def read():
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "r", encoding="utf-8") as stream:
                info = os.fstat(stream.fileno())
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                        or info.st_mode & 0o022 or info.st_size > 65536):
                    raise ValueError()
                data = json.loads(stream.read(65537))
            env = data["env"]
            return data, env
        except Exception:
            raise HarnessError("Local Claude configuration is unavailable or unsafe") from None

    data, env = read()
    try:
        model = HarnessModelConfig(provider="development", endpoint=env["ANTHROPIC_BASE_URL"],
                                   model=env.get("ANTHROPIC_MODEL") or data["model"], development=True)
    except Exception:
        raise HarnessError("Local Claude endpoint/model configuration is invalid") from None

    def credential_loader():
        current, values = read()
        if (values.get("ANTHROPIC_BASE_URL", "").rstrip("/") != model.endpoint
                or (values.get("ANTHROPIC_MODEL") or current.get("model")) != model.model):
            raise HarnessError("Local backend configuration changed; reload it explicitly")
        try:
            return validate_key(values.get("ANTHROPIC_AUTH_TOKEN") or values["ANTHROPIC_API_KEY"])
        except Exception:
            raise HarnessError("Local Claude credential is unavailable") from None

    return model, credential_loader
