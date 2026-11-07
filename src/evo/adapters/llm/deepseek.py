from evo.adapters.llm.client import ChatProvider
from evo.config import ProviderConfig


class DeepSeekProvider(ChatProvider):
    def __init__(self, config=None, **kwargs):
        config = config or ProviderConfig(provider="deepseek")
        if config.provider != "deepseek":
            raise ValueError("DeepSeek provider configuration required")
        super().__init__(config, **kwargs)
