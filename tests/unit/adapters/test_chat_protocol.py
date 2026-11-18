import json
import unittest

from evo.adapters.harness.chat_protocol import as_sse, from_chat, to_chat


class ChatProtocolTests(unittest.TestCase):
    def response(self, **message):
        return {"id": "fixture", "choices": [{"finish_reason": "stop", "message": message}],
                "usage": {"prompt_tokens": 12, "completion_tokens": 5}}

    def test_tool_roundtrip_and_actual_usage(self):
        payload = {"system": [{"type": "text", "text": "system"}], "max_tokens": 32000,
                   "tools": [{"name": "read_file", "input_schema": {"type": "object"}}],
                   "tool_choice": {"type": "tool", "name": "read_file"},
                   "messages": [{"role": "assistant", "content": [
                       {"type": "tool_use", "id": "c1", "name": "read_file", "input": {"path": "a.md"}}]},
                       {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "c1",
                                                       "content": [{"type": "text", "text": "hello"}]}]}]}
        chat = to_chat(payload, "permitted")
        self.assertEqual(chat["model"], "permitted")
        self.assertEqual(chat["max_tokens"], 8192)
        self.assertFalse(chat["stream"])
        self.assertEqual(chat["messages"][-1], {"role": "tool", "tool_call_id": "c1", "content": "hello"})
        self.assertEqual(chat["tool_choice"]["function"]["name"], "read_file")
        result = self.response(content=None, tool_calls=chat["messages"][1]["tool_calls"])
        result["choices"][0]["finish_reason"] = "tool_calls"
        message = from_chat(result, "permitted")
        self.assertEqual(message["content"][0]["input"], {"path": "a.md"})
        self.assertEqual(message["usage"], {"input_tokens": 12, "output_tokens": 5})
        events = [json.loads(x[6:]) for x in as_sse(message).decode().splitlines() if x.startswith("data: ")]
        self.assertEqual(events[0]["type"], "message_start")
        self.assertEqual(events[2]["delta"]["type"], "input_json_delta")
        self.assertEqual(events[-2]["usage"]["output_tokens"], 5)
        self.assertEqual(events[-1]["type"], "message_stop")

    def test_text_and_sse_preserve_unicode(self):
        message = from_chat(self.response(content="你好"), "permitted")
        self.assertEqual(message["stop_reason"], "end_turn")
        self.assertIn("你好", as_sse(message).decode())

    def test_missing_usage_malformed_arguments_and_inconsistent_results_fail(self):
        for malformed in (None, "[]", "{broken"):
            result = self.response(content=None, tool_calls=[{"type": "function", "id": "c1",
                "function": {"name": "read_file", "arguments": malformed}}])
            result["choices"][0]["finish_reason"] = "tool_calls"
            with self.subTest(arguments=malformed), self.assertRaises((ValueError, TypeError)):
                from_chat(result, "permitted")
        result = self.response(content="hello")
        del result["usage"]
        with self.assertRaises(KeyError):
            from_chat(result, "permitted")
        result = self.response(content="hello")
        result["choices"][0]["finish_reason"] = "tool_calls"
        with self.assertRaises(ValueError):
            from_chat(result, "permitted")

    def test_unsupported_images_and_unknown_forced_tool_fail(self):
        for content in ([{"type": "image", "source": {}}], [{"type": "document"}]):
            with self.assertRaises(ValueError):
                to_chat({"messages": [{"role": "user", "content": content}]}, "permitted")
        with self.assertRaises(ValueError):
            to_chat({"messages": [], "tools": [{"name": "read_file", "input_schema": {}}],
                     "tool_choice": {"type": "tool", "name": "shell"}}, "permitted")
