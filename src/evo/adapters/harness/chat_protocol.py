"""Plain-text/tool protocol translation for the permitted HKGAI Chat API."""
import json
import re


def text_content(value):
    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        raise ValueError("Unsupported model content")
    if any(not isinstance(b, dict) or b.get("type") != "text" or not isinstance(b.get("text"), str) for b in value):
        raise ValueError("Only text content is supported")
    return "\n".join(b["text"] for b in value)


def to_chat(payload, model):
    messages = []
    if payload.get("system"):
        messages.append({"role": "system", "content": text_content(payload["system"])})
    for message in payload["messages"]:
        role, content = message["role"], message["content"]
        if role not in ("user", "assistant"):
            raise ValueError("Unsupported message role")
        if isinstance(content, str):
            messages.append({"role": role, "content": content})
            continue
        if not isinstance(content, list):
            raise ValueError("Invalid message blocks")
        text, calls, results = [], [], []
        for block in content:
            kind = block["type"]
            if kind == "text":
                text.append(text_content([block]))
            elif role == "assistant" and kind == "tool_use":
                calls.append({"id": block["id"], "type": "function",
                              "function": {"name": block["name"], "arguments": json.dumps(block["input"])}})
            elif role == "user" and kind == "tool_result":
                results.append({"role": "tool", "tool_call_id": block["tool_use_id"],
                                "content": ("Tool error: " if block.get("is_error") else "")
                                + text_content(block.get("content", ""))})
            elif role == "assistant" and kind in ("thinking", "redacted_thinking"):
                continue  # Never forward provider-specific reasoning blocks.
            else:
                raise ValueError("Unsupported model content block")
        messages.extend(results)
        if text or calls:
            item = {"role": role, "content": "\n".join(text) if text else None}
            if calls:
                item["tool_calls"] = calls
            messages.append(item)
    maximum = payload.get("max_tokens", 4096)
    if type(maximum) is not int or maximum < 1:
        raise ValueError("Invalid output bound")
    body = {"model": model, "messages": messages, "stream": False, "max_tokens": min(maximum, 8192)}
    if payload.get("tools"):
        tools = []
        for tool in payload["tools"]:
            name = tool["name"]
            if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", name):
                raise ValueError("Invalid tool name")
            tools.append({"type": "function", "function": {"name": name,
                         "description": tool.get("description", ""), "parameters": tool["input_schema"]}})
        body.update(tools=tools, parallel_tool_calls=False)
        choice = payload.get("tool_choice", {"type": "auto"})
        kind = choice["type"]
        if kind in ("auto", "any", "none"):
            body["tool_choice"] = {"auto": "auto", "any": "required", "none": "none"}[kind]
        elif kind == "tool":
            if choice["name"] not in {t["function"]["name"] for t in tools}:
                raise ValueError("Tool choice is not registered")
            body["tool_choice"] = {"type": "function", "function": {"name": choice["name"]}}
        else:
            raise ValueError("Unsupported tool choice")
    if payload.get("stop_sequences"):
        body["stop"] = payload["stop_sequences"]
    return body


def from_chat(result, model):
    choice = result["choices"][0]
    reason = choice["finish_reason"]
    if reason not in ("stop", "tool_calls", "length"):
        raise ValueError("Unsupported completion result")
    content = []
    message = choice["message"]
    if message.get("content"):
        if not isinstance(message["content"], str):
            raise ValueError("Invalid text result")
        content.append({"type": "text", "text": message["content"]})
    calls = message.get("tool_calls", [])
    for call in calls:
        arguments = json.loads(call["function"]["arguments"])
        if call.get("type") != "function" or not isinstance(arguments, dict):
            raise ValueError("Invalid tool arguments")
        content.append({"type": "tool_use", "id": call["id"],
                        "name": call["function"]["name"], "input": arguments})
    if not content or bool(calls) != (reason == "tool_calls"):
        raise ValueError("Inconsistent model result")
    usage = result["usage"]
    if any(type(usage.get(k)) is not int or usage[k] < 0 for k in ("prompt_tokens", "completion_tokens")):
        raise ValueError("Missing actual token usage")
    return {"id": result["id"], "type": "message", "role": "assistant", "model": model,
            "content": content, "stop_reason": {"stop": "end_turn", "tool_calls": "tool_use", "length": "max_tokens"}[reason],
            "stop_sequence": None, "usage": {"input_tokens": usage["prompt_tokens"],
                                           "output_tokens": usage["completion_tokens"]}}


def as_sse(message):
    def event(kind, data):
        return "event: " + kind + "\ndata: " + json.dumps({"type": kind, **data}, ensure_ascii=False) + "\n\n"
    start = {**message, "content": [], "stop_reason": None,
             "usage": {**message["usage"], "output_tokens": 0}}
    output = event("message_start", {"message": start})
    for index, block in enumerate(message["content"]):
        if block["type"] == "text":
            output += event("content_block_start", {"index": index, "content_block": {"type": "text", "text": ""}})
            delta = {"type": "text_delta", "text": block["text"]}
        else:
            output += event("content_block_start", {"index": index, "content_block": {**block, "input": {}}})
            delta = {"type": "input_json_delta", "partial_json": json.dumps(block["input"], ensure_ascii=False)}
        output += event("content_block_delta", {"index": index, "delta": delta})
        output += event("content_block_stop", {"index": index})
    output += event("message_delta", {"delta": {"stop_reason": message["stop_reason"], "stop_sequence": None},
                                      "usage": {"output_tokens": message["usage"]["output_tokens"]}})
    output += event("message_stop", {})
    return output.encode("utf-8")
