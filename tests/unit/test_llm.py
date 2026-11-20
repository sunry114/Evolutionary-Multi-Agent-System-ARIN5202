import json
import os
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from evo.adapters.llm.client import ChatProvider, ProviderError
from evo.config import ProviderConfig


def response(content='{"ok": true}', finish="stop"):
    return {"choices": [{"message": {"content": content}, "finish_reason": finish}]}


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"HKGAI_API_KEY": "test-secret", "DEEPSEEK_API_KEY": "test-secret"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_object_and_auth(self):
        calls = []
        def transport(*args):
            calls.append(args)
            return response()
        result = ChatProvider(ProviderConfig(), transport).complete([], json_object=True)
        self.assertEqual(result, {"ok": True})
        self.assertEqual(calls[0][0], "https://api.deepseek.com/chat/completions")
        self.assertEqual(calls[0][1]["Authorization"], "Bearer test-secret")
        self.assertEqual(calls[0][2]["model"], "deepseek-chat")

    def test_retries_transient_errors(self):
        for error in (HTTPError("x", 429, "test-secret", {}, None),
                      HTTPError("x", 503, "test-secret", {}, None),
                      TimeoutError("test-secret"), URLError("test-secret")):
            with self.subTest(error=type(error)):
                from unittest.mock import Mock
                transport = Mock(side_effect=[error, response()])
                sleeps = []
                self.assertEqual(ChatProvider(ProviderConfig(), transport, sleeps.append).complete([], json_object=True), {"ok": True})
                self.assertEqual(transport.call_count, 2)
                self.assertEqual(sleeps, [1])

    def test_no_retry_auth_and_secret_safe_errors(self):
        from unittest.mock import Mock
        for error in (HTTPError("x", 401, "test-secret", {}, None), ValueError("test-secret")):
            transport = Mock(side_effect=error)
            with self.assertRaises(ProviderError) as raised:
                ChatProvider(ProviderConfig(), transport).complete([])
            self.assertNotIn("test-secret", str(raised.exception))
            self.assertEqual(transport.call_count, 1)

    def test_retry_exhaustion(self):
        from unittest.mock import Mock
        transport = Mock(side_effect=TimeoutError("test-secret"))
        with self.assertRaises(ProviderError):
            ChatProvider(ProviderConfig(), transport, lambda _: None).complete([])
        self.assertEqual(transport.call_count, 3)

    def test_reject_bad_completions(self):
        for content, finish in [("", "stop"), ("{}", "length"), ("[]", "stop"),
                                ("null", "stop"), ("broken", "stop")]:
            with self.subTest(content=content, finish=finish), self.assertRaises(ProviderError):
                ChatProvider(ProviderConfig(), lambda *a: response(content, finish)).complete([], json_object=True)

    def test_completion_diagnostics_distinguish_truncation_and_malformed_json(self):
        for content, finish, reason in (("private unfinished data", "length", "truncated"),
                                         ("private malformed data", "stop", "invalid_json"),
                                         ("  ", "stop", "empty_content"),
                                         ("[]", "stop", "not_object"),
                                         (None, "stop", "invalid_content")):
            with self.subTest(reason=reason):
                raw = {**response(content, finish), "usage": {"completion_tokens": 8192}}
                with self.assertRaises(ProviderError) as raised:
                    ChatProvider(ProviderConfig(), lambda *a: raw).complete([], json_object=True)
                self.assertEqual(raised.exception.code, "invalid_completion")
                self.assertEqual(raised.exception.details["completion_reason"], reason)
                self.assertEqual(raised.exception.details["completion_tokens"], 8192)
                self.assertNotIn("private", str(raised.exception) + str(raised.exception.details))

    def test_completion_diagnostics_discard_arbitrary_provider_text(self):
        error = ProviderError("safe", details={"completion_reason": "secret", "finish_reason": "secret",
                                             "content_chars": "secret", "completion_tokens": -1, "body": "secret"})
        self.assertEqual(error.details, {})
        for raw in ({}, {"choices": []}, {"choices": [{"message": {"content": "secret"}, "finish_reason": []}]}):
            with self.subTest(raw=raw), self.assertRaises(ProviderError) as raised:
                ChatProvider(ProviderConfig(), lambda *a: raw).complete([], json_object=True)
            self.assertNotIn("secret", str(raised.exception) + str(raised.exception.details))

    def test_configuration_boundary(self):
        for config in ({"provider": "other"}, {"base_url": "http://api1.hkgai.net"},
                       {"base_url": "https://attacker.test"}, {"timeout": float("nan")},
                       {"development": True, "evaluation": True, "base_url": "https://test.test"}):
            with self.subTest(config=config), self.assertRaises(ValueError):
                ProviderConfig(**config)
        self.assertEqual(ProviderConfig(development=True, base_url="https://test.test").base_url,
                         "https://test.test")

    def test_redirects_are_refused(self):
        from evo.adapters.llm.client import NoRedirect
        self.assertIsNone(NoRedirect().redirect_request(None, None, 302, "", {}, "https://evil.test"))
