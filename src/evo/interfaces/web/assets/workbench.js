"use strict";

const el = id => document.getElementById(id);
const token = document.querySelector('meta[name="csrf-token"]').content;
const laneNames = {
  needs_action: "待人工处理", review: "待验收", changes: "待修订", draft: "待启动",
  in_progress: "执行中", blocked: "已阻塞", done: "已验收",
};
const executionNames = {
  created: "待启动", planning: "规划中", ready: "待分派", assigned: "已分派", running: "执行中",
  waiting_human: "等待人工", success: "执行成功", failed: "执行失败", blocked: "已阻塞",
};
const requestNames = {pending: "待处理", approved: "已批准", denied: "已拒绝", answered: "已回复", expired: "已过期"};
const reviewNames = {pending: "待验收", accepted: "已接受", changes_requested: "已退回", revising: "修订中"};
const routingNames = {squad: "队长协商", direct: "指定成员"};
const tabNames = {discussion: "讨论", negotiation: "分派协商", logs: "运行记录", artifacts: "产物", decisions: "审批与审查", revisions: "修订"};
const eventNames = {
  issue_assigned: "更新负责人", plan_created: "生成执行计划", task_created: "创建任务", task_transition: "更新状态",
  routing_call: "路由模型调用", memory_recorded: "记录个人经验",
  harness_started: "启动执行器", harness_paused: "执行暂停", harness_finished: "执行结束", action_started: "工具启动",
  action_completed: "工具结束", objective_verified: "通过产物验收", artifact_published: "共享产物", orchestration_error: "执行异常",
  delegation_requested: "发起委派", delegation_completed: "返回委派结果", resume_claimed: "恢复执行",
  request_created: "发起人工请求", request_answered: "人工请求已处理", request_expired: "请求过期",
  review_decided: "人审决定", revision_linked: "创建修订", clarification_requested: "请求澄清",
};
const state = {workspace: null, detail: null, issues: [], messages: [], tab: "discussion", generation: 0};

function node(tag, value, parent, className) {
  const item = document.createElement(tag);
  if (className) item.className = className;
  if (value !== undefined && value !== null) item.textContent = String(value);
  if (parent) parent.append(item);
  return item;
}

function button(parent, label, click, style = "") {
  const item = node("button", label, parent, ("button " + style).trim());
  item.type = "button";
  item.onclick = () => click(item);
  return item;
}

function formButton(parent, label, value, style = "") {
  const control = node("button", label, parent, ("button " + style).trim());
  control.type = "submit";
  control.value = value;
  return control;
}

function link(parent, label, target, className) {
  const item = node("a", label, parent, className);
  item.href = target;
  return item;
}

function field(parent, label, tag, name) {
  const wrap = node("label", label, parent);
  const control = node(tag, "", wrap);
  control.name = name;
  return control;
}

function option(select, label, value) {
  node("option", label, select).value = value;
}

function notice(message = "", error = false) {
  const item = el("status");
  item.textContent = message;
  item.classList.toggle("error", error);
  item.hidden = !message;
}

async function api(operation, payload) {
  const response = await fetch("/api/" + operation, {
    method: "POST", headers: {"Content-Type": "application/json", "X-CSRF-Token": token},
    body: JSON.stringify(payload),
  });
  const data = await response.json();
  if (!response.ok) throw Error(data.error || "操作失败");
  return data;
}

function route() {
  const [view, key, name] = location.hash.slice(1).split("/");
  try {
    if (view === "issue" && key) return {view, key: decodeURIComponent(key)};
    if (view === "owner" && ["agent", "squad"].includes(key) && name) {
      return {view, type: key, key: decodeURIComponent(name)};
    }
  } catch {
    return {view: "issues"};
  }
  return {view: ["attention", "workspace"].includes(view) ? view : "issues"};
}

