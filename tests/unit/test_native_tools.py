import json
import os
import unittest
from unittest.mock import Mock, patch

from evo.adapters.llm.client import ChatProvider, ProviderError
from evo.config import ProviderConfig


TOOLS = [{"name": "read_file", "description": "Read a file", "inputSchema": {
    "type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}]


def call(name="read_file", arguments='{"path":"report.md"}'):
    return {"type": "function", "function": {"name": name, "arguments": arguments}}


def reply(calls, finish="tool_calls"):
    return {"choices": [{"finish_reason": finish, "message": {"content": None, "tool_calls": calls}}]}


class NativeToolTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"})
        env.start()
        self.addCleanup(env.stop)

    def test_native_call_uses_function_schema_without_text_json_mode(self):
        transport = Mock(return_value=reply([call()]))
        provider = ChatProvider(ProviderConfig(), transport)
        result = provider.decide([{"role": "user", "content": "Read the report"}], TOOLS)
        self.assertTrue(provider.native_tools)
        self.assertFalse(ChatProvider(ProviderConfig(provider="hkgai")).native_tools)
        self.assertEqual(result, {"type": "tool", "name": "read_file", "arguments": {"path": "report.md"}})
        payload = transport.call_args.args[2]
        self.assertEqual(payload["tools"][0]["function"]["parameters"], TOOLS[0]["inputSchema"])
        self.assertEqual(payload["tool_choice"], "required")
        self.assertIs(payload["parallel_tool_calls"], False)
        self.assertNotIn("response_format", payload)

    def test_terminal_calls_are_proposals_for_independent_verification(self):
        for name, kind in (("finish_task", "done"), ("report_blocker", "blocked")):
            with self.subTest(name=name):
                provider = ChatProvider(ProviderConfig(), Mock(return_value=reply([call(name, '{"text":"evidence"}') ])))
                self.assertEqual(provider.decide([], TOOLS), {"type": kind, "text": "evidence"})

    def test_multiple_unknown_truncated_and_malformed_calls_execute_nothing(self):
        cases = [reply([call(), call()]), reply([call("unknown")]), reply([call(arguments="secret malformed")]),
                 reply([call(arguments="[]")]), reply([call("finish_task", '{"text":""}')]),
                 reply([call()], "length"), reply([], "stop"), {}]
        for raw in cases:
            with self.subTest(raw=raw), self.assertRaises(ProviderError) as raised:
                ChatProvider(ProviderConfig(), Mock(return_value=raw)).decide([], TOOLS)
            self.assertEqual(raised.exception.code, "invalid_completion")
            self.assertNotIn("secret", str(raised.exception) + str(raised.exception.details))

    def test_transport_retry_rules_are_shared_with_text_completions(self):
        transport = Mock(side_effect=[TimeoutError("secret"), reply([call()])])
        provider = ChatProvider(ProviderConfig(), transport, lambda _: None)
        self.assertEqual(provider.decide([], TOOLS)["name"], "read_file")
        self.assertEqual(transport.call_count, 2)
