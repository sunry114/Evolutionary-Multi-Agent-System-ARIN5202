"""Human DM and group chat, adapted from the original Engine chat methods."""

import json
import re
from uuid import uuid4

from evo.domain.models import Agent, Message
from evo.domain.policies import team_coordinator


def human_question(content, human="human"):
    """Extract only sections addressed to the human from legacy mixed replies."""
    mentions = list(re.finditer(r"(?<![\w@])@([A-Za-z0-9_-]+)\b", content))
    if not mentions:
        return content.strip()
    questions = []
    for index, mention in enumerate(mentions):
        if mention.group(1) != human:
            continue
        end = mentions[index + 1].start() if index + 1 < len(mentions) else len(content)
        question = content[mention.end():end].strip(" \t\r\n:：-*#")
        if question:
            questions.append(question)
    return "\n\n".join(questions)


class ChatService:
    def __init__(self, provider, messages, agents, human="human", *, message_writer=None,
                 question_writer=None, waiting_agents=()):
        self.provider = provider
        self.messages = messages
        agents = tuple(agents)
        self.agents = {agent.name: agent for agent in agents}
        if not self.agents or len(self.agents) != len(agents):
            raise ValueError("Agent names must be unique and nonempty")
        self.human = human
        self.waiting_for_human = False
        self._save_message = message_writer or messages.save_message
        self._question_writer = question_writer
        self.waiting_agents = set(waiting_agents)
        self.directed_replies = []
        self.human_question = ""
        self.coordinator = team_coordinator(agents)
        self.escalations = []
        self.question_sources = []
        self.coordinator_waiting = self.coordinator in self.waiting_agents

    def _reply(self, agent, history, group=False, addressed_by=None):
        mode = "in this human and multi-agent group chat" if group else "to the human privately"
        routing = (" If you need a response from another Agent, write @agent_name in your reply."
                   if group else "")
        if addressed_by:
            routing += " You were addressed by @%s; answer that Agent's latest message." % addressed_by
        routing += (" You are the team's sole human contact. Resolve teammates' questions "
                    "from available context where possible; consolidate only unresolved "
                    "questions for the human. Relay task-relevant decisions to the team."
                    if agent.name == self.coordinator else
                    " The team's sole human contact is @%s. Send unresolved questions to "
                    "that coordinator using @%s and waiting_for_human:true with human_question. "
                    "Never address @human directly. All agents may independently request "
                    "critical-action approval through platform tools; approval is separate "
                    "from ordinary questions." % (self.coordinator, self.coordinator))
        result = self.provider.complete(
            [{"role": "system", "content":
              "You are %s, specializing in %s. Reply %s.%s Return JSON with a nonempty reply string. "
              "When external input is needed, set waiting_for_human to true and provide "
              "human_question. Only the coordinator may mention @human; other agents "
              "must address the coordinator. Do not invent the human's answer or keep discussing "
              "while waiting; the human will reply privately in a later message. "
              "Other agents may continue work that does not depend on that answer. "
              "Platform capabilities: create Python/JS/TS/HTML/CSS/SQL text files; "
              "execute only Python standard-library scripts in isolated Docker. "
              "No Node/npm, shell or dependency-installation tool is available. Prefer "
              "a stdlib Python backend for a runnable web app unless the human requires "
              "another stack. Do not commit the team to unsupported runtime commands. "
              "When asking the human, also return human_question: a concise standalone "
              "question containing only what the human must answer. Put agent-to-agent "
              "discussion exclusively in reply; never include it in human_question. "
              "Only mention another agent to request concrete work, not for acknowledgements."
              % (agent.name, agent.specialty, mode, routing)},
             {"role": "user", "content": json.dumps(history, ensure_ascii=False)}],
            json_object=True)
        if not isinstance(result, dict) or not isinstance(result.get("reply"), str):
            raise ValueError("Agent returned an invalid chat reply")
        reply = result["reply"].strip()
        if not reply:
            raise ValueError("Agent returned an empty chat reply")
        if len(reply.encode("utf-8")) > 32768:
            raise ValueError("Agent chat reply exceeds the message size limit")
        self.waiting_for_human = (result.get("waiting_for_human") is True or
                                 self.human in re.findall(r"(?<![\w@])@([A-Za-z0-9_-]+)\b", reply))
        question = result.get("human_question")
        self.human_question = (human_question(question, self.human)
                               if isinstance(question, str) and question.strip()
                               else human_question(reply, self.human))
        return reply

    def _save_group_reply(self, agent, reply, task_id):
        needs_human = self.waiting_for_human
        if needs_human and agent != self.coordinator:
            reply = re.sub(r"(?<![\w@])@human\b", "@" + self.coordinator, reply)
            if "@" + self.coordinator not in reply:
                reply += "\n@" + self.coordinator + " 请协助确认：" + self.human_question
        self._save_message(Message(uuid4().hex, "group", agent, reply,
                                   "chat" if task_id else None, task_id))
        if needs_human:
            self.waiting_agents.add(agent)
            question = self.human_question or "请补充你对当前任务的需求或限制。"
            if agent == self.coordinator:
                self.coordinator_waiting = True
                self.question_sources = [agent]
                if self._question_writer is not None:
                    self._question_writer(agent, question, task_id)
            else:
                self.escalations.append({"agent": agent, "question": question})
        else:
            self.waiting_agents.discard(agent)

    def _flush_questions(self, task_id):
        if not self.escalations:
            return
        sources = list(dict.fromkeys(item["agent"] for item in self.escalations))
        question = "\n\n".join(item["question"] for item in self.escalations)
        if any(name != self.coordinator for name in sources):
            history = [m for m in self.messages.list_group_messages()
                       if m.get("task_id") == task_id and (not task_id or m.get("recipient") == "chat")]
            history.append({"sender": "system", "content":
                            "Consolidate these teammate questions. Resolve them yourself when "
                            "possible; otherwise ask the human one focused consolidated question. "
                            "Do not request more agent turns in this consolidation reply. " +
                            json.dumps(self.escalations, ensure_ascii=False)})
            reply = self._reply(self.agents[self.coordinator], history, group=True)
            self._save_message(Message(uuid4().hex, "group", self.coordinator, reply,
                                       "chat" if task_id else None, task_id))
            question = self.human_question
            if not self.waiting_for_human:
                self.waiting_agents.difference_update(sources)
                if not self.coordinator_waiting:
                    self.waiting_agents.discard(self.coordinator)
                self.escalations.clear()
                return
        self.question_sources = sources
        self.waiting_agents.add(self.coordinator)
        self.coordinator_waiting = True
        if self._question_writer is not None:
            self._question_writer(self.coordinator, question, task_id)
        self.escalations.clear()

    def resume_question(self, agent_name, task_id, *, sources=()):
        """The coordinator reads the private answer and wakes dependent teammates."""
        history = [m for m in self.messages.list_direct_messages(self.human, agent_name)
                   if m.get("task_id") == task_id]
        history.append({"sender": "system", "content":
                        "The human answered your private question. Reply to the team with "
                        "the task-relevant decision and next step. Keep private details private."})
        reply = self._reply(self.agents[agent_name], history, group=True)
        self._save_group_reply(agent_name, reply, task_id)
        targets = [] if self.waiting_for_human else [name for name in sources
                  if name in self.agents and name != agent_name]
        targets.extend(self._mentions(reply, agent_name))
        mentions = [(agent_name, target) for target in dict.fromkeys(targets)]
        self._followups(mentions, task_id)
        self._flush_questions(task_id)
        self.waiting_for_human = bool(self.waiting_agents)
        return {agent_name: reply}

    def direct_message(self, agent_name, content, task_id=None):
        if agent_name not in self.agents:
            raise ValueError("Unknown agent")
        if not isinstance(content, str) or not content.strip():
            raise ValueError("Message cannot be empty")
        self._save_message(Message(uuid4().hex, "dm", self.human,
                                           content.strip(), agent_name, task_id))
        history = [message for message in self.messages.list_direct_messages(self.human, agent_name)
                   if message.get("task_id") == task_id]
        reply = self._reply(self.agents[agent_name], history)
        if self.waiting_for_human and agent_name != self.coordinator:
            self._save_group_reply(agent_name, reply, task_id)
            self._flush_questions(task_id)
            self.waiting_for_human = bool(self.waiting_agents)
            return "问题已交给总负责人 " + self.coordinator + " 统一处理。"
        self._save_message(Message(uuid4().hex, "dm", agent_name,
                                           reply, self.human, task_id))
        return reply

    def group_message(self, content, task_id=None):
        if not isinstance(content, str) or not content.strip():
            raise ValueError("Message cannot be empty")
        self._save_message(Message(uuid4().hex, "group", self.human,
                                           content.strip(), "chat" if task_id else None, task_id))
        def history():
            return [record for record in self.messages.list_group_messages()
                    if (record.get("task_id") == task_id and
                        (not task_id or record.get("recipient") == "chat"))]
        replies = {}
        self.directed_replies = []
        mentions = []
        for agent in self.agents.values():
            if agent.name in self.waiting_agents:
                continue
            reply = self._reply(agent, history(), group=True)
            self._save_group_reply(agent.name, reply, task_id)
            replies[agent.name] = reply
            mentions.extend((agent.name, target) for target in self._mentions(reply, agent.name))
        self._followups(mentions, task_id)
        self._flush_questions(task_id)
        self.waiting_for_human = bool(self.waiting_agents)
        return replies

    def _followups(self, mentions, task_id):
        # Bound follow-ups so two Agents cannot cause an unending @ loop.
        answered = set()
        directed = []
        while mentions and len(directed) < 8:
            source, target = mentions.pop(0)
            if (source, target) in answered or target in self.waiting_agents or source in self.waiting_agents:
                continue
            answered.add((source, target))
            history = [record for record in self.messages.list_group_messages()
                       if record.get("task_id") == task_id and
                       (not task_id or record.get("recipient") == "chat")]
            reply = self._reply(self.agents[target], history, group=True, addressed_by=source)
            self._save_group_reply(target, reply, task_id)
            directed.append({"from": target, "to": source, "reply": reply})
            mentions.extend((target, next_target) for next_target in self._mentions(reply, target))
        self.directed_replies = directed

    def _mentions(self, content, sender):
        return [name for name in dict.fromkeys(re.findall(r"(?<![\w@])@([A-Za-z0-9_-]+)\b", content))
                if name in self.agents and name != sender]
