from evo.adapters.llm.client import ChatProvider
from evo.config import ProviderConfig


class HKGAIProvider(ChatProvider):
    def __init__(self, config=None, **kwargs):
        config = config or ProviderConfig(provider="hkgai")
        if config.provider != "hkgai":
            raise ValueError("HKGAI provider configuration required")
        super().__init__(config, **kwargs)
