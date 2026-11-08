import contextlib
import io
import json
import os
import ssl
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

from evo.config import ProviderConfig
from evo.adapters.llm.client import ChatProvider, ProviderError, post_json, MAX_RESPONSE
from evo.adapters.llm.credentials import CredentialError, validate_key
from evo.adapters.llm.diagnostics import check_provider
from evo.adapters.llm.factory import create_provider
from evo.interfaces.cli import main

CONFIG = {"EVO_PROVIDER": "hkgai", "EVO_BASE_URL": "", "EVO_MODEL": "",
          "EVO_TIMEOUT": "30", "EVO_RETRIES": "2"}


class ConfigurationTests(unittest.TestCase):
    def test_default_course_path_and_factory(self):
        with patch.dict(os.environ, CONFIG):
            config = ProviderConfig.from_environment()
            self.assertEqual(config.base_url, "https://api1.hkgai.net/v1")
            self.assertEqual(create_provider(config).config, config)

    def test_deepseek_environment(self):
        with patch.dict(os.environ, {**CONFIG, "EVO_PROVIDER": "deepseek", "EVO_MODEL": "course-model"}):
            provider = create_provider()
            self.assertEqual(provider.config.provider, "deepseek")
            self.assertEqual(provider.config.model, "course-model")

    def test_invalid_environment_cannot_enable_arbitrary_endpoint(self):
        for setting in ({"EVO_BASE_URL": "https://evil.test"}, {"EVO_TIMEOUT": "nan"},
                        {"EVO_RETRIES": "2.5"}, {"EVO_PROVIDER": "anthropic"}):
            with self.subTest(setting=setting), patch.dict(os.environ, {**CONFIG, **setting}):
                with self.assertRaises(ValueError):
                    ProviderConfig.from_environment(evaluation=True)

    def test_key_validation(self):
        for value in ("", "one\ntwo", "one two", "one\ttwo", "x" * 16385):
            with self.subTest(value_length=len(value)), self.assertRaises(CredentialError):
                validate_key(value)
        self.assertEqual(validate_key(" example "), "example")


class DiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.factory = patch("evo.adapters.llm.diagnostics.create_provider")
        self.create = self.factory.start()
        self.addCleanup(self.factory.stop)
        self.provider = self.create.return_value

    def test_one_request_bounded_tokens_and_no_content_in_report(self):
        self.provider.complete.return_value = "OK"
        report = check_provider(ProviderConfig(), api_key="example")
        self.assertEqual(report["status"], "success")
        self.assertEqual(self.create.call_args.args[0].retries, 0)
        self.assertEqual(self.provider.complete.call_args.kwargs["max_tokens"], 64)
        self.assertEqual(self.provider.complete.call_count, 1)
        self.assertNotIn("example", json.dumps(report))
        self.assertNotIn("messages", report)

    def test_json_and_unexpected_reply(self):
        self.provider.complete.return_value = {"ok": True}
        self.assertEqual(check_provider(ProviderConfig(), json_object=True)["status"], "success")
        self.provider.complete.return_value = {"ok": 1}
        self.assertEqual(check_provider(ProviderConfig(), json_object=True)["status"], "failed")
        self.provider.complete.return_value = "some unexpected model output"
        report = check_provider(ProviderConfig())
        self.assertEqual(report["code"], "unexpected_reply")
        self.assertNotIn("some unexpected", json.dumps(report))

    def test_secret_safe_errors(self):
        for error in (CredentialError("secret-test-value"),
                      ProviderError("secret-test-value", code="authentication"),
                      RuntimeError("secret-test-value")):
            self.provider.complete.side_effect = error
            report = check_provider(ProviderConfig())
            self.assertEqual(report["status"], "failed")
            self.assertNotIn("secret-test-value", json.dumps(report))

    def test_offline_cli_does_not_require_backend_or_key(self):
        with patch.dict(os.environ, CONFIG), contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(main(["provider-info"]), 0)
        self.assertIn("/v1", json.loads(out.getvalue())["base_url"])
        self.create.assert_not_called()

    def test_live_flag_required_before_any_call(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            main(["check-provider"])
        self.create.assert_not_called()

    def test_live_cli_exit_codes(self):
        with patch.dict(os.environ, CONFIG):
            for reply, expected in (("OK", 0), ("wrong", 1)):
                self.provider.complete.return_value = reply
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(main(["check-provider", "--live"]), expected)

    def test_hidden_key_not_saved(self):
        self.provider.complete.return_value = "OK"
        with patch.dict(os.environ, CONFIG), patch("getpass.getpass", return_value="temporary"), contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(main(["check-provider", "--live", "--ask-key"]), 0)
        self.assertEqual(self.provider.complete.call_args.kwargs["api_key"], "temporary")
        self.assertNotIn("temporary", out.getvalue())

    def test_hidden_prompt_refuses_echo_fallback(self):
        import getpass
        import warnings
        def unsafe_prompt(*args):
            warnings.warn("Cannot hide input", getpass.GetPassWarning)
            self.fail("Echoed key input must never be reached")
        with patch.dict(os.environ, CONFIG), patch("getpass.getpass", unsafe_prompt), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(["check-provider", "--live", "--ask-key"]), 2)
        self.create.assert_not_called()


class TransportTests(unittest.TestCase):
    def test_http_classification(self):
        for status, code in ((401, "authentication"), (403, "permission"),
                             (404, "endpoint"), (429, "rate_limit"), (503, "http_error")):
            transport = Mock(side_effect=HTTPError("x", status, "private-body", {}, None))
            with self.subTest(status=status), self.assertRaises(ProviderError) as result:
                ChatProvider(ProviderConfig(retries=0), transport).complete([], api_key="example")
            self.assertEqual(result.exception.code, code)
            self.assertNotIn("private-body", str(result.exception))

    def test_certificate_error_is_not_retried(self):
        transport = Mock(side_effect=URLError(ssl.SSLCertVerificationError("private-data")))
        with self.assertRaises(ProviderError) as result:
            ChatProvider(ProviderConfig(), transport).complete([], api_key="example")
        self.assertEqual(result.exception.code, "tls")
        self.assertEqual(transport.call_count, 1)

    def test_output_limit_and_per_call_key(self):
        transport = Mock(return_value={"choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}]})
        provider = ChatProvider(ProviderConfig(), transport)
        with patch("evo.adapters.llm.client.load_key") as load:
            self.assertEqual(provider.complete([], max_tokens=64, api_key="temporary"), "OK")
            load.assert_not_called()
        self.assertEqual(transport.call_args.args[2]["max_tokens"], 64)
        for limit in (0, -1, True, 32769):
            with self.assertRaises(ValueError):
                provider.complete([], max_tokens=limit, api_key="example")

    def test_transport_closes_response_and_bounds_read(self):
        for data, rejected in ((b'{"ok":true}', False), (b"x" * (MAX_RESPONSE + 1), True)):
            response = Mock()
            response.read.return_value = data
            context = Mock()
            context.__enter__ = Mock(return_value=response)
            context.__exit__ = Mock(return_value=False)
            opener = Mock()
            opener.open.return_value = context
            with patch("evo.adapters.llm.client.build_opener", return_value=opener):
                if rejected:
                    with self.assertRaises(ProviderError):
                        post_json("https://example.test", {}, {}, 30)
                else:
                    self.assertEqual(post_json("https://example.test", {}, {}, 30), {"ok": True})
            response.read.assert_called_once_with(MAX_RESPONSE + 1)
            context.__exit__.assert_called_once()
