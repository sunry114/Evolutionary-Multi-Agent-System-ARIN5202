"""Pluggable one-agent harnesses. No adapter starts implicitly on import."""


def create_harness(config, state_root, broker, *, model=None, credential_loader=None, provider=None):
    if config.adapter == "provider":
        from .provider import ProviderHarnessAdapter
        return ProviderHarnessAdapter(state_root, broker, config=config, provider=provider)
    if config.adapter == "baseline":
        from .baseline import BaselineHarnessAdapter
        return BaselineHarnessAdapter(state_root, broker, config=config)
    if config.adapter == "claude_code":
        from .claude_code import ClaudeCodeAdapter
        return ClaudeCodeAdapter(state_root, broker, config=config, model=model,
                                 credential_loader=credential_loader)
    from evo.ports.harness import HarnessError
    raise HarnessError("This harness is reserved but not implemented")
