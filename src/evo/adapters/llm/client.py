"""Bounded, non-streaming chat completion adapter with secret-safe errors."""
import json
import time
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from evo.adapters.llm.credentials import load_key

MAX_RESPONSE = 2 * 1024 * 1024


class ProviderError(RuntimeError):
    pass


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

    def complete(self, messages, *, json_object=False):
        key = load_key(self.config.provider)
        payload = {"model": self.config.model, "messages": messages, "stream": False}
        if json_object:
            payload["response_format"] = {"type": "json_object"}
        for attempt in range(self.config.retries + 1):
            try:
                result = self._transport(
                    self.config.base_url + "/chat/completions",
                    {"Authorization": "Bearer " + key, "Content-Type": "application/json"},
                    payload, self.config.timeout)
                break
            except HTTPError as error:
                retryable = error.code in (429, 500, 502, 503, 504)
                error.close()
                if not retryable or attempt == self.config.retries:
                    raise ProviderError("Provider HTTP request failed") from None
            except (TimeoutError, URLError, ConnectionError):
                if attempt == self.config.retries:
                    raise ProviderError("Provider connection failed") from None
            except Exception:
                raise ProviderError("Invalid provider response") from None
            self._sleep(min(2 ** attempt, 4))
        try:
            choice = result["choices"][0]
            content = choice["message"]["content"]
            if choice["finish_reason"] != "stop" or not isinstance(content, str) or not content.strip():
                raise ValueError()
            if json_object:
                content = json.loads(content)
                if not isinstance(content, dict):
                    raise ValueError()
            return content
        except (KeyError, IndexError, TypeError, ValueError):
            raise ProviderError("Empty, truncated or malformed model completion") from None
