"""Human DM and group chat, adapted from the original Engine chat methods."""

import json
import re
from uuid import uuid4

from evo.domain.models import Agent, Message


class ChatService:
    def __init__(self, provider, messages, agents, human="human"):
        self.provider = provider
        self.messages = messages
        agents = tuple(agents)
        self.agents = {agent.name: agent for agent in agents}
        if not self.agents or len(self.agents) != len(agents):
            raise ValueError("Agent names must be unique and nonempty")
        self.human = human

    def _reply(self, agent, history, group=False, addressed_by=None):
        mode = "in this human and multi-agent group chat" if group else "to the human privately"
        routing = (" If you need a response from another Agent, write @agent_name in your reply."
                   if group else "")
        if addressed_by:
            routing += " You were addressed by @%s; answer that Agent's latest message." % addressed_by
        result = self.provider.complete(
            [{"role": "system", "content":
              "You are %s, specializing in %s. Reply %s.%s Return JSON with a nonempty reply string."
              % (agent.name, agent.specialty, mode, routing)},
             {"role": "user", "content": json.dumps(history, ensure_ascii=False)}],
            json_object=True)
        if not isinstance(result, dict) or not isinstance(result.get("reply"), str):
            raise ValueError("Agent returned an invalid chat reply")
        reply = result["reply"].strip()
        if not reply:
            raise ValueError("Agent returned an empty chat reply")
        return reply[:2000 if group else 4000]

    def direct_message(self, agent_name, content, task_id=None):
        if agent_name not in self.agents:
            raise ValueError("Unknown agent")
        if not isinstance(content, str) or not content.strip():
            raise ValueError("Message cannot be empty")
        self.messages.save_message(Message(uuid4().hex, "dm", self.human,
                                           content.strip(), agent_name, task_id))
        history = [message for message in self.messages.list_direct_messages(self.human, agent_name)
                   if message.get("task_id") == task_id]
        reply = self._reply(self.agents[agent_name], history)
        self.messages.save_message(Message(uuid4().hex, "dm", agent_name,
                                           reply, self.human, task_id))
        return reply

    def group_message(self, content, task_id=None):
        if not isinstance(content, str) or not content.strip():
            raise ValueError("Message cannot be empty")
        self.messages.save_message(Message(uuid4().hex, "group", self.human,
                                           content.strip(), "chat" if task_id else None, task_id))
        def history():
            return [record for record in self.messages.list_group_messages()
                    if (record.get("task_id") == task_id and
                        (not task_id or record.get("recipient") == "chat"))]
        def save(agent, reply):
            self.messages.save_message(Message(uuid4().hex, "group", agent,
                                               reply, "chat" if task_id else None, task_id))
        replies = {}
        mentions = []
        for agent in self.agents.values():
            reply = self._reply(agent, history(), group=True)
            save(agent.name, reply)
            replies[agent.name] = reply
            mentions.extend((agent.name, target) for target in self._mentions(reply, agent.name))
        # Bound follow-ups so two Agents cannot cause an unending @ loop.
        answered = set()
        directed = []
        while mentions and len(directed) < 8:
            source, target = mentions.pop(0)
            if (source, target) in answered:
                continue
            answered.add((source, target))
            reply = self._reply(self.agents[target], history(), group=True, addressed_by=source)
            save(target, reply)
            directed.append({"from": target, "to": source, "reply": reply})
            mentions.extend((target, next_target) for next_target in self._mentions(reply, target))
        self.directed_replies = directed
        return replies

    def _mentions(self, content, sender):
        return [name for name in dict.fromkeys(re.findall(r"(?<![\w@])@([A-Za-z0-9_-]+)\b", content))
                if name in self.agents and name != sender]
