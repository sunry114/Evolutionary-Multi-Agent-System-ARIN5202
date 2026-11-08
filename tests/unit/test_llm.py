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
        self.env = patch.dict(os.environ, {"HKGAI_API_KEY": "test-secret"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_object_and_auth(self):
        calls = []
        def transport(*args):
            calls.append(args)
            return response()
        result = ChatProvider(ProviderConfig(), transport).complete([], json_object=True)
        self.assertEqual(result, {"ok": True})
        self.assertEqual(calls[0][0], "https://api1.hkgai.net/v1/chat/completions")
        self.assertEqual(calls[0][1]["Authorization"], "Bearer test-secret")
        self.assertEqual(calls[0][2]["model"], "t2_hkgai-v3_fp8_1m_e7")

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
