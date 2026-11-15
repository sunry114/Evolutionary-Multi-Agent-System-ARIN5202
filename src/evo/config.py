"""Non-secret provider configuration."""
import math
import os
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

ENDPOINTS = {
    "deepseek": ("https://api.deepseek.com", "https://api.deepseek.com/v1"),
    "hkgai": ("https://api1.hkgai.net/v1", "https://api1.hkgai.net",
              "https://studio-api.hkchat.app/models/v1"),
}
DEFAULT_MODELS = {"deepseek": "deepseek-chat", "hkgai": "t2_hkgai-v3_fp8_1m_e7"}


@dataclass(frozen=True)
class ProviderConfig:
    provider: str = "hkgai"
    model: str = ""
    base_url: str = ""
    timeout: float = 60
    retries: int = 2
    development: bool = False
    evaluation: bool = False

    def __post_init__(self):
        if self.provider not in ENDPOINTS:
            raise ValueError("Only DeepSeek and HKGAI providers are permitted")
        base = (self.base_url or ENDPOINTS[self.provider][0]).rstrip("/")
        parsed = urlsplit(base)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username
                or parsed.password or parsed.query or parsed.fragment):
            raise ValueError("Provider endpoint must be a plain HTTPS base URL")
        if base not in ENDPOINTS[self.provider] and (not self.development or self.evaluation):
            raise ValueError("Unapproved provider endpoint; custom URLs require development mode")
        if (not math.isfinite(self.timeout) or not 0 < self.timeout <= 300
                or type(self.retries) is not int or not 0 <= self.retries <= 4):
            raise ValueError("Invalid timeout or retry limit")
        object.__setattr__(self, "base_url", base)
        object.__setattr__(self, "model", self.model or DEFAULT_MODELS[self.provider])

    @classmethod
    def from_environment(cls, *, evaluation=False):
        """Load only documented, non-secret settings; never enable dev mode implicitly."""
        try:
            return cls(
                provider=os.environ.get("EVO_PROVIDER", "hkgai"),
                model=os.environ.get("EVO_MODEL", ""),
                base_url=os.environ.get("EVO_BASE_URL", ""),
                timeout=float(os.environ.get("EVO_TIMEOUT", "60")),
                retries=int(os.environ.get("EVO_RETRIES", "2")),
                evaluation=evaluation,
            )
        except (ValueError, TypeError, OverflowError):
            raise ValueError("Invalid provider environment configuration") from None


@dataclass(frozen=True)
class HarnessConfig:
    adapter: str = "baseline"
    cli_path: str = "claude"
    cli_version: str = "2.1.311"
    timeout: float = 180
    max_output_bytes: int = 4 * 1024 * 1024
    max_tool_calls: int = 20

    def __post_init__(self):
        if self.adapter not in ("baseline", "claude_code", "pi", "custom"):
            raise ValueError("Unknown harness adapter")
        if (not isinstance(self.cli_path, str) or not self.cli_path
                or not re.fullmatch(r"2\.\d+\.\d+", self.cli_version)
                or type(self.timeout) not in (int, float) or not math.isfinite(self.timeout)
                or not 0 < self.timeout <= 600
                or type(self.max_output_bytes) is not int or not 1024 <= self.max_output_bytes <= 8 * 1024 * 1024
                or type(self.max_tool_calls) is not int or not 1 <= self.max_tool_calls <= 20):
            raise ValueError("Invalid harness resource limits or CLI version")


@dataclass(frozen=True)
class HarnessModelConfig:
    """Anthropic wire protocol; provider identity is independent of that protocol."""

    provider: str = "deepseek"
    endpoint: str = "https://api.deepseek.com/anthropic"
    model: str = ""
    development: bool = False
    evaluation: bool = False

    def __post_init__(self):
        try:
            endpoint = self.endpoint.rstrip("/")
            parts = urlsplit(endpoint)
            approved = ((self.provider == "deepseek" and endpoint == "https://api.deepseek.com/anthropic")
                        or (self.provider == "hkgai" and endpoint in ENDPOINTS["hkgai"]))
            if (not parts.hostname or parts.username or parts.password or parts.query or parts.fragment
                    or parts.scheme not in ("http", "https") or not 0 < (parts.port or 80) < 65536
                    or any(ord(c) <= 32 for c in endpoint) or "\\" in endpoint or "%" in endpoint
                    or any(p in (".", "..") for p in parts.path.split("/"))
                    or type(self.development) is not bool or type(self.evaluation) is not bool
                    or (not approved and (self.provider != "development" or not self.development or self.evaluation))
                    or (parts.scheme != "https" and not self.development)
                    or (self.evaluation and self.development)
                    or not isinstance(self.model, str) or not re.fullmatch(r"[A-Za-z0-9_./:\[\]-]{1,200}", self.model)):
                raise ValueError()
        except (ValueError, TypeError, AttributeError):
            raise ValueError("Invalid harness backend; third-party endpoints require explicit development mode") from None
        object.__setattr__(self, "endpoint", endpoint)
