"""Durable human decisions. No model-supplied approval flags are trusted."""

import math
import uuid


class ApprovalService:
    def __init__(self, repository, *, max_age_seconds=3600, clarification_agent="planner"):
        if (isinstance(max_age_seconds, bool) or not isinstance(max_age_seconds, (float, int))
                or not math.isfinite(max_age_seconds) or not 0 < max_age_seconds <= 86400):
            raise ValueError("Invalid approval lifetime")
        self.repository = repository
        self.max_age_seconds = max_age_seconds
        self.clarification_agent = clarification_agent

    def request_approval(self, task_id, agent, action, payload):
        return self.repository.prepare_critical_action(task_id, agent, action, payload)

    def request_clarification(self, task_id, question):
        if not isinstance(question, str) or not question.strip() or len(question) > 500:
            raise ValueError("Invalid clarification question")
        request_id = str(uuid.uuid4())
        self.repository.save_approval_request(request_id=request_id, task_id=task_id,
                                             agent=self.clarification_agent, kind="clarification",
                                             payload={"question": question})
        return request_id

    def get_answer(self, request_id):
        request = self.repository.expire_request_if_due(request_id, max_age_seconds=self.max_age_seconds)
        if request["status"] == "pending":
            return None
        return {"status": request["status"], "answer": request["answer"]}

    def check_scope(self, request_id, task_id, agent, action, payload):
        request = self.repository.get_approval_request(request_id)
        if (request["kind"] != "approval" or request["task_id"] != task_id
                or request["agent"] != agent or request["action"] != action or request["payload"] != payload):
            raise ValueError("Approval does not match this action")

    def answer(self, request_id, response):
        request = self.repository.answer_request(request_id, response, max_age_seconds=self.max_age_seconds)
        return {"request_id": request_id, "status": request["status"]}

    def claim(self, request_id, task_id, agent, action, payload):
        return self.repository.claim_approved_action(request_id, task_id, agent, action, payload,
                                                     max_age_seconds=self.max_age_seconds)
