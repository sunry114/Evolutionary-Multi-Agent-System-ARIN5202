"""Human DM and group chat, adapted from the original Engine chat methods."""

import json
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

    def _reply(self, agent, history, group=False):
        mode = "in this human and multi-agent group chat" if group else "to the human privately"
        result = self.provider.complete(
            [{"role": "system", "content":
              "You are %s, specializing in %s. Reply %s. Return JSON with a nonempty reply string."
              % (agent.name, agent.specialty, mode)},
             {"role": "user", "content": json.dumps(history, ensure_ascii=False)}],
            json_object=True)
        if not isinstance(result, dict) or not isinstance(result.get("reply"), str):
            raise ValueError("Agent returned an invalid chat reply")
        reply = result["reply"].strip()
        if not reply:
            raise ValueError("Agent returned an empty chat reply")
        return reply[:2000 if group else 4000]

    def direct_message(self, agent_name, content):
        if agent_name not in self.agents:
            raise ValueError("Unknown agent")
        if not isinstance(content, str) or not content.strip():
            raise ValueError("Message cannot be empty")
        self.messages.save_message(Message(uuid4().hex, "dm", self.human,
                                           content.strip(), agent_name))
        history = self.messages.list_direct_messages(self.human, agent_name)
        reply = self._reply(self.agents[agent_name], history)
        self.messages.save_message(Message(uuid4().hex, "dm", agent_name,
                                           reply, self.human))
        return reply

    def group_message(self, content):
        if not isinstance(content, str) or not content.strip():
            raise ValueError("Message cannot be empty")
        self.messages.save_message(Message(uuid4().hex, "group", self.human,
                                           content.strip()))
        replies = {}
        for agent in self.agents.values():
            # Task bids and action logs may be group-kind records in old stores.
            # Only explicit chat messages belong in visible group history.
            history = [record for record in self.messages.list_group_messages()
                       if not record.get("task_id")]
            reply = self._reply(agent, history, group=True)
            self.messages.save_message(Message(uuid4().hex, "group", agent.name, reply))
            replies[agent.name] = reply
        return replies