function issueUrl(id) { return "#issue/" + encodeURIComponent(id); }
function ownerUrl(name, type) { return name ? "#owner/" + type + "/" + encodeURIComponent(name) : "#issues"; }
function issueKey(id) { return "EVO-" + id.slice(-7).toUpperCase(); }
function title(task) { return task.prompt.split("\n")[0]; }
function when(iso) { return iso ? new Date(iso).toLocaleString("zh-CN", {month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit"}) : ""; }
function empty(parent, text) { node("p", text, parent, "empty"); }

function lane(parent, status) {
  const item = node("span", "", parent, "lane");
  item.dataset.lane = status;
  node("span", "", item, "dot").setAttribute("aria-hidden", "true");
  node("span", laneNames[status], item);
}

function renderNavigation(active) {
  const {agents, squad, mode} = state.workspace;
  el("notice").textContent = mode;
  const owners = el("owner-nav");
  const select = el("owner-filter");
  owners.replaceChildren();
  select.replaceChildren();
  option(select, "全部", "");
  const add = (name, type) => {
    const kind = type === "squad" ? "Squad" : "Agent";
    const target = link(owners, "", ownerUrl(name, type));
    node("span", type === "squad" ? "S" : name[0].toUpperCase(), target, "nav-icon");
    node("span", name, target);
    target.setAttribute("aria-label", kind + " " + name);
    if (active.view === "owner" && active.key === name && active.type === type) target.setAttribute("aria-current", "page");
    option(select, kind + " · " + name, type + ":" + name);
  };
  if (squad) add(squad.name, "squad");
  agents.forEach(name => add(name, "agent"));
  select.value = active.view === "owner" ? active.type + ":" + active.key : "";
  for (const item of document.querySelectorAll("[data-route]")) {
    if (item.dataset.route === (active.view === "issue" || active.view === "owner" ? "issues" : active.view)) {
      item.setAttribute("aria-current", "page");
    } else item.removeAttribute("aria-current");
  }
}

function renderList() {
  const current = route();
  if (!["issues", "attention", "owner"].includes(current.view)) return;
  el("list-title").textContent = current.view === "owner" ? current.key + " 的工单" :
    current.view === "attention" ? "待我处理" : "工单";
  const query = el("search").value.trim().toLowerCase();
  const filter = el("status-filter").value;
  const needsAttention = ["needs_action", "review", "changes"];
  const issues = state.issues.filter(issue =>
    (!filter || issue.lane === filter) &&
    (current.view !== "attention" || needsAttention.includes(issue.lane)) &&
    (!query || [issue.prompt, issue.id, issueKey(issue.id)].some(value => value.toLowerCase().includes(query))));
  el("issue-count").textContent = issues.length + " 张";
  const list = el("issue-list");
  list.replaceChildren();
  if (!issues.length) {
    empty(list, query || filter || current.view !== "issues" ? "没有符合条件的工单" : "暂无工单");
    return;
  }
  for (const [key, label] of Object.entries(laneNames)) {
    const matches = issues.filter(issue => issue.lane === key);
    if (!matches.length) continue;
    const group = node("section", "", list, "issue-group");
    const heading = node("div", "", group, "group-heading");
    node("span", "", heading, "dot").dataset.lane = key;
    node("h2", label, heading);
    node("small", matches.length, heading);
    for (const item of matches) {
      const row = link(group, "", issueUrl(item.id), "issue-row");
      node("span", issueKey(item.id), row, "issue-key");
      node("span", title(item), row, "title-cell");
      node("span", item.assignee_id ? (item.assignee_type === "squad" ? "Squad · " : "") + item.assignee_id : "未指派", row, "owner-cell");
      node("time", when(item.updated_at), row, "row-date");
    }
  }
}

async function mutate(control, operation, payload, message, tab) {
  const before = location.hash;
  control.disabled = true;
  notice();
  try {
    await api(operation, payload);
    if (before === location.hash) {
      if (tab) state.tab = tab;
      await load();
      if (before === location.hash) notice(message);
    }
  } catch (error) {
    if (before === location.hash) notice(error.message, true);
  } finally {
    control.disabled = false;
  }
}

function openTab(key) {
  const selectedByTab = el("issue-tabs").contains(document.activeElement);
  state.tab = key;
  renderActivity();
  if (selectedByTab) el("issue-tabs").querySelector('[aria-pressed="true"]').focus();
}

function assignmentForm(parent, task) {
  const form = node("form", "", parent, "stack");
  const assignee = field(form, "指派给", "select", "assignee");
  option(assignee, "选择负责人", "");
  assignee.required = true;
  const {squad, agents} = state.workspace;
  if (squad) option(assignee, "Squad · " + squad.name, "squad:" + squad.name);
  for (const name of agents) option(assignee, "Agent · " + name, "agent:" + name);
  if (task.assignee_type) {
    assignee.value = task.assignee_type + ":" + task.assignee_id;
  }
  const submit = formButton(form, "确认指派", "", "primary");
  form.onsubmit = event => {
    event.preventDefault();
    const [type, id] = assignee.value.split(":");
    const payload = {task_id: task.id, [type]: id};
    void mutate(submit, "assign", payload, "已更新负责人");
  };
  return form;
}

function renderInspector(detail) {
  const parent = el("inspector");
  parent.replaceChildren();
  const current = detail.current;
  const next = node("section", "", parent, "card next");
  node("h2", "下一步", next);
  const action = detail.next_action;
  if (action === "assign") {
    assignmentForm(next, current);
  } else if (action === "start" || action === "resume") {
    const label = action === "resume" ? "继续执行" : (detail.runs.length > 1 ? "启动修订" : "启动工单");
    button(next, label, control => mutate(control, action, {task_id: current.id},
      action === "resume" ? "已处理恢复请求" : "已处理启动请求"), "primary");
    if (action === "start") {
      const edit = node("details", "", next);
      node("summary", "更改负责人", edit);
      assignmentForm(edit, current);
    }
  } else if (action) {
    const labels = {answer: "处理审批或澄清", review: "验收交付", revision: "创建修订"};
    button(next, labels[action], () => openTab(action === "revision" ? "revisions" : "decisions"), "primary");
  } else if (detail.lane === "blocked") {
    button(next, "查看运行记录", () => openTab("logs"));
  } else {
    node("p", detail.lane === "done" ? "交付已验收" : "等待执行更新", next, "muted");
  }
  if (current.result) node("p", current.result, next, "result-text");

  const props = node("section", "", parent, "card");
  node("h2", "工单属性", props);
  const list = node("dl", "", props);
  const property = (label, value) => {
    node("dt", label, list);
    return node("dd", value, list);
  };
  lane(property("状态", ""), detail.lane);
  const owner = property("负责人", current.assignee_id ? "" : "未指派");
  if (current.assignee_id) link(owner, (current.assignee_type === "squad" ? "Squad · " : "") + current.assignee_id,
    ownerUrl(current.assignee_id, current.assignee_type));
  if (current.routing_mode) property("分派方式", routingNames[current.routing_mode]);
  const owners = [...new Set(detail.tree.filter(task => task.parent_id && task.owner && task.owner !== "collective").map(task => task.owner))];
  if (owners.length) {
    const members = property("执行成员", "");
    owners.forEach((name, index) => {
      if (index) node("span", "、", members);
      link(members, name, ownerUrl(name, "agent"));
    });
  }
  property("工单 ID", detail.issue.id);
  if (detail.runs.length > 1) property("当前运行", current.id);
}

function feedItem(parent, heading, metadata, body) {
  const item = node("article", "", parent, "feed-item");
  const head = node("div", "", item, "feed-head");
  node("strong", heading, head);
  if (metadata) node("small", metadata, head);
  if (body) node("p", body, item, "feed-content");
  return item;
}

function discussion(panel, detail) {
  const feed = node("div", "", panel, "feed");
  if (!detail.messages.length) empty(feed, "还没有讨论");
  for (const item of detail.messages) {
    const title = {group: "群聊", comment: "评论", agent: "委派消息"}[item.kind];
    feedItem(feed, item.sender === "human" ? "你" : item.sender,
      [title, when(item.created_at)].filter(Boolean).join(" · "), item.content);
  }
  const form = node("form", "", panel, "stack");
  const message = field(form, "工单讨论", "textarea", "message");
  message.required = true;
  message.maxLength = 8000;
  message.placeholder = "留言或与成员讨论这张工单";
  const actions = node("div", "", form, "form-actions");
  const post = formButton(actions, "发表评论", "comment", "primary");
  formButton(actions, "发送工单群聊", "group");
  form.onsubmit = event => {
    event.preventDefault();
    const submitter = event.submitter || post;
    void mutate(submitter, submitter.value, {task_id: detail.issue.id, message: message.value},
      submitter.value === "comment" ? "评论已保存" : "群聊已保存", "discussion");
  };
}

function negotiation(panel, detail) {
  const labels = {
    assignment_proposed: "提名", assignment_reply: "成员回复", assignment_confirmed: "确认负责人",
    assignment_blocked: "协商阻塞", assignment_question: "请求澄清", assignment_clarification_requested: "等待澄清",
  };
  const decisions = {revise: "反提议", recommend: "推荐同伴", blocked: "报告阻塞", accept: "接受"};
  const feed = node("div", "", panel, "feed");
  if (!detail.negotiations.length) empty(feed, "暂无分派记录");
  for (const event of detail.negotiations) {
    const payload = event.payload;
    const sender = payload.from || event.actor;
    const recipient = payload.to || payload.owner;
    const heading = (decisions[payload.decision] || labels[event.event_type] || event.event_type) +
      " · " + sender + (recipient && sender !== recipient ? " → " + recipient : "");
    const item = feedItem(feed, heading, [payload.subtask_id, payload.round ? "第 " + payload.round + " 轮" : "",
      when(event.created_at)].filter(Boolean).join(" · "), payload.reason || payload.question);
    if (payload.scope) node("p", "职责调整 · " + payload.scope, item, "feed-content");
    if (payload.member) node("p", "推荐 · " + payload.member, item, "feed-content");
    if (payload.description) node("p", "最终职责 · " + payload.description, item, "feed-content");
  }
}

function logs(panel, detail) {
  const feed = node("div", "", panel, "feed");
  if (!detail.logs.length) empty(feed, "暂无运行记录");
  for (const event of [...detail.logs].reverse()) {
    const payload = event.payload;
    const item = feedItem(feed, eventNames[event.event_type] || event.event_type,
      [event.actor, issueKey(event.task_id), when(event.created_at)].filter(Boolean).join(" · "),
      payload.reason || (typeof payload.result === "string" ? payload.result : null));
    const expand = node("details", "", item);
    node("summary", "查看事件详情", expand);
    node("pre", JSON.stringify(payload, null, 2), expand, "output");
  }
}

function artifacts(panel, detail) {
  const feed = node("div", "", panel, "feed");
  if (!detail.artifacts.length) empty(feed, "暂无产物");
  for (const artifact of detail.artifacts) {
    const item = feedItem(feed, artifact.path, [artifact.owner, issueKey(artifact.task_id),
      artifact.verified ? "已验证" : "已共享"].filter(Boolean).join(" · "));
    if (!artifact.archive) continue;
    const preview = node("pre", "", item, "output");
    preview.hidden = true;
    button(item, "查看产物", async control => {
      const before = location.hash;
      control.disabled = true;
      try {
        const response = await api("artifact", {task_id: detail.issue.id, event_id: artifact.event_id});
        if (before === location.hash && item.isConnected) {
          preview.textContent = response.content + (response.truncated ? "\n…预览已截断" : "");
          preview.hidden = false;
        }
      } catch (error) {
        if (before === location.hash) notice(error.message, true);
      } finally { control.disabled = false; }
    });
  }
}

function decisions(panel, detail) {
  node("h2", "审批与澄清", panel);
  const feed = node("div", "", panel, "feed");
  if (!detail.approvals.length) empty(feed, "暂无审批或澄清");
  for (const request of detail.approvals) {
    const clarification = request.kind === "clarification";
    const argumentsText = request.payload.arguments ? JSON.stringify(request.payload.arguments) : "";
    const item = feedItem(feed, clarification ? "澄清" : request.action || "审批",
      [request.agent, requestNames[request.status], when(request.created_at)].filter(Boolean).join(" · "),
      clarification ? request.payload.question : argumentsText);
    if (request.answer && request.answer.answer) node("p", request.answer.answer, item, "feed-content");
    if (!request.actionable) continue;
    const payload = {task_id: detail.issue.id, request_id: request.id};
    if (clarification) {
      const form = node("form", "", item, "stack");
      const text = field(form, "澄清回答", "textarea", "answer");
      text.required = true;
      const submit = formButton(form, "保存回答", "", "primary");
      form.onsubmit = event => {
        event.preventDefault();
        void mutate(submit, "answer", {...payload, answer: text.value}, "回答已保存", "decisions");
      };
    } else {
      const actions = node("div", "", item, "form-actions");
      button(actions, "批准", control => mutate(control, "answer", {...payload, approved: true}, "已批准", "decisions"), "primary");
      button(actions, "拒绝", control => mutate(control, "answer", {...payload, approved: false}, "已拒绝", "decisions"), "danger");
    }
  }

  node("h2", "交付审查", panel);
  const reviews = node("div", "", panel, "feed");
  for (const [index, run] of detail.runs.entries()) {
    if (!run.review_status) continue;
    feedItem(reviews, index ? "修订 " + index : "初次交付", reviewNames[run.review_status], run.review_feedback);
  }
  if (detail.next_action !== "review") {
    if (!reviews.children.length) empty(reviews, "交付完成后进入审查");
    return;
  }
  const form = node("form", "", panel, "stack");
  const feedback = field(form, "审查意见", "textarea", "feedback");
  feedback.placeholder = "退回时填写修改意见";
  const actions = node("div", "", form, "form-actions");
  const accept = formButton(actions, "接受交付", "accepted", "primary");
  formButton(actions, "退回修改", "changes_requested", "danger");
  form.onsubmit = event => {
    event.preventDefault();
    const submitter = event.submitter || accept;
    const changed = submitter.value === "changes_requested";
    if (changed && !feedback.value.trim()) {
      notice("请填写退回修改的原因", true);
      feedback.focus();
      return;
    }
    void mutate(submitter, "review", {task_id: detail.current.id, decision: submitter.value, feedback: feedback.value},
      changed ? "已退回，等待修订" : "交付已接受", changed ? "revisions" : "decisions");
  };
}

function revisions(panel, detail) {
  const feed = node("div", "", panel, "feed");
  for (const [index, run] of detail.runs.entries()) {
    const metadata = [executionNames[run.status], reviewNames[run.review_status]].filter(Boolean).join(" · ");
    const item = feedItem(feed, index ? "修订 " + index : "初次运行", metadata, run.review_feedback);
    if (index) node("p", run.prompt.split("\n").at(-1), item, "feed-content");
  }
  if (detail.next_action !== "revision") return;
  const form = node("form", "", panel, "stack");
  const prompt = field(form, "修订要求", "textarea", "prompt");
  prompt.value = detail.current.review_feedback || "";
  prompt.required = true;
  const submit = formButton(form, "创建修订", "", "primary");
  form.onsubmit = event => {
    event.preventDefault();
    void mutate(submit, "revision", {task_id: detail.current.id, prompt: prompt.value},
      "修订已创建", "revisions");
  };
}

const renderers = {discussion, negotiation, logs, artifacts, decisions, revisions};
function renderActivity() {
  const detail = state.detail;
  if (!detail) return;
  const tabs = el("issue-tabs");
  const panel = el("issue-panel");
  tabs.replaceChildren();
  panel.replaceChildren();
  for (const [key, label] of Object.entries(tabNames)) {
    const control = node("button", label, tabs, "tab");
    control.type = "button";
    control.setAttribute("aria-pressed", String(key === state.tab));
    control.onclick = () => openTab(key);
  }
  renderers[state.tab](panel, detail);
}

function renderDetail(detail) {
  const item = el("issue-heading");
  item.replaceChildren();
  const metadata = node("div", "", item, "issue-meta");
  node("span", issueKey(detail.issue.id), metadata, "issue-key");
  lane(metadata, detail.lane);
  node("h1", title(detail.issue), item);
  const rest = detail.issue.prompt.split("\n").slice(1).join("\n").trim();
  if (rest) node("p", rest, item);
  renderActivity();
  renderInspector(detail);
}

function renderWorkspace() {
  const {agents, agent_details: roles, squad, capabilities} = state.workspace;
  el("configure-form").elements.registry.value = JSON.stringify({agents: roles || [], squad}, null, 2);
  for (const name of ["dm", "memory"]) {
    const select = el(name + "-form").elements.agent;
    const previous = select.value;
    select.replaceChildren();
    for (const agent of agents) option(select, agent, agent);
    if (agents.includes(previous)) select.value = previous;
  }
  for (const name of ["configure", "eval"]) {
    for (const control of el(name + "-form").elements) control.disabled = capabilities?.[name] === false;
  }
  const groupList = el("group-list");
  groupList.replaceChildren();
  for (const item of state.messages.filter(item => item.kind === "group")) {
    feedItem(groupList, item.sender, when(item.created_at), item.content);
  }
  renderPrivate();
}

function renderPrivate() {
  const parent = el("dm-list");
  parent.replaceChildren();
  const agent = el("dm-form").elements.agent.value;
  for (const item of state.messages.filter(item => item.kind === "dm" &&
      (item.sender === agent || item.recipient === agent || item.agent === agent))) {
    feedItem(parent, item.sender, when(item.created_at), item.content);
  }
}

async function load() {
  const active = route();
  const generation = ++state.generation;
  const detail = active.view === "issue";
  const workspace = active.view === "workspace";
  el("list-view").hidden = detail || workspace;
  el("detail-view").hidden = !detail;
  el("workspace-view").hidden = !workspace;
  const crumb = el("breadcrumb");
  crumb.replaceChildren();
  link(crumb, "工单", "#issues");
  if (detail || workspace) {
    node("span", "/", crumb).setAttribute("aria-hidden", "true");
    node("strong", detail ? issueKey(active.key) : "工作区", crumb);
  }
  if (detail && state.detail?.issue.id !== active.key) {
    state.detail = null;
    el("issue-heading").replaceChildren();
    el("issue-tabs").replaceChildren();
    el("issue-panel").replaceChildren();
    el("inspector").replaceChildren();
    empty(el("issue-panel"), "正在读取工单");
  }
  try {
    const shell = await fetch("/api/state");
    if (!shell.ok) throw Error("无法读取工作区");
    const summary = await shell.json();
    if (generation !== state.generation) return;
    state.workspace = summary;
    renderNavigation(active);
    if (detail) {
      const records = await api("task", {task_id: active.key});
      if (generation !== state.generation) return;
      if (records.issue.id !== active.key) {
        location.replace(issueUrl(records.issue.id));
        return;
      }
      state.detail = records;
      renderDetail(records);
    } else if (workspace) {
      const data = await api("messages", {});
      if (generation !== state.generation) return;
      state.messages = data.messages;
      renderWorkspace();
    } else {
      const response = active.view === "owner" ?
        await api("tasks", {owner: active.key, owner_type: active.type}) : summary;
      if (generation !== state.generation) return;
      state.issues = response.tasks;
      renderList();
    }
  } catch (error) {
    if (generation === state.generation) {
      if (detail && state.detail === null) el("issue-panel").replaceChildren();
      notice(error.message, true);
    }
  }
}

for (const [key, label] of Object.entries(laneNames)) option(el("status-filter"), label, key);
el("owner-filter").onchange = event => {
  const [type, name] = event.target.value.split(":");
  location.hash = ownerUrl(name, type);
};
el("search").oninput = renderList;
el("status-filter").onchange = renderList;
el("refresh").onclick = () => { notice(); void load(); };
el("dm-form").elements.agent.onchange = renderPrivate;
window.addEventListener("hashchange", () => {
  state.tab = "discussion";
  notice();
  window.scrollTo(0, 0);
  void load();
});

const dialog = el("create-dialog");
const form = el("create-form");
el("new-issue").onclick = () => {
  form.reset();
  el("create-error").hidden = true;
  dialog.showModal();
};
el("close-create").onclick = () => dialog.close();
form.onsubmit = async event => {
  event.preventDefault();
  const submit = form.querySelector('button[type="submit"]');
  const {prompt, expected_path, expected_text, from_group, no_memory} = Object.fromEntries(new FormData(form));
  const error = el("create-error");
  submit.disabled = true;
  error.hidden = true;
  try {
    const payload = {prompt, from_group: Boolean(from_group), no_memory: Boolean(no_memory)};
    if (Boolean(expected_path) !== Boolean(expected_text)) throw Error("请同时填写验收文件和预期内容");
    if (expected_path) payload.checks = {s1: {path: expected_path, contains: expected_text}};
    const result = await api("create", payload);
    dialog.close();
    location.hash = issueUrl(result.task.id);
  } catch (failure) {
    error.textContent = failure.message;
    error.hidden = false;
  } finally { submit.disabled = false; }
};

for (const operation of ["configure", "group", "dm", "memory", "eval"]) {
  const workspaceForm = el(operation + "-form");
  workspaceForm.onsubmit = async event => {
    event.preventDefault();
    const control = workspaceForm.querySelector('button[type="submit"]');
    const before = location.hash;
    let payload = Object.fromEntries(new FormData(workspaceForm));
    control.disabled = true;
    notice();
    try {
      if (operation === "configure") payload = JSON.parse(payload.registry);
      if (operation === "eval") {
        payload.compare_memory = payload.memory === "compare";
        payload.no_memory = payload.memory === "off";
        delete payload.memory;
      }
      const result = await api(operation, payload);
      if (before !== location.hash) return;
      if (operation === "memory" || operation === "eval") {
        const output = el(operation + "-result");
        output.textContent = JSON.stringify(operation === "memory" ? result.memory : result, null, 2);
        output.hidden = false;
      } else {
        if (workspaceForm.elements.message) workspaceForm.elements.message.value = "";
        await load();
        if (before === location.hash) notice("已保存");
      }
    } catch (error) {
      if (before === location.hash) {
        notice(error.message, true);
        if (operation === "dm" || operation === "group") await load();
      }
    } finally { control.disabled = false; }
  };
}

void load();
