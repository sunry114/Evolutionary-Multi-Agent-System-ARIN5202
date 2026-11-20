"use strict";

const el = id => document.getElementById(id);
const token = document.querySelector('meta[name="csrf-token"]').content;
const workspaceId = location.pathname.split("/")[2] || "default";
const apiBase = location.pathname + "api/";
const laneNames = {
  backlog: "待办池", todo: "待处理", in_progress: "进行中", in_review: "待审查",
  blocked: "已阻塞", done: "已完成", cancelled: "已取消",
};
const runNames = {queued: "已排队", running: "运行中", stopping: "正在停止", stopped: "已停止",
  completed: "已完成", failed: "执行失败", interrupted: "执行中断"};
const runKinds = {assignment: "工单指派", mention: "评论提及", handoff: "成员回报"};
const activeRun = run => ["queued", "running", "stopping"].includes(run.status);
const routingNames = {squad: "队长协调", direct: "指定成员"};
const tabNames = {discussion: "讨论", logs: "运行记录", artifacts: "产物"};
const eventNames = {
  issue_run_queued: "运行入队", issue_run_started: "运行开始", issue_run_stop_requested: "请求停止",
  issue_run_finished: "运行结束", issue_run_output: "Agent 进度", issue_run_tool: "Claude Code 原生工具",
  issue_status_changed: "工单状态变更", issue_review_requested: "请求转待审查",
  issue_assigned: "更新负责人", plan_created: "生成执行计划", task_created: "创建任务", task_transition: "更新状态",
  routing_call: "路由模型调用", memory_recorded: "记录个人经验",
  harness_started: "启动执行器", harness_paused: "执行暂停", harness_finished: "执行结束", action_started: "工具启动",
  action_completed: "工具结束", objective_verified: "通过产物验收", artifact_published: "共享产物", orchestration_error: "执行异常",
  delegation_requested: "发起委派", delegation_completed: "返回委派结果", resume_claimed: "恢复执行",
  request_created: "发起人工请求", request_answered: "人工请求已处理", request_expired: "请求过期",
  review_decided: "人审决定", revision_linked: "创建修订", clarification_requested: "请求澄清",
};
const state = {workspace: null, detail: null, issues: [], tab: "discussion", generation: 0,
  chats: [], groups: [], chatKey: null, messages: [], drafts: new Map(), pending: new Map(), chatErrors: new Map()};
const markdown = window.markdownit?.({html: false, linkify: true});
const markdownTags = new Set(["p", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "ul", "ol", "li", "hr",
  "strong", "em", "s", "a", "table", "thead", "tbody", "tr", "th", "td"]);
if (markdown) markdown.validateLink = href => Boolean(markdownUrl(href));

function node(tag, value, parent, className) {
  const item = document.createElement(tag);
  if (className) item.className = className;
  if (value !== undefined && value !== null) item.textContent = String(value);
  if (parent) parent.append(item);
  return item;
}

function markdownUrl(href) {
  try {
    const url = new URL(href, document.baseURI);
    return ["http:", "https:", "mailto:"].includes(url.protocol) ? url : null;
  } catch {
    return null;
  }
}

function markdownAttributes(element, part) {
  if (part.tag === "a") {
    const href = part.attrGet("href");
    const url = markdownUrl(href);
    if (!url) return;
    element.setAttribute("href", href);
    const title = part.attrGet("title");
    if (title) element.title = title;
    if (url.protocol !== "mailto:" && url.origin !== location.origin) {
      element.target = "_blank";
      element.rel = "noopener noreferrer";
    }
  } else if (part.tag === "ol") {
    element.start = Number(part.attrGet("start") || 1);
  } else if (part.tag === "td" || part.tag === "th") {
    const alignment = part.attrGet("style")?.match(/^text-align:(left|center|right)$/)?.[1];
    if (alignment) element.dataset.align = alignment;
  }
}

function appendMarkdown(parent, parts) {
  const open = [parent];
  for (const part of parts) {
    if (part.hidden) continue;
    const target = open[open.length - 1];
    switch (part.type) {
      case "inline": appendMarkdown(target, part.children); break;
      case "softbreak":
      case "hardbreak": node("br", null, target); break;
      case "image": target.append(markdown.renderer.renderInlineAsText(part.children, markdown.options, {})); break;
      case "code_inline": node("code", part.content, target); break;
      case "fence":
      case "code_block": node("code", part.content, node("pre", null, target)); break;
      default: {
        if (!markdownTags.has(part.tag)) {
          if (part.content) target.append(part.content);
          break;
        }
        if (part.nesting === -1) {
          open.pop();
          break;
        }
        const wrap = part.tag === "table" ? node("div", null, target, "markdown-table") : target;
        const element = node(part.tag, null, wrap);
        markdownAttributes(element, part);
        if (part.nesting === 1) open.push(element);
      }
    }
  }
}

