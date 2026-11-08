"""Explicit live connection check. Never returns model output or credentials."""
from dataclasses import asdict, replace
from time import perf_counter

from evo.adapters.llm.client import ProviderError
from evo.adapters.llm.credentials import CredentialError
from evo.adapters.llm.factory import create_provider

HINTS = {
    "authentication": "Check whether the key is valid or has expired.",
    "permission": "Check key access to this model and service.",
    "endpoint": "Check the course API path; an approved /v1 base may be required.",
    "rate_limit": "Check quota and rate limits; retry later.",
    "tls": "Check system certificates; TLS verification must remain enabled.",
    "connection": "Check network reachability and provider availability.",
    "http_error": "Check provider status and the documented request format.",
    "invalid_response": "Check that the endpoint returns a chat-completion JSON response.",
    "invalid_completion": "Check model compatibility, JSON mode and output truncation.",
    "credential": "Use setup-key on Linux, a provider key environment variable, or --ask-key.",
}


def provider_info(config):
    settings = asdict(config)
    return {k: settings[k] for k in ("provider", "model", "base_url", "timeout", "retries")}


def check_provider(config, *, json_object=False, api_key=None):
    # One attempt: a diagnostic should not silently spend multiple requests.
    config = replace(config, retries=0)
    started = perf_counter()
    prompt = ('Return exactly this JSON object: {"ok": true}. No other text.'
              if json_object else "Reply with only the word OK.")
    report = {**provider_info(config), "check": "json_object" if json_object else "text"}
    try:
        result = create_provider(config).complete(
            [{"role": "user", "content": prompt}], json_object=json_object,
            max_tokens=64, api_key=api_key)
        valid = (isinstance(result, dict) and len(result) == 1 and result.get("ok") is True
                 if json_object else result.strip().upper() == "OK")
        report.update(status="success" if valid else "failed",
                      code="ok" if valid else "unexpected_reply")
        if not valid:
            report["hint"] = "Connection succeeded but the fixed smoke response was not followed."
    except CredentialError:
        report.update(status="failed", code="credential", hint=HINTS["credential"])
    except ProviderError as error:
        report.update(status="failed", code=error.code,
                      hint=HINTS.get(error.code, "Provider request failed."))
    except Exception:
        report.update(status="failed", code="provider_error", hint="Provider check failed.")
    report["elapsed_seconds"] = round(perf_counter() - started, 4)
    return report
