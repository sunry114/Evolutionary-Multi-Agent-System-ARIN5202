"""Team integration entry point: same configuration for CLI and agent backend."""
from evo.config import ProviderConfig
from evo.adapters.llm.deepseek import DeepSeekProvider
from evo.adapters.llm.hkgai import HKGAIProvider


def create_provider(config=None):
    config = config or ProviderConfig.from_environment()
    provider_type = {"hkgai": HKGAIProvider, "deepseek": DeepSeekProvider}[config.provider]
    return provider_type(config)
