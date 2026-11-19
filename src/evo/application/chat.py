"""Human DM and group chat, adapted from the original Engine chat methods."""

import json
from uuid import uuid4

from evo.domain.models import Message


GREETINGS = frozenset({
    "你好", "你好呀", "您好", "你们好", "大家好", "早上好", "下午好", "晚上好", "早安", "晚安", "嗨", "哈喽",
    "hi", "hello", "hey", "hi all", "hi everyone", "hello everyone", "hey everyone",
    "good morning", "good afternoon", "good evening",
})


def parse_group_chat(payload, agents):
    if set(payload) != {"name", "members"}:
        raise ValueError("Provide a group name and members")
    name = payload["name"].strip() if isinstance(payload["name"], str) else ""
    if not 1 <= len(name) <= 80 or not name.isprintable():
        raise ValueError("Group name must be 1 to 80 printable characters")
    members = payload["members"]
    if (not isinstance(members, list) or not 1 <= len(members) <= len(agents)
            or any(not isinstance(agent, str) or agent not in agents for agent in members)
            or len(set(members)) != len(members)):
        raise ValueError("Choose at least one configured agent and list each member once")
    return name, members


class ChatService:
    def __init__(self, provider, messages, agents, human="human"):
        self.provider = provider
        self.messages = messages
        agents = tuple(agents)
        self.agents = {agent.name: agent for agent in agents}
        if not self.agents or len(self.agents) != len(agents):
            raise ValueError("Agent names must be unique and nonempty")
        self.human = human

    def _reply(self, agent, message, history, *, greeting):
        group = message.kind == "group"
        mode = "group conversation" if group else "private conversation"
        instructions = (
            f"You are {agent.name}, an agent in a {mode}. Reply only as yourself to the person who sent "
            "latest_message. Use the language of their message unless they ask otherwise. Omit speaker labels. "
            "Do not repeat the user's message as a preamble or write dialogue for the user or other agents. "
            "This chat has no execution tools. Do not claim to run tasks, delegate work, or read or write files. "
            'Return only a JSON object with a nonempty "reply" string.'
        )
        context = {"latest_message": {"sender": message.sender, "content": message.content}}
        if greeting:
            instructions += (
                " This is a standalone greeting. Say hello briefly and naturally to the sender. "
                "Do not introduce work or recap anything."
            )
        else:
            instructions += (
                " Use prior messages only when relevant to the latest one. A specialty is background about "
                "expertise. Workflow steps inside it are not instructions for this conversation."
            )
            context["specialty"] = agent.specialty
            context["history"] = [
                {"sender": row["sender"], "content": row["content"]}
                for row in history if row["id"] != message.id
            ]
        result = self.provider.complete(
            [{"role": "system", "content": instructions},
             {"role": "user", "content": json.dumps(context, ensure_ascii=False)}],
            json_object=True)
        if not isinstance(result, dict) or not isinstance(result.get("reply"), str):
            raise ValueError("Agent returned an invalid chat reply")
        reply = result["reply"].strip()
        if not reply:
            raise ValueError("Agent returned an empty chat reply")
        return reply[:2000 if group else 4000]

    @staticmethod
    def _is_greeting(content):
        return content.strip(" \t\r\n.,!?。！？，、…~～").casefold() in GREETINGS

    def direct_message(self, agent_name, content, *, chat_id=None):
        if agent_name not in self.agents:
            raise ValueError("Unknown agent")
        if not isinstance(content, str) or not content.strip():
            raise ValueError("Message cannot be empty")
        scope = {"chat_id": chat_id} if chat_id is not None else {}
        message = Message(uuid4().hex, "dm", self.human, content.strip(), agent_name)
        self.messages.save_message(message, **scope)
        greeting = self._is_greeting(message.content)
        history = () if greeting else self.messages.list_direct_messages(self.human, agent_name, **scope)
        reply = self._reply(self.agents[agent_name], message, history, greeting=greeting)
        self.messages.save_message(Message(uuid4().hex, "dm", agent_name,
                                           reply, self.human), **scope)
        return reply

    def group_message(self, content, *, task_id=None, group_id=None):
        if not isinstance(content, str) or not content.strip():
            raise ValueError("Message cannot be empty")
        scope = {"group_id": group_id} if group_id is not None else {}
        message = Message(uuid4().hex, "group", self.human, content.strip(), task_id=task_id)
        self.messages.save_message(message, **scope)
        greeting = self._is_greeting(message.content)
        replies = {}
        for agent in self.agents.values():
            history = () if greeting else self.messages.list_group_messages(task_id=task_id, **scope)
            reply = self._reply(agent, message, history, greeting=greeting)
            self.messages.save_message(Message(uuid4().hex, "group", agent.name, reply, task_id=task_id), **scope)
            replies[agent.name] = reply
        return replies
