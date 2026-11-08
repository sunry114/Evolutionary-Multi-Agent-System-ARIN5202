"""Non-secret provider configuration."""
import math
import os
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