function markdownMessage(parent, content, className) {
  if (!content) return;
  if (!markdown) {
    node("div", content, parent, className);
    return;
  }
  const body = node("div", null, parent, className + " markdown-body");
  appendMarkdown(body, markdown.parse(content, {}));
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

async function api(operation, payload, base = apiBase) {
  const response = await fetch(base + operation, {
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
    if (view === "chat" && key === "group" && name) {
      const groupId = decodeURIComponent(name);
      return {view, key: "group/" + groupId, groupId};
    }
    if (view === "chat") return {view, key: key ? decodeURIComponent(key) : "group"};
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
function chatUrl(id) { return "#chat/" + encodeURIComponent(id); }
function groupChatUrl(id) { return "#chat/group/" + encodeURIComponent(id); }
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
  const section = ["chat", "workspace"].includes(active.view) ? active.view : "issues";
  for (const item of document.querySelectorAll("[data-route]")) {
    if (item.dataset.route === section) {
      item.setAttribute("aria-current", "page");
    } else item.removeAttribute("aria-current");
  }
  for (const item of document.querySelectorAll("[data-issue-route]")) {
    if (section === "issues" && item.dataset.issueRoute === (active.view === "attention" ? "attention" : "issues")) {
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
  const needsAttention = ["in_review", "blocked"];
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
  const current = detail.issue;
  const runs = detail.executions || [];
  const next = node("section", "", parent, "card next");
  node("h2", "工单状态", next);
  if (state.workspace.capabilities?.issue_runs) {
    const form = node("form", "", next, "stack");
    const status = field(form, "状态", "select", "status");
    for (const [value, label] of Object.entries(laneNames)) {
      option(status, label, value);
    }
    status.value = detail.lane;
    const apply = formButton(form, "更新状态", "", "primary");
    form.onsubmit = event => {
      event.preventDefault();
      void mutate(apply, "issue-status", {task_id: current.id, status: status.value}, "已更新状态");
    };
  }
  if (!current.assignee_id) {
    assignmentForm(next, current);
  } else {
    const edit = node("details", "", next);
    node("summary", "更改负责人", edit);
    assignmentForm(edit, current);
    if (detail.lane === "todo" && !runs.some(run => run.kind === "assignment")) {
      button(next, "启动工单", control => mutate(control, "start", {task_id: current.id}, "已提交运行", "logs"));
    }
  }
  const active = runs.find(activeRun);
  if (active) {
    node("p", `${active.agent} · ${runNames[active.status]}`, next, "muted");
    button(next, "查看运行", () => openTab("logs"));
  }

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
    const card = feedItem(feed, item.sender === "human" ? "你" : item.sender,
      [title, when(item.created_at)].filter(Boolean).join(" · "));
    markdownMessage(card, item.content, "feed-content");
  }
  const form = node("form", "", panel, "stack");
  const message = field(form, "工单讨论", "textarea", "message");
  message.required = true;
  message.maxLength = 8000;
  message.placeholder = "留言，或用 @Agent 名称请成员执行";
  if (state.workspace.capabilities?.issue_runs) {
    const mentions = node("div", "", form, "form-actions");
    for (const agent of state.workspace.agents) button(mentions, "@" + agent, () => {
      const prefix = message.value && !/\s$/.test(message.value) ? " " : "";
      message.value += prefix + "@" + agent + " ";
      message.focus();
    }, "quiet");
  }
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

function logs(panel, detail) {
  const executions = node("div", "", panel, "feed");
  for (const run of detail.executions || []) {
    const card = feedItem(executions, `${run.agent} · ${runNames[run.status]}`,
      [runKinds[run.kind], when(run.created_at), run.retry_of ? "重试" : ""].filter(Boolean).join(" · "));
    markdownMessage(card, run.result, "feed-content");
    const controls = node("div", "", card, "form-actions");
    const payload = {task_id: detail.issue.id, run_id: run.id};
    if (["queued", "running"].includes(run.status)) {
      button(controls, "停止运行", control => mutate(control, "stop-run", payload, "已提交停止请求", "logs"), "danger");
    }
    if (run.latest_attempt && ["failed", "stopped"].includes(run.status)) {
      button(controls, "重试运行", control => mutate(control, "retry-run", payload, "重试已入队", "logs"));
    }
  }
  const feed = node("div", "", panel, "feed");
  if (!detail.logs.length) empty(feed, "暂无运行记录");
  for (const event of [...detail.logs, ...detail.negotiations].sort((a, b) => b.id - a.id)) {
    const payload = event.payload;
    const toolStatus = {started: "开始", completed: "完成", failed: "失败"};
    const text = event.event_type === "issue_run_tool" ? payload.name + " · " + toolStatus[payload.status]
      : payload.reason || (typeof payload.result === "string" ? payload.result : payload.result?.message);
    const item = feedItem(feed, eventNames[event.event_type] || event.event_type,
      [event.actor, issueKey(event.task_id), when(event.created_at)].filter(Boolean).join(" · "),
      text);
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

const renderers = {discussion, logs, artifacts};
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

const squadForm = el("configure-form");
const squadFields = el("squad-fields");
const squadMembers = el("squad-members");
const agentDialog = el("agent-dialog");
const agentForm = el("agent-form");
const agentEditFields = el("agent-edit-fields");
const rosterDialog = el("roster-dialog");
const rosterForm = el("roster-form");
const rosterFields = el("roster-fields");
const rosterRows = el("roster-rows");

function configFeedback(id, message = "", error = false) {
  const feedback = el(id);
  feedback.textContent = message;
  feedback.classList.toggle("form-error", error);
  feedback.hidden = !message;
}

function workspaceUrl(id, view) {
  const base = id === "default" ? "/" : "/w/" + encodeURIComponent(id) + "/";
  return base + "#" + view;
}

const workspaceMenu = el("workspace-menu");
const workspaceSwitch = el("workspace-switch");
let workspaceChoices = [];

function renderWorkspaceChoices() {
  const list = el("workspace-list");
  list.replaceChildren();
  const view = ["chat", "workspace"].includes(route().view) ? route().view : "issues";
  for (const workspace of workspaceChoices) {
    const choice = link(list, "", workspaceUrl(workspace.id, view));
    choice.className = "workspace-choice";
    node("span", Array.from(workspace.name)[0].toUpperCase(), choice, "workspace-mark").setAttribute("aria-hidden", "true");
    node("span", workspace.name, choice);
    if (workspace.id === workspaceId) {
      choice.setAttribute("aria-current", "true");
      node("span", "✓", choice).setAttribute("aria-hidden", "true");
      choice.onclick = event => {
        event.preventDefault();
        workspaceMenu.open = false;
        workspaceSwitch.focus();
      };
      el("workspace-mark").textContent = Array.from(workspace.name)[0].toUpperCase();
      el("workspace-name").textContent = workspace.name;
      workspaceSwitch.title = workspace.name;
    }
  }
}

async function loadWorkspaces() {
  try {
    const response = await fetch("/api/workspaces");
    if (!response.ok) return;
    const {workspaces} = await response.json();
    if (JSON.stringify(workspaceChoices) !== JSON.stringify(workspaces)) {
      workspaceChoices = workspaces;
      renderWorkspaceChoices();
    }
    workspaceMenu.hidden = false;
  } catch {
    // Workspace data routes are independent of the workspace selector.
  }
}

workspaceMenu.ontoggle = () => {
  if (workspaceMenu.open) {
    renderWorkspaceChoices();
    void loadWorkspaces();
  }
};
document.addEventListener("click", event => {
  if (workspaceMenu.open && !workspaceMenu.contains(event.target)) workspaceMenu.open = false;
});
workspaceMenu.onkeydown = event => {
  if (event.key === "Escape" && workspaceMenu.open) {
    event.preventDefault();
    workspaceMenu.open = false;
    workspaceSwitch.focus();
  }
};

const workspaceDialog = el("workspace-dialog");
const workspaceForm = el("workspace-form");
const workspaceSubmit = workspaceForm.querySelector("button[type=submit]");
el("new-workspace").onclick = () => {
  workspaceMenu.open = false;
  workspaceForm.reset();
  configFeedback("workspace-create-error");
  workspaceDialog.showModal();
};
el("close-workspace").onclick = () => workspaceDialog.close();
workspaceDialog.oncancel = event => {
  if (workspaceSubmit.disabled) event.preventDefault();
};
workspaceForm.onsubmit = async event => {
  event.preventDefault();
  if (workspaceSubmit.disabled) return;
  const name = workspaceForm.elements.name.value.trim();
  workspaceSubmit.disabled = el("close-workspace").disabled = true;
  configFeedback("workspace-create-error");
  try {
    const {workspace} = await api("new-workspace", {name}, "/api/");
    location.assign(workspaceUrl(workspace.id, "workspace"));
  } catch (error) {
    configFeedback("workspace-create-error", error.message, true);
    workspaceSubmit.disabled = el("close-workspace").disabled = false;
  }
};

function syncSquadLead() {
  for (const checkbox of squadMembers.querySelectorAll("input")) {
    checkbox.disabled = checkbox.value === squadForm.elements.lead.value;
    if (checkbox.disabled) checkbox.checked = true;
  }
}

function renderMemberOptions(parent, agents, selected = []) {
  parent.replaceChildren();
  for (const name of agents) {
    const label = node("label", "", parent, "check");
    const checkbox = node("input", "", label);
    checkbox.type = "checkbox";
    checkbox.value = name;
    checkbox.checked = selected.includes(name);
    node("span", name, label);
  }
}

function checkedMembers(parent) {
  return [...parent.querySelectorAll("input:checked")].map(input => input.value);
}

function renderSquadSettings() {
  const {agents, squad, capabilities} = state.workspace;
  const lead = squadForm.elements.lead;
  squadForm.elements.squad_name.value = squad?.name || "";
  lead.replaceChildren();
  for (const name of agents) option(lead, name, name);
  renderMemberOptions(squadMembers, agents, squad?.members);
  lead.value = squad?.lead || "";
  syncSquadLead();
  squadFields.disabled = !squad || capabilities?.configure === false;
}

function renderAgentProfiles() {
  const {agents, agent_details: roles, capabilities} = state.workspace;
  const list = el("agent-profiles");
  list.replaceChildren();
  el("manage-agents").disabled = capabilities?.configure === false || !roles?.length;
  for (const agent of roles || agents.map(name => ({name, specialty: ""}))) {
    const row = node("li", "", list, "agent-profile");
    avatar(row, agent.name);
    const text = node("div", "", row);
    node("h3", agent.name, text);
    if (agent.specialty) node("p", agent.specialty, text, "muted");
    const edit = button(row, "编辑专长", () => {
      agentForm.dataset.agent = agent.name;
      el("agent-edit-title").textContent = `编辑 ${agent.name} 的专长`;
      agentForm.elements.specialty.value = agent.specialty;
      configFeedback("agent-edit-error");
      configFeedback("agent-feedback");
      agentDialog.showModal();
    }, "quiet");
    edit.id = "edit-agent-" + agent.name;
    edit.setAttribute("aria-label", `编辑 ${agent.name} 的专长`);
    edit.disabled = capabilities?.configure === false;
  }
}

function updateRosterControls() {
  el("add-agent").disabled = rosterRows.children.length >= 8;
  for (const remove of rosterRows.querySelectorAll("button")) remove.disabled = rosterRows.children.length <= 1;
}

function addRosterRow(agent = {name: "", specialty: ""}) {
  const row = node("div", "", rosterRows, "agent-editor");
  const header = node("div", "", row, "agent-editor-title");
  const name = node("input", "", node("label", "Agent 名称", header));
  name.name = "name";
  name.maxLength = 64;
  name.pattern = "[A-Za-z0-9_\\-]{1,64}";
  name.required = true;
  name.value = agent.name;
  button(header, "移除", () => {
    row.remove();
    updateRosterControls();
    el("add-agent").focus();
  }, "quiet");
  const specialty = node("textarea", "", node("label", "专长", row));
  specialty.name = "specialty";
  specialty.rows = 2;
  specialty.maxLength = 1000;
  specialty.required = true;
  specialty.value = agent.specialty;
  updateRosterControls();
  return name;
}

el("manage-agents").onclick = () => {
  rosterRows.replaceChildren();
  for (const agent of state.workspace.agent_details) addRosterRow(agent);
  configFeedback("roster-error");
  rosterDialog.showModal();
};
el("add-agent").onclick = () => addRosterRow().focus();
el("close-roster").onclick = () => rosterDialog.close();
rosterDialog.oncancel = event => {
  if (rosterFields.disabled) event.preventDefault();
};
rosterForm.onsubmit = async event => {
  event.preventDefault();
  if (rosterFields.disabled) return;
  const agents = [...rosterRows.children].map(row => ({
    name: row.querySelector("input").value.trim(),
    specialty: row.querySelector("textarea").value.trim(),
  }));
  rosterFields.disabled = el("close-roster").disabled = true;
  configFeedback("roster-error");
  try {
    await api("configure", {agents});
    rosterDialog.close();
    await load();
    configFeedback("agent-feedback", "Agent 配置已保存");
    el("manage-agents").focus();
  } catch (error) {
    configFeedback("roster-error", error.message, true);
  } finally {
    rosterFields.disabled = el("close-roster").disabled = false;
  }
};

function renderWorkspace() {
  const {agents, capabilities} = state.workspace;
  renderSquadSettings();
  renderAgentProfiles();
  configFeedback("squad-feedback");
  configFeedback("agent-feedback");
  const select = el("memory-form").elements.agent;
  const previous = select.value;
  select.replaceChildren();
  for (const agent of agents) option(select, agent, agent);
  if (agents.includes(previous)) select.value = previous;
  for (const control of el("eval-form").elements) control.disabled = capabilities?.eval === false;
}

const chatForm = el("chat-form");
const chatInput = chatForm.elements.message;
const chatDialog = el("chat-dialog");

function avatar(parent, name, style = "") {
  const [initial = ""] = name;
  const item = node("span", initial.toUpperCase(), parent, `chat-avatar ${style}`.trim());
  item.setAttribute("aria-hidden", "true");
}

function renderChatNavigation(key) {
  const group = el("chat-group");
  if (key === "group") group.setAttribute("aria-current", "page");
  else group.removeAttribute("aria-current");
  const groupList = el("group-list");
  groupList.replaceChildren();
  for (const room of state.groups) {
    const entry = link(groupList, "", groupChatUrl(room.id));
    if (key === "group/" + room.id) entry.setAttribute("aria-current", "page");
    avatar(entry, room.name, "chat-avatar-group");
    const title = node("span", room.name, entry, "chat-link-title");
    title.title = room.name;
  }
  const list = el("chat-list");
  list.replaceChildren();
  for (const chat of state.chats) {
    const label = chat.title?.trim().split("\n")[0];
    const entry = link(list, "", chatUrl(chat.id));
    if (key === chat.id) entry.setAttribute("aria-current", "page");
    avatar(entry, chat.agent);
    const info = node("span", "", entry, "chat-link-info");
    const heading = node("span", label || `与 ${chat.agent} 的聊天`, info, "chat-link-title");
    heading.title = heading.textContent;
    node("small", [label && chat.agent, when(chat.updated_at)].filter(Boolean).join(" · "), info);
  }
}

function chatMessage(parent, message) {
  const human = message.sender === "human";
  const sender = human ? "你" : message.sender;
  const row = node("article", "", parent, human ? "chat-message chat-message-human" : "chat-message");
  avatar(row, sender, human ? "chat-avatar-human" : "");
  const body = node("div", "", row, "chat-message-body");
  const meta = node("div", "", body, "chat-message-meta");
  node("strong", sender, meta);
  if (message.created_at) {
    node("time", when(message.created_at), meta).dateTime = message.created_at;
  }
  markdownMessage(body, message.content, "chat-bubble");
}

function hasPosted(messages, pending) {
  return messages.some(row => row.sender === "human" && row.content === pending.message && !pending.before.has(row.id));
}

function renderChat(active, loading = false) {
  const workspaceGroup = active.key === "group";
  const group = workspaceGroup || Boolean(active.groupId);
  const room = state.groups.find(item => item.id === active.groupId);
  const chat = state.chats.find(item => item.id === active.key);
  const name = workspaceGroup ? "工作区群聊" : (room?.name || chat?.agent || "聊天");
  const icon = el("chat-avatar");
  icon.classList.toggle("chat-avatar-group", group);
  icon.textContent = group ? "群" : name.slice(0, 1).toUpperCase();
  el("chat-title").textContent = name;
  const agents = state.workspace?.agents || [];
  const members = room?.members || agents;
  const available = members.some(member => agents.includes(member));
  const removed = chat && !agents.includes(chat.agent);
  el("chat-caption").textContent = group
    ? members.map(member => agents.includes(member) ? member : member + "（已移出工作区）").join(" · ")
    : removed ? "该 Agent 已移出工作区" : "私聊";
  renderChatNavigation(active.key);
  const messages = el("chat-messages");
  messages.replaceChildren();
  const pending = state.pending.get(active.key);
  if (loading) {
    empty(messages, "正在读取聊天");
  } else if (!state.messages.length && !pending) {
    const placeholder = node("div", "", messages, "chat-conversation-empty");
    avatar(placeholder, group ? "群" : name, group ? "chat-avatar-group" : "");
    const description = group && !available ? "群聊暂无可回复的 Agent" : `向 ${name} 发送第一条消息`;
    node("p", description, placeholder);
  } else {
    for (const message of state.messages) chatMessage(messages, message);
    if (pending) {
      if (!hasPosted(state.messages, pending)) chatMessage(messages, {sender: "human", content: pending.message});
      node("p", group ? "成员正在回复…" : `${name} 正在回复…`, messages, "chat-pending");
    }
  }
  chatForm.hidden = !workspaceGroup && !room && !chat;
  chatInput.value = state.drafts.get(active.key) || "";
  chatInput.disabled = Boolean(removed || loading || pending || (group && !available));
  chatInput.placeholder = group ? `发消息到 ${name}` : `发消息给 ${name}`;
  chatForm.querySelector("button[type=submit]").disabled = chatInput.disabled;
  const feedback = state.chatErrors.get(active.key);
  el("chat-feedback").textContent = feedback || "";
  el("chat-feedback").hidden = !feedback;
  const transcript = el("chat-transcript");
  transcript.scrollTop = transcript.scrollHeight;
}

function chatMessageScope(active) {
  if (active.groupId) return {group_id: active.groupId};
  return active.key === "group" ? {scope: "group"} : {chat_id: active.key};
}

async function load() {
  const active = route();
  const generation = ++state.generation;
  const detail = active.view === "issue";
  const workspace = active.view === "workspace";
  const chatting = active.view === "chat";
  el("list-view").hidden = detail || workspace || chatting;
  el("detail-view").hidden = !detail;
  el("workspace-view").hidden = !workspace;
  el("chat-view").hidden = !chatting;
  el("breadcrumb").hidden = chatting;
  el("new-issue").hidden = workspace || chatting;
  el("section-nav").hidden = workspace;
  el("chat-navigation").hidden = !chatting;
  el("issue-navigation").hidden = chatting;
  const shellLayout = document.querySelector(".shell").classList;
  shellLayout.toggle("without-section", workspace);
  shellLayout.toggle("is-chat", chatting);
  if (chatting && state.chatKey !== active.key) {
    state.chatKey = active.key;
    state.messages = [];
    renderChat(active, true);
  }
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
    const shell = await fetch(apiBase + "state");
    if (!shell.ok) throw Error("无法读取工作区");
    const summary = await shell.json();
    if (generation !== state.generation) return;
    state.workspace = summary;
    if (active.view === "owner" && active.type === "squad" && summary.squad && active.key !== summary.squad.name) {
      location.replace(ownerUrl(summary.squad.name, "squad"));
      return;
    }
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
    } else if (chatting) {
      const index = await api("chat-threads", {});
      if (generation !== state.generation) return;
      state.chats = index.chats;
      state.groups = index.groups || [];
      const exists = active.groupId
        ? state.groups.some(group => group.id === active.groupId)
        : active.key === "group" || state.chats.some(chat => chat.id === active.key);
      if (!exists) {
        location.replace(chatUrl("group"));
        return;
      }
      const history = await api("messages", chatMessageScope(active));
      if (generation !== state.generation) return;
      state.messages = history.messages;
      renderChat(active);
    } else if (workspace) {
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
el("refresh").onclick = () => { notice(); void load(); void loadWorkspaces(); };
window.addEventListener("hashchange", () => {
  state.tab = "discussion";
  notice();
  window.scrollTo(0, 0);
  void load();
});

let creatingChat = false;
el("new-chat").onclick = () => {
  const picker = el("chat-agent-picker");
  picker.replaceChildren();
  el("chat-create-error").hidden = true;
  for (const agent of state.workspace?.agents || []) {
    const choice = button(picker, "", async control => {
      if (creatingChat) return;
      creatingChat = true;
      control.disabled = true;
      el("chat-create-error").hidden = true;
      try {
        const {chat} = await api("new-chat", {agent});
        if (chatDialog.open) {
          chatDialog.close();
          location.hash = chatUrl(chat.id);
        }
      } catch (error) {
        const feedback = el("chat-create-error");
        feedback.textContent = error.message;
        feedback.hidden = false;
      } finally {
        control.disabled = false;
        creatingChat = false;
      }
    }, "quiet");
    avatar(choice, agent);
    const info = node("span", "", choice, "chat-agent-info");
    node("span", agent, info);
    const specialty = state.workspace.agent_details?.find(item => item.name === agent)?.specialty;
    if (specialty) node("small", specialty, info);
  }
  if (!picker.children.length) empty(picker, "工作区暂无 Agent");
  chatDialog.showModal();
};

const groupDialog = el("group-dialog");
const groupForm = el("group-form");
const groupFields = el("group-fields");
const groupMembers = el("group-members");
groupForm.oninput = () => configFeedback("group-create-error");
el("new-group").onclick = () => {
  groupForm.reset();
  renderMemberOptions(groupMembers, state.workspace?.agents || []);
  configFeedback("group-create-error");
  groupDialog.showModal();
};
el("close-group").onclick = () => groupDialog.close();
groupDialog.oncancel = event => {
  if (groupFields.disabled) event.preventDefault();
};
groupForm.onsubmit = async event => {
  event.preventDefault();
  if (groupFields.disabled) return;
  const name = groupForm.elements.group_name.value.trim();
  const members = checkedMembers(groupMembers);
  if (!name || !members.length) {
    configFeedback("group-create-error", !name ? "请输入群聊名称" : "请选择至少一个 Agent", true);
    return;
  }
  groupFields.disabled = el("close-group").disabled = true;
  configFeedback("group-create-error");
  try {
    const {group} = await api("new-group", {name, members});
    groupDialog.close();
    location.hash = groupChatUrl(group.id);
  } catch (error) {
    configFeedback("group-create-error", error.message, true);
  } finally {
    groupFields.disabled = el("close-group").disabled = false;
  }
};

chatInput.oninput = () => state.drafts.set(state.chatKey, chatInput.value);
chatForm.onsubmit = async event => {
  event.preventDefault();
  const active = route();
  const message = chatInput.value.trim();
  if (active.view !== "chat" || state.chatKey !== active.key || !message || state.pending.has(active.key)) return;
  const pending = {message, before: new Set(state.messages.map(row => row.id))};
  state.pending.set(active.key, pending);
  state.drafts.delete(active.key);
  state.chatErrors.delete(active.key);
  renderChat(active);
  try {
    if (active.groupId) await api("group", {group_id: active.groupId, message});
    else if (active.key === "group") await api("group", {message});
    else await api("dm", {chat_id: active.key, message});
  } catch (error) {
    state.chatErrors.set(active.key, error.message);
    try {
      const history = await api("messages", chatMessageScope(active));
      if (!hasPosted(history.messages, pending)) state.drafts.set(active.key, message);
    } catch {
      state.drafts.set(active.key, message);
    }
  } finally {
    state.pending.delete(active.key);
    if (route().view === "chat") {
      await load();
      if (route().key === active.key && !chatInput.disabled) chatInput.focus();
    }
  }
};

const dialog = el("create-dialog");
const form = el("create-form");
el("new-issue").onclick = () => {
  form.reset();
  const select = form.elements.assignee;
  select.replaceChildren();
  option(select, "先不指派", "");
  if (state.workspace?.squad) option(select, "Squad · " + state.workspace.squad.name, "squad:" + state.workspace.squad.name);
  for (const agent of state.workspace?.agents || []) option(select, "Agent · " + agent, "agent:" + agent);
  el("create-error").hidden = true;
  dialog.showModal();
};
el("close-create").onclick = () => dialog.close();
form.onsubmit = async event => {
  event.preventDefault();
  const submit = form.querySelector('button[type="submit"]');
  const {prompt, assignee, from_group, no_memory} = Object.fromEntries(new FormData(form));
  const error = el("create-error");
  submit.disabled = true;
  error.hidden = true;
  try {
    const payload = {prompt, from_group: Boolean(from_group)};
    if (no_memory) payload.no_memory = true;
    if (assignee) {
      const [type, id] = assignee.split(":");
      payload[type] = id;
    }
    const result = await api("create", payload);
    dialog.close();
    location.hash = issueUrl(result.task.id);
  } catch (failure) {
    error.textContent = failure.message;
    error.hidden = false;
  } finally { submit.disabled = false; }
};

squadForm.elements.lead.onchange = syncSquadLead;
squadForm.oninput = () => configFeedback("squad-feedback");
squadForm.onsubmit = async event => {
  event.preventDefault();
  if (squadFields.disabled) return;
  const {squad_name, lead} = squadForm.elements;
  const checked = new Set(checkedMembers(squadMembers));
  const members = state.workspace.squad.members.filter(name => checked.delete(name)).concat([...checked]);
  squadFields.disabled = true;
  configFeedback("squad-feedback");
  try {
    const {squad} = await api("configure", {squad: {name: squad_name.value, lead: lead.value, members}});
    if (route().view === "workspace") {
      state.workspace.squad = squad;
      renderSquadSettings();
      configFeedback("squad-feedback", "小队配置已保存");
    }
  } catch (error) {
    if (route().view === "workspace") configFeedback("squad-feedback", error.message, true);
  } finally {
    squadFields.disabled = !state.workspace?.squad || state.workspace.capabilities?.configure === false;
  }
};

el("close-agent").onclick = el("cancel-agent").onclick = () => {
  if (!agentEditFields.disabled) agentDialog.close();
};
agentDialog.oncancel = event => {
  if (agentEditFields.disabled) event.preventDefault();
};
agentForm.onsubmit = async event => {
  event.preventDefault();
  if (agentEditFields.disabled) return;
  const name = agentForm.dataset.agent;
  const specialty = agentForm.elements.specialty.value;
  agentEditFields.disabled = el("close-agent").disabled = true;
  configFeedback("agent-edit-error");
  try {
    const {agents} = await api("configure", {agent: {name, specialty}});
    const saved = agents.find(item => item.name === name);
    agentDialog.close();
    if (route().view === "workspace") {
      const profiles = state.workspace.agent_details || agents;
      state.workspace.agent_details = profiles.map(agent => agent.name === name ? saved : agent);
      renderAgentProfiles();
      configFeedback("agent-feedback", `${name} 的专长已保存`);
      el("edit-agent-" + name)?.focus();
    }
  } catch (error) {
    configFeedback("agent-edit-error", error.message, true);
  } finally {
    agentEditFields.disabled = el("close-agent").disabled = false;
  }
};

for (const operation of ["memory", "eval"]) {
  const workspaceForm = el(operation + "-form");
  workspaceForm.onsubmit = async event => {
    event.preventDefault();
    const control = workspaceForm.querySelector('button[type="submit"]');
    const before = location.hash;
    const payload = Object.fromEntries(new FormData(workspaceForm));
    control.disabled = true;
    notice();
    try {
      if (operation === "eval") {
        payload.compare_memory = payload.memory === "compare";
        payload.no_memory = payload.memory === "off";
        delete payload.memory;
      }
      const result = await api(operation, payload);
      if (before !== location.hash) return;
      const output = el(operation + "-result");
      output.textContent = JSON.stringify(operation === "memory" ? result.memory : result, null, 2);
      output.hidden = false;
    } catch (error) {
      if (before === location.hash) notice(error.message, true);
    } finally { control.disabled = false; }
  };
}

let polling = false;
window.setInterval(async () => {
  if (polling || !state.workspace?.capabilities?.issue_runs || document.hidden || dialog.open) return;
  const active = route();
  if (["workspace", "chat"].includes(active.view) || (active.view === "issue" && !state.detail)) return;
  const generation = state.generation;
  polling = true;
  try {
    if (active.view !== "issue") {
      const filters = active.view === "owner" ? {owner: active.key, owner_type: active.type} : {};
      const result = await api("tasks", filters);
      if (generation === state.generation && JSON.stringify(result.tasks) !== JSON.stringify(state.issues)) {
        state.issues = result.tasks;
        renderList();
      }
      return;
    }
    const records = await api("task", {task_id: active.key});
    if (generation !== state.generation || JSON.stringify(records) === JSON.stringify(state.detail)) return;
    if (el("inspector").contains(document.activeElement)) return;
    const draft = el("issue-panel").querySelector("textarea");
    if (draft?.value || el("issue-panel").contains(document.activeElement)) {
      state.detail = {...state.detail, lane: records.lane, issue: records.issue, executions: records.executions};
      renderInspector(state.detail);
    } else if (state.tab === "artifacts" && JSON.stringify(records.artifacts) === JSON.stringify(state.detail.artifacts)) {
      state.detail = {...state.detail, lane: records.lane, issue: records.issue, executions: records.executions};
      renderInspector(state.detail);
    } else {
      state.detail = records;
      renderDetail(records);
    }
  } catch (_) {
    // Keep the current view during a transient polling error.
  } finally { polling = false; }
}, 750);
void load();
void loadWorkspaces();
