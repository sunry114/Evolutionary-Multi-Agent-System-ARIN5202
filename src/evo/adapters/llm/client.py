"""Bounded, non-streaming chat completion adapter with secret-safe errors."""
import json
import ssl
import time
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from evo.adapters.llm.credentials import load_key

MAX_RESPONSE = 2 * 1024 * 1024


class ProviderError(RuntimeError):
    def __init__(self, message, *, code="provider_error", details=None):
        super().__init__(message)
        self.code = code
        # Only locally generated categories and counts may enter durable logs.
        details = details if isinstance(details, dict) else {}
        self.details = {}
        if details.get("completion_reason") in (
                "truncated", "unsupported_finish", "empty_content", "invalid_content",
                "invalid_json", "not_object", "malformed_envelope", "invalid_tool_call"):
            self.details["completion_reason"] = details["completion_reason"]
        if details.get("finish_reason") in ("stop", "length", "content_filter", "tool_calls", "function_call"):
            self.details["finish_reason"] = details["finish_reason"]
        for name in ("content_chars", "completion_tokens"):
            if type(details.get(name)) is int and details[name] >= 0:
                self.details[name] = details[name]
        if details.get("native_issue") in ("envelope", "tool_count", "unknown_function", "arguments_json",
                                            "arguments_type", "terminal_arguments"):
            self.details["native_issue"] = details["native_issue"]


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def post_json(url, headers, payload, timeout):
    request = Request(url, json.dumps(payload).encode("utf-8"), headers, method="POST")
    # Never forward credentials through redirects or environment-defined proxies.
    from urllib.request import ProxyHandler
    with build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=timeout) as response:
        data = response.read(MAX_RESPONSE + 1)
    if len(data) > MAX_RESPONSE:
        raise ProviderError("Provider response exceeds size limit")
    return json.loads(data)


class ChatProvider:
    def __init__(self, config, transport=post_json, sleep=time.sleep):
        self.config = config
        self._transport = transport
        self._sleep = sleep

    @property
    def native_tools(self):
        return self.config.provider == "deepseek"

    def decide(self, messages, tools):
        """Propose exactly one action via native functions; never execute it here."""
        terminal = {"type": "object", "properties": {"text": {"type": "string"}},
                    "required": ["text"], "additionalProperties": False}
        functions = [{"type": "function", "function": {
            "name": tool["name"], "description": tool["description"], "parameters": tool["inputSchema"]}}
                     for tool in tools]
        for name, description in (("finish_task", "Report completed work and evidence; delivery is independently checked."),
                                  ("report_blocker", "Report a specific blocker preventing further work.")):
            functions.append({"type": "function", "function": {
                "name": name, "description": description, "parameters": terminal}})
        result = self._request({"model": self.config.model, "messages": messages, "stream": False,
                                "tools": functions, "tool_choice": "required", "parallel_tool_calls": False},
                               load_key(self.config.provider))
        details = {}
        issue = "envelope"
        try:
            choice = result["choices"][0]
            details["finish_reason"] = choice["finish_reason"]
            usage = result.get("usage", {})
            if isinstance(usage, dict):
                details["completion_tokens"] = usage.get("completion_tokens")
            if choice["finish_reason"] == "length":
                details["completion_reason"] = "truncated"
                raise ValueError()
            calls = choice["message"]["tool_calls"]
            issue = "tool_count"
            if choice["finish_reason"] != "tool_calls" or not isinstance(calls, list) or len(calls) != 1:
                raise ValueError()
            call = calls[0]
            name = call["function"]["name"]
            issue = "unknown_function"
            if call["type"] != "function" or name not in {f["function"]["name"] for f in functions}:
                raise ValueError()
            issue = "arguments_json"
            arguments = json.loads(call["function"]["arguments"])
            issue = "arguments_type"
            if not isinstance(arguments, dict):
                raise ValueError()
            if name in ("finish_task", "report_blocker"):
                issue = "terminal_arguments"
                if set(arguments) != {"text"} or not isinstance(arguments["text"], str) or not arguments["text"].strip():
                    raise ValueError()
                return {"type": "done" if name == "finish_task" else "blocked", **arguments}
            return {"type": "tool", "name": name, "arguments": arguments}
        except (KeyError, IndexError, TypeError, ValueError):
            details.setdefault("completion_reason", "invalid_tool_call")
            details["native_issue"] = issue
            raise ProviderError("Invalid native function call", code="invalid_completion", details=details) from None

    def complete(self, messages, *, json_object=False, max_tokens=None, api_key=None):
        from evo.adapters.llm.credentials import validate_key
        key = load_key(self.config.provider) if api_key is None else validate_key(api_key)
        if max_tokens is not None and (type(max_tokens) is not int or not 1 <= max_tokens <= 32768):
            raise ValueError("Invalid output token limit")
        payload = {"model": self.config.model, "messages": messages, "stream": False}
        if json_object:
            payload["response_format"] = {"type": "json_object"}
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        result = self._request(payload, key)
        details = {}
        try:
            choice = result["choices"][0]
            content = choice["message"]["content"]
            details = {"finish_reason": choice["finish_reason"],
                       "content_chars": len(content) if isinstance(content, str) else 0}
            usage = result.get("usage")
            if isinstance(usage, dict):
                details["completion_tokens"] = usage.get("completion_tokens")
            if choice["finish_reason"] != "stop":
                details["completion_reason"] = "truncated" if choice["finish_reason"] == "length" else "unsupported_finish"
                raise ValueError()
            if not isinstance(content, str):
                details["completion_reason"] = "invalid_content"
                raise ValueError()
            if not content.strip():
                details["completion_reason"] = "empty_content"
                raise ValueError()
            if json_object:
                try:
                    content = json.loads(content)
                except json.JSONDecodeError:
                    details["completion_reason"] = "invalid_json"
                    raise
                if not isinstance(content, dict):
                    details["completion_reason"] = "not_object"
                    raise ValueError()
            return content
        except (KeyError, IndexError, TypeError, ValueError):
            details.setdefault("completion_reason", "malformed_envelope")
            raise ProviderError("Empty, truncated or malformed model completion",
                                code="invalid_completion", details=details) from None

    def _request(self, payload, key):
        for attempt in range(self.config.retries + 1):
            try:
                result = self._transport(
                    self.config.base_url + "/chat/completions",
                    {"Authorization": "Bearer " + key, "Content-Type": "application/json"},
                    payload, self.config.timeout)
                return result
            except HTTPError as error:
                retryable = error.code in (429, 500, 502, 503, 504)
                error.close()
                if not retryable or attempt == self.config.retries:
                    code = {401: "authentication", 403: "permission", 404: "endpoint",
                            429: "rate_limit"}.get(error.code, "http_error")
                    raise ProviderError("Provider HTTP request failed", code=code) from None
            except URLError as error:
                if isinstance(error.reason, ssl.SSLCertVerificationError):
                    raise ProviderError("Provider TLS certificate verification failed", code="tls") from None
                if attempt == self.config.retries:
                    raise ProviderError("Provider connection failed", code="connection") from None
            except (TimeoutError, ConnectionError):
                if attempt == self.config.retries:
                    raise ProviderError("Provider connection failed", code="connection") from None
            except Exception:
                raise ProviderError("Invalid provider response", code="invalid_response") from None
            self._sleep(min(2 ** attempt, 4))
