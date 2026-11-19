"""Run explicitly with Playwright installed; no live provider calls are made."""
import tempfile
import threading
from pathlib import Path

from playwright.sync_api import sync_playwright, expect

from evo.domain.models import Agent, Task
from evo.domain.statuses import TaskStatus
from evo.interfaces.sqlite_application import SQLiteApplication
from evo.interfaces.web.app import make_server


def main():
    dev_entered, dev_release = threading.Event(), threading.Event()
    qa_entered, qa_release = threading.Event(), threading.Event()

    class Provider:
        def complete(self, messages, **kwargs):
            agent = messages[0]["content"].split("You are ", 1)[1].split(",", 1)[0]
            if agent == "lead":
                if "The human answered your private question" in messages[1]["content"]:
                    return {"reply": "Lead decision: use Room A; proceed with implementation."}
                return {"reply": "@dev Coordinate API design.\n@human Which rooms should we support?\n@qa Prepare validation.",
                        "human_question": "Which rooms should we support?", "waiting_for_human": True}
            if agent == "dev":
                dev_entered.set()
                if not dev_release.wait(30):
                    raise TimeoutError("Browser did not release developer")
            if agent == "qa":
                qa_entered.set()
                if not qa_release.wait(30):
                    raise TimeoutError("Browser did not release QA")
            return {"reply": agent + " can prepare independent work."}

    with tempfile.TemporaryDirectory() as state:
        app = SQLiteApplication(Path(state), provider=Provider(), agents=tuple(
            Agent(name, name + " specialty") for name in ("lead", "dev", "qa", "release")))
        server = make_server(app, 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        errors = []
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                page = browser.new_page()
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto("http://" + server.authority)
                expect(page.locator("#notice")).to_contain_text("SQLite")
                page.locator('#run-form textarea[name="prompt"]').fill("Build a room booking system")
                page.locator("#run-form button").click()
                assert dev_entered.wait(5)
                expect(page.locator("#group")).to_be_visible()
                expect(page.locator("#group-form")).to_have_count(0)
                expect(page.locator("#group-list")).to_contain_text("Which rooms")
                expect(page.locator("#group-list")).not_to_contain_text("dev can prepare")
                root_id = next(t["id"] for t in app.store.list_tasks() if not t["parent_id"])
                root_card = page.locator('#task-list article[data-task-id="' + root_id + '"]')
                expect(root_card).to_have_attribute("data-selected", "true")
                expect(root_card.locator(".task-phase")).to_have_text("讨论中 · 等待人类回复")
                expect(page.locator("#group-task-summary .task-phase")).to_have_text("讨论中 · 等待人类回复")

                page.locator('[data-view="dm"]').click()
                answer = page.get_by_role("textbox", name="回复 lead", exact=True)
                expect(answer).to_be_visible()
                expect(page.locator("#dm-rooms")).to_contain_text("Which rooms should we support?")
                expect(page.locator("#dm-rooms")).not_to_contain_text("Coordinate API design")
                expect(page.locator("#dm-rooms")).not_to_contain_text("Prepare validation")
                answer.fill("Room A; private budget detail")
                page.locator("#refresh").click()
                expect(answer).to_have_value("Room A; private budget detail")
                # External task changes must sync even while a human is typing.
                other_id = app.dispatch("create", {"prompt": "An external task"})["task_id"]
                other_card = page.locator('#task-list article[data-task-id="' + other_id + '"]')
                expect(other_card).to_have_count(1)
                expect(answer).to_have_value("Room A; private budget detail")
                expect(page.locator("#group-task")).to_have_value(root_id)
                page.locator('[data-view="tasks"]').click()
                details = root_card.locator("details")
                details.locator("summary").click()
                details.get_by_role("button", name="加载 trace").click()
                expect(details.locator("pre")).to_have_count(1)
                app.store.terminate_task(other_id)
                expect(page.locator('#tasks-done article[data-task-id="' + other_id + '"]')).to_have_count(1)
                expect(details).to_have_attribute("open", "")
                expect(details.locator("pre")).to_have_count(1)
                page.locator('[data-view="dm"]').click()
                expect(answer).to_have_value("Room A; private budget detail")

                dev_release.set()
                assert qa_entered.wait(5)
                page.locator('[data-view="group"]').click()
                expect(page.locator("#group-list")).to_contain_text("dev can prepare")
                expect(page.locator("#group-list")).not_to_contain_text("qa can prepare")
                page.locator('[data-view="dm"]').click()
                expect(answer).to_have_value("Room A; private budget detail")
                page.get_by_role("button", name="发送回复", exact=True).click()
                expect(page.locator("#status")).to_contain_text("私聊回答已保存")
                qa_release.set()
                page.locator('[data-view="group"]').click()
                for name in ("dev", "qa", "release"):
                    expect(page.locator("#group-list")).to_contain_text(name + " can prepare")
                expect(page.locator("#group-list")).to_contain_text("Lead decision: use Room A")
                expect(page.locator("#group-list")).not_to_contain_text("private budget detail")
                expect(root_card.locator(".task-phase")).to_have_text("讨论完成，待开始执行")
                expect(page.locator("#group-task-summary .task-phase")).to_have_text("讨论完成，待开始执行")
                # Selection follows both navigation directions, including ended tasks.
                page.locator("#group-task").select_option(other_id)
                expect(page.locator("#dm-task")).to_have_value(other_id)
                expect(other_card).to_have_attribute("data-selected", "true")
                expect(page.locator("#group-task-summary .task-phase")).to_have_text("已阻塞 / 已终止")
                page.locator("#group-task-summary").get_by_role("button", name="回到任务看板").click()
                expect(page.locator("#tasks")).to_be_visible()
                root_card.get_by_role("button", name="进入群聊").click()
                expect(page.locator("#group-task")).to_have_value(root_id)
                expect(page.locator("#dm-task")).to_have_value(root_id)
                expect(root_card).to_have_attribute("data-selected", "true")
                # Every worker retains its own critical-action approval card.
                requests = {}
                for name in ("dev", "qa"):
                    child = root_id + "-" + name
                    app.store.save_task(Task(child, "Run checks", owner=name, parent_id=root_id,
                                             status=TaskStatus.RUNNING))
                    requests[name] = app.approvals.request_approval(
                        child, name, "run_python", {"arguments": {"path": "check.py"}, "sha256": "f" * 64})
                page.locator('[data-view="dm"]').click()
                dev_room = page.locator("#dm-rooms article").filter(has=page.locator("h3", has_text="dev"))
                qa_room = page.locator("#dm-rooms article").filter(has=page.locator("h3", has_text="qa"))
                expect(dev_room.get_by_role("button", name="批准", exact=True)).to_be_visible()
                expect(qa_room.get_by_role("button", name="拒绝", exact=True)).to_be_visible()
                expect(root_card.locator(".task-phase")).to_have_text("等待审批")
                expect(page.locator("#group-task-summary .task-phase")).to_have_text("等待审批")
                expect(dev_room.locator("textarea")).to_have_count(0)
                expect(qa_room.locator("textarea")).to_have_count(0)
                dev_room.get_by_role("button", name="批准", exact=True).click()
                qa_room.get_by_role("button", name="拒绝", exact=True).click()
                expect(dev_room).to_have_count(0)
                expect(qa_room).to_have_count(0)
                assert app.store.get_approval_request(requests["dev"])["status"] == "approved"
                assert app.store.get_approval_request(requests["qa"])["status"] == "denied"
                page.locator('[data-view="group"]').click()
                page.locator("#group-task-summary").get_by_role("button", name="终止任务", exact=True).click()
                page.locator("#confirm-terminate").click()
                expect(page.locator("#group-task-summary .task-phase")).to_have_text("已阻塞 / 已终止")
                expect(page.locator('#tasks-done article[data-task-id="' + root_id + '"]')).to_have_count(1)
                expect(page.locator("#group-list .bubble")).to_have_count(0)
                expect(page.locator("#group-task-summary").get_by_role("button", name="开始执行")).to_have_count(0)
                page.locator('[data-view="tasks"]').click()
                page.locator("#clear-finished").click()
                expect(root_card).to_have_count(0)
                expect(page.locator("#group-task")).to_have_value("")
                expect(page.locator("#dm-task")).to_have_value("")
                expect(page.locator("#group-task-summary")).to_be_hidden()
                fresh_id = app.dispatch("create", {"prompt": "New task without active selection"})["task_id"]
                expect(page.locator('#task-list article[data-task-id="' + fresh_id + '"]')).to_have_count(1)
                assert not errors, errors
                browser.close()
        finally:
            dev_release.set()
            qa_release.set()
            server.shutdown()
            server.server_close()
            thread.join(3)
    print("PASS: board/group synchronization, shared selection and phases, external updates while typing, approvals, clearing, no page errors")


if __name__ == "__main__":
    main()
