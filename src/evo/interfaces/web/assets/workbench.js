"use strict";

const el = id => document.getElementById(id);
const token = document.querySelector('meta[name="csrf-token"]').content;
const projectMode = document.body.hasAttribute("data-projects");
const projectId = location.pathname.startsWith("/p/") ? location.pathname.split("/")[2] : "";
const workspaceId = location.pathname.startsWith("/w/") ? location.pathname.split("/")[2] : "";
const catalogView = projectMode && !projectId;
const apiBase = location.pathname + "api/";
const defaultGroupName = "工作区群聊";
const laneNames = {
  backlog: "待办池", todo: "待处理", in_progress: "进行中", in_review: "待审查",
  blocked: "已阻塞", done: "已完成", cancelled: "已取消",
};
const runNames = {queued: "已排队", running: "运行中", stopping: "正在停止", stopped: "已停止",
  completed: "已完成", failed: "执行失败", interrupted: "执行中断"};
const runKinds = {assignment: "工单指派", mention: "评论提及", handoff: "成员回报"};
const activeRun = run => ["queued", "running", "stopping"].includes(run.status);
const runToolEvents = new Set(["issue_run_tool", "issue_run_platform_tool"]);
const runProgressEvents = new Set(["issue_run_output", "issue_run_api_retry", ...runToolEvents]);
const toolStatus = {started: "开始", completed: "完成", success: "完成", failed: "失败",
  blocked: "已阻塞", waiting_human: "等待处理", waiting_dependency: "等待依赖"};
const routingNames = {squad: "队长协调", direct: "指定成员"};
const tabNames = {discussion: "讨论", logs: "运行记录", artifacts: "产物"};
const eventNames = {
  issue_run_queued: "运行入队", issue_run_started: "运行开始", issue_run_stop_requested: "请求停止",
  issue_run_finished: "运行结束", issue_run_output: "Agent 进度", issue_run_tool: "Claude Code 原生工具",
  issue_run_platform_tool: "平台工具", issue_run_api_retry: "Claude Code API 重试", issue_run_native_session: "Claude Code 会话",
  issue_status_changed: "工单状态变更", issue_review_requested: "请求转待审查",
  issue_assigned: "更新负责人", issue_group_linked: "关联群聊", plan_created: "生成执行计划",
  task_created: "创建任务", task_transition: "更新状态",
  routing_call: "路由模型调用", memory_recorded: "记录个人经验",
  harness_started: "启动执行器", harness_paused: "执行暂停", harness_finished: "执行结束", action_started: "工具启动",
  action_completed: "工具结束", objective_verified: "通过产物验收", artifact_published: "共享产物", orchestration_error: "执行异常",
  delegation_requested: "发起委派", delegation_completed: "返回委派结果", resume_claimed: "恢复执行",
  request_created: "发起人工请求", request_answered: "人工请求已处理", request_expired: "请求过期",
  review_decided: "人审决定", revision_linked: "创建修订", clarification_requested: "请求澄清",
};
const state = {workspace: null, detail: null, issues: [], tab: "discussion", generation: 0,
  chats: [], groups: [], chatKey: null, messages: [], drafts: new Map(), pending: new Map(), chatErrors: new Map()};
let discussionView = null;
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
  if (!markdown) return node("div", content, parent, className);
  const body = node("div", null, parent, className + " markdown-body");
  appendMarkdown(body, markdown.parse(content, {}));
  return body;
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
  if (catalogView && !["workspace", "chat"].includes(view)) return {view: "projects"};
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
    return {view: catalogView ? "projects" : "issues"};
  }
  return {view: ["attention", "workspace", "tools"].includes(view) ? view : "issues"};
}

function issueUrl(id) { return "#issue/" + encodeURIComponent(id); }
function chatUrl(id) { return "#chat/" + encodeURIComponent(id); }
function groupChatUrl(id) { return "#chat/group/" + encodeURIComponent(id); }
function ownerUrl(name, type) { return name ? "#owner/" + type + "/" + encodeURIComponent(name) : "#issues"; }
function issueKey(id) { return "EVO-" + id.slice(-7).toUpperCase(); }
function issueTitle(issue) { return issue.title || issue.prompt.split("\n")[0]; }
function when(iso) { return iso ? new Date(iso).toLocaleString("zh-CN", {month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit"}) : ""; }
const completionFormatter = new Intl.DateTimeFormat("zh-CN", {
  year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit",
  hourCycle: "h23",
});

function preciseTime(parent, iso, label) {
  if (!iso) return;
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return;
  node("time", [label, completionFormatter.format(date)].filter(Boolean).join(" "), parent).dateTime = iso;
}

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
  const section = ["chat", "workspace", "tools"].includes(active.view) ? active.view : "issues";
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
    (!query || [issueTitle(issue), issue.prompt, issue.id, issueKey(issue.id)]
      .some(value => value.toLowerCase().includes(query))));
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
      node("span", issueTitle(item), row, "title-cell");
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
  if (detail.project) property("项目", detail.project.name);
}

function feedItem(parent, heading, metadata, body, {timestamp, timeLabel, elapsedSeconds} = {}) {
  const item = node("article", "", parent, "feed-item");
  const head = node("div", "", item, "feed-head");
  node("strong", heading, head);
  if (metadata) node("small", metadata, head);
  preciseTime(head, timestamp, timeLabel);
  if (Number.isInteger(elapsedSeconds) && elapsedSeconds >= 0) {
    const minutes = Math.floor(elapsedSeconds / 60);
    const seconds = String(elapsedSeconds % 60).padStart(2, "0");
    node("small", `用时 ${minutes} 分 ${seconds} 秒`, head);
  }
  if (body) node("p", body, item, "feed-content");
  return item;
}

function issueEventText(event) {
  const {payload} = event;
  if (runToolEvents.has(event.event_type)) {
    return [payload.name, toolStatus[payload.status]].filter(Boolean).join(" · ");
  }
  return payload.reason || (typeof payload.result === "string" ? payload.result : payload.result?.message);
}

function updateReplyPreview(preview, control) {
  const expanded = control.getAttribute("aria-expanded") === "true";
  const clipped = !expanded && preview.scrollHeight > preview.clientHeight;
  control.hidden = !expanded && !clipped;
  preview.classList.toggle("reply-clipped", clipped);
  const bottom = preview.getBoundingClientRect().bottom;
  for (const link of preview.querySelectorAll("a[href]")) {
    link.inert = clipped && link.getBoundingClientRect().bottom > bottom;
  }
}

function discussionMessage(item, replies) {
  const human = item.sender === "human";
  const title = {group: "群聊", comment: "评论", agent: "Agent 消息", run_error: "运行未完成"}[item.kind];
  const card = feedItem(null, human ? "你" : item.sender,
    [title, human ? when(item.created_at) : ""].filter(Boolean).join(" · "), undefined,
    {timestamp: human ? null : item.created_at, timeLabel: item.kind === "group" ? "回复完成于" : "发布于",
      elapsedSeconds: item.elapsed_seconds});
  if (human || !item.content) {
    markdownMessage(card, item.content, "feed-content");
    return card;
  }

  const preview = node("div", null, card, "reply-preview");
  preview.id = "reply-" + item.id;
  const body = markdownMessage(preview, item.content, "feed-content");
  const toggle = button(card, "展开回复", control => {
    const expanded = preview.classList.toggle("reply-expanded");
    control.textContent = expanded ? "收起回复" : "展开回复";
    control.setAttribute("aria-expanded", String(expanded));
    updateReplyPreview(preview, control);
    if (!expanded) control.scrollIntoView({block: "nearest"});
  }, "quiet reply-toggle");
  toggle.setAttribute("aria-controls", preview.id);
  toggle.setAttribute("aria-expanded", "false");
  toggle.hidden = true;
  replies.observe(body);
  return card;
}

function discussionProgressRow(run) {
  const details = node("details", null, null, "discussion-progress");
  const summary = node("summary", null, details);
  const spinner = node("span", null, summary, "run-spinner");
  spinner.setAttribute("aria-hidden", "true");
  node("span", "Agent 进度", summary);
  const status = node("span", null, summary, "progress-status");
  const latest = node("span", null, summary, "progress-latest");
  const empty = node("p", "本轮暂无进度记录", details);
  const history = node("ol", null, details, "progress-history");
  history.tabIndex = 0;
  history.setAttribute("role", "list");
  history.setAttribute("aria-label", `${run.agent} 的本轮进度`);
  return {details, spinner, status, latest, empty, history, latestText: "", cursor: 0};
}

function updateDiscussionProgress({container, rows}, detail) {
  const runs = detail.executions || [];
  const running = runs.filter(run => run.status === "running" || run.status === "stopping");
  const visibleRuns = running.length ? running : runs.filter(run => run.started_at).slice(0, 1);
  const visibleIds = new Set(visibleRuns.map(run => run.id));
  for (const [id, row] of rows) {
    if (!visibleIds.has(id)) {
      row.details.remove();
      rows.delete(id);
    }
  }
  let next = container.firstChild;
  for (const run of visibleRuns) {
    let row = rows.get(run.id);
    if (!row) {
      row = discussionProgressRow(run);
      rows.set(run.id, row);
    }
    if (row.details === next) next = row.details.nextSibling;
    else container.insertBefore(row.details, next);
  }

  for (const event of detail.logs) {
    const row = rows.get(event.task_id);
    if (!row || event.id <= row.cursor || !runProgressEvents.has(event.event_type)) continue;
    const text = issueEventText(event)?.trim();
    if (!text) continue;
    const entry = node("li", null);
    const label = node("div", null, entry, "progress-meta");
    node("span", event.actor, label);
    preciseTime(label, event.created_at);
    node("p", text, entry);
    row.history.prepend(entry);
    row.cursor = event.id;
    row.latestText = text;
  }
  const hasRunning = running.length > 0;
  for (const run of visibleRuns) {
    const row = rows.get(run.id);
    const status = `${run.agent} · ${runNames[run.status]}`;
    if (row.status.textContent !== status) row.status.textContent = status;
    if (row.latest.textContent !== row.latestText) row.latest.textContent = row.latestText;
    row.latest.hidden = !hasRunning || !row.latestText;
    row.spinner.hidden = !hasRunning;
    row.history.hidden = !row.cursor;
    row.empty.hidden = Boolean(row.cursor);
  }
  container.hidden = !visibleRuns.length;
  return hasRunning;
}

function updateDiscussion(detail) {
  const {feed, placeholder, cards, replies, progress} = discussionView;
  let next = placeholder.nextSibling;
  for (const item of detail.messages) {
    let card = cards.get(item.id);
    if (!card) {
      card = discussionMessage(item, replies);
      cards.set(item.id, card);
    }
    if (card === next) next = card.nextSibling;
    else feed.insertBefore(card, next);
  }

  const hasRunning = updateDiscussionProgress(progress, detail);
  feed.hidden = !detail.messages.length && hasRunning;
  placeholder.hidden = Boolean(detail.messages.length);
}

function discussion(panel, detail) {
  if (detail.source_group) {
    const source = node("p", "关联群聊 · ", panel, "muted discussion-source");
    const {id, name} = detail.source_group;
    link(source, id ? name : defaultGroupName, workspaceChatUrl(id ? groupChatUrl(id) : chatUrl("group")));
  } else if (state.workspace.capabilities?.["link-group"]) {
    const source = node("div", "", panel, "discussion-source");
    button(source, "关联群聊", control => openLinkGroup(control, detail.issue.id));
  }
  const feed = node("div", "", panel, "feed");
  const replies = new ResizeObserver(entries => {
    for (const {target} of entries) {
      const preview = target.parentElement;
      updateReplyPreview(preview, preview.nextElementSibling);
    }
  });
  discussionView = {feed, placeholder: node("p", "还没有讨论", feed, "empty"), cards: new Map(), replies,
    progress: {container: node("div", null, panel, "discussion-progress-list"), rows: new Map()}};
  updateDiscussion(detail);
  const form = node("form", "", panel, "stack");
  const message = field(form, "工单讨论", "textarea", "message");
  message.required = true;
  message.maxLength = 8000;
  const issueRuns = state.workspace.capabilities?.issue_runs;
  message.placeholder = issueRuns ? "留言。用 @Agent 并选择「评论并唤醒」可启动运行。" : "留言";
  if (issueRuns) {
    const mentions = node("div", "", form, "form-actions");
    for (const agent of state.workspace.agents) button(mentions, "@" + agent, () => {
      const prefix = message.value && !/\s$/.test(message.value) ? " " : "";
      message.value += prefix + "@" + agent + " ";
      message.focus();
    }, "quiet");
  }
  const actions = node("div", "", form, "form-actions");
  const post = formButton(actions, "发表评论", "comment", "primary");
  if (issueRuns) formButton(actions, "评论并唤醒", "notify");
  formButton(actions, "发送工单群聊", "group");
  form.onsubmit = event => {
    event.preventDefault();
    const submitter = event.submitter || post;
    const group = submitter.value === "group";
    const payload = {task_id: detail.issue.id, message: message.value};
    if (issueRuns && !group) payload.notify = submitter.value === "notify";
    void mutate(submitter, group ? "group" : "comment", payload,
      group ? "群聊已保存" : "评论已保存", "discussion");
  };
}

function logs(panel, detail) {
  const executions = node("div", "", panel, "feed");
  for (const run of detail.executions || []) {
    const card = feedItem(executions, `${run.agent} · ${runNames[run.status]}`,
      [runKinds[run.kind], when(run.created_at), run.retry_of ? "重试" : ""].filter(Boolean).join(" · "), undefined,
      {timestamp: run.finished_at, timeLabel: "运行结束于"});
    if (run.output_dir && run.started_at) node("code", run.output_dir, card, "project-path");
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
    const item = feedItem(feed, eventNames[event.event_type] || event.event_type,
      [event.actor, issueKey(event.task_id), when(event.created_at)].filter(Boolean).join(" · "),
      issueEventText(event));
    const expand = node("details", "", item);
    const truncated = payload.output_truncated || payload.outcome?.output_truncated;
    node("summary", truncated ? "查看工具输出预览（已截断）" : "查看事件详情", expand);
    node("pre", payload.output ?? JSON.stringify(payload, null, 2), expand, "output");
  }
}

function artifacts(panel, detail) {
  const feed = node("div", "", panel, "feed");
  if (!detail.artifacts.length) {
    empty(feed, "暂无可预览产物");
    for (const run of (detail.executions || []).filter(run => run.started_at && run.output_dir)) {
      node("code", run.output_dir, feed, "project-path");
    }
  }
  for (const artifact of detail.artifacts) {
    const item = feedItem(feed, artifact.path, [artifact.owner, issueKey(artifact.task_id),
      artifact.verified ? "已验证" : "已共享"].filter(Boolean).join(" · "));
    const output = (detail.executions || []).find(run => run.id === artifact.task_id)?.output_dir;
    if (output) node("code", output + "/" + artifact.path, item, "project-path");
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
function clearDiscussion() {
  discussionView?.replies.disconnect();
  discussionView = null;
}

function renderActivity() {
  const detail = state.detail;
  if (!detail) return;
  const tabs = el("issue-tabs");
  const panel = el("issue-panel");
  clearDiscussion();
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

function renderIssueHeading(detail) {
  const {issue} = detail;
  const title = issueTitle(issue);
  const item = el("issue-heading");
  item.replaceChildren();
  const metadata = node("div", "", item, "issue-meta");
  node("span", issueKey(issue.id), metadata, "issue-key");
  lane(metadata, detail.lane);
  node("h1", title, item);
  const description = issue.title ? issue.prompt : issue.prompt.slice(title.length).trim();
  if (description) node("p", description, item);
}

function renderDetail(detail) {
  renderIssueHeading(detail);
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

function projectPath(id) {
  return "/p/" + encodeURIComponent(id) + "/";
}

function projectUrl(id, hash = "#issues") {
  return projectPath(id) + hash;
}

function workspaceUrl(id) {
  return "/w/" + encodeURIComponent(id) + "/";
}

function currentWorkspaceId() {
  return workspaceId || state.workspace?.workspace?.id;
}

function workspaceChatUrl(hash = "#chat") {
  const id = currentWorkspaceId();
  return projectMode && (id || !projectId) ? workspaceUrl(id || "default") + hash : hash;
}

function setCurrent(item, current, value = "page") {
  if (current) item.setAttribute("aria-current", value);
  else item.removeAttribute("aria-current");
}

function renderPrimaryNavigation(active) {
  const currentWorkspace = currentWorkspaceId();
  const projectLink = el("project-home-link");
  projectLink.hidden = !currentWorkspace;
  if (currentWorkspace) projectLink.href = workspaceUrl(currentWorkspace);
  const section = ["chat", "workspace", "tools"].includes(active.view) ? active.view : "issues";
  setCurrent(projectLink, section === "issues");
  for (const item of document.querySelectorAll(".nav [data-route]")) {
    const kind = item.dataset.route;
    item.hidden = projectMode && kind !== "chat" && (kind === "issues" ||
      (kind === "workspace" && !currentWorkspace) || (kind === "tools" && !projectId));
    if (kind === "chat") item.href = workspaceChatUrl();
    setCurrent(item, kind === section);
  }
}

const projectBranches = new Map();

function setProjectIssueSelection(list, id) {
  for (const entry of list.querySelectorAll("a")) setCurrent(entry, entry.dataset.issueId === id);
}

function projectTreeStatus(list, message) {
  list.replaceChildren();
  node("li", message, list, "project-tree-status");
}

async function loadProjectIssues(project, branch) {
  if (branch.list.hidden) return;
  const request = ++branch.request;
  if (!branch.issues) projectTreeStatus(branch.list, "正在读取工单");
  branch.list.setAttribute("aria-busy", "true");
  try {
    const response = await fetch(projectPath(project.id) + "api/issue-index");
    if (!response.ok) throw Error("无法读取工单");
    const {issues} = await response.json();
    if (request !== branch.request) return;
    const signature = JSON.stringify(issues);
    if (signature !== branch.issues) {
      branch.issues = signature;
      branch.list.replaceChildren();
      for (const issue of issues) {
        const entry = link(node("li", null, branch.list), issue.title, projectUrl(project.id, issueUrl(issue.id)));
        entry.dataset.issueId = issue.id;
        entry.title = issue.title;
      }
      if (!issues.length) projectTreeStatus(branch.list, "暂无工单");
    }
    const active = route();
    setProjectIssueSelection(branch.list, project.id === projectId && active.view === "issue" ? active.key : undefined);
  } catch (error) {
    if (request === branch.request && !branch.issues) projectTreeStatus(branch.list, error.message);
  } finally {
    if (request === branch.request) branch.list.removeAttribute("aria-busy");
  }
}

function addProjectBranch(project) {
  const item = node("li", null, el("project-tree"));
  const row = node("div", null, item, "project-tree-row");
  const toggle = button(row, null, control => {
    branch.list.hidden = !branch.list.hidden;
    control.setAttribute("aria-expanded", String(!branch.list.hidden));
    void loadProjectIssues(project, branch);
  }, "quiet nav-button");
  node("span", null, toggle, "tree-chevron").setAttribute("aria-hidden", "true");
  const name = link(row, project.name, projectUrl(project.id));
  name.id = "project-name-" + project.id;
  name.title = project.name;
  setCurrent(name, project.id === projectId, "true");
  toggle.setAttribute("aria-label", project.name + "的工单");
  const create = button(row, null, () => openCreateIssue(project), "quiet nav-button");
  create.title = "新建工单";
  create.setAttribute("aria-label", "新建工单");
  create.setAttribute("aria-describedby", name.id);
  node("span", null, create, "plus-icon").setAttribute("aria-hidden", "true");
  const list = node("ul", null, item);
  list.id = "project-issues-" + project.id;
  list.hidden = project.id !== projectId;
  toggle.setAttribute("aria-expanded", String(!list.hidden));
  toggle.setAttribute("aria-controls", list.id);
  const branch = {item, list, request: 0, issues: null};
  return branch;
}

async function loadProjects(id, generation, overview) {
  try {
    const response = await fetch(workspaceUrl(id) + "api/projects");
    if (!response.ok) throw Error("无法读取项目");
    const {projects} = await response.json();
    if (generation !== state.generation) return;
    if (overview) {
      const cards = el("project-cards");
      cards.replaceChildren();
      for (const project of projects) {
        const card = link(cards, "", projectUrl(project.id), "project-card card");
        node("strong", project.name, card);
        node("code", project.directory, card, "project-path");
      }
      if (!projects.length) empty(cards, "暂无项目");
    }
    const ids = new Set(projects.map(project => project.id));
    for (const [projectId, branch] of projectBranches) {
      if (ids.has(projectId)) continue;
      branch.item.remove();
      projectBranches.delete(projectId);
    }
    for (const project of projects) {
      if (!projectBranches.has(project.id)) projectBranches.set(project.id, addProjectBranch(project));
      void loadProjectIssues(project, projectBranches.get(project.id));
    }
  } catch (error) {
    if (generation === state.generation) notice(error.message, true);
  }
}

const workspaceMenu = el("workspace-menu");
const workspaceSwitch = el("workspace-switch");
let workspaceChoices = [];

function renderWorkspaceChoices() {
  const list = el("workspace-list");
  list.replaceChildren();
  const selected = currentWorkspaceId();
  const name = workspaceChoices.find(workspace => workspace.id === selected)?.name || "工作区";
  el("workspace-mark").textContent = Array.from(name)[0].toUpperCase();
  el("workspace-name").textContent = name;
  workspaceSwitch.title = name;
  for (const workspace of workspaceChoices) {
    const choice = link(list, "", workspaceUrl(workspace.id));
    choice.className = "workspace-choice";
    node("span", Array.from(workspace.name)[0].toUpperCase(), choice, "workspace-mark").setAttribute("aria-hidden", "true");
    node("span", workspace.name, choice);
    if (workspace.id === selected) {
      choice.setAttribute("aria-current", "true");
      node("span", "✓", choice).setAttribute("aria-hidden", "true");
    }
  }
}

async function loadWorkspaces() {
  if (!projectMode) return;
  try {
    const response = await fetch("/api/workspaces");
    if (!response.ok) throw Error("无法读取工作区");
    const {workspaces} = await response.json();
    workspaceChoices = workspaces;
    if (!projectId && !workspaceId) {
      const cards = el("project-cards");
      cards.replaceChildren();
      for (const workspace of workspaces) {
        const card = link(cards, "", workspaceUrl(workspace.id), "project-card card");
        node("strong", workspace.name, card);
      }
    }
    renderWorkspaceChoices();
    workspaceMenu.hidden = false;
  } catch {
    if (catalogView) notice("无法读取工作区", true);
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

function bindCatalogForm(kind, buttons, getBase, destination) {
  const modal = el(kind + "-dialog");
  const form = el(kind + "-form");
  const close = el("close-" + kind);
  const submit = form.querySelector("button[type=submit]");
  const feedback = kind + "-create-error";
  for (const id of buttons) el(id).onclick = () => {
    workspaceMenu.open = false;
    form.reset();
    configFeedback(feedback);
    modal.showModal();
  };
  close.onclick = () => modal.close();
  modal.oncancel = event => {
    if (submit.disabled) event.preventDefault();
  };
  form.onsubmit = async event => {
    event.preventDefault();
    if (submit.disabled) return;
    const payload = Object.fromEntries([...new FormData(form)].map(([name, value]) => [name, value.trim()]));
    submit.disabled = close.disabled = true;
    configFeedback(feedback);
    try {
      const result = await api("new-" + kind, payload, getBase());
      location.assign(destination(result[kind].id));
    } catch (error) {
      configFeedback(feedback, error.message, true);
      submit.disabled = close.disabled = false;
    }
  };
}

bindCatalogForm("workspace", ["new-workspace", "new-workspace-home"], () => "/api/", workspaceUrl);
bindCatalogForm("project", ["new-project"], () => workspaceUrl(currentWorkspaceId()) + "api/", projectUrl);

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

function renderWorkspace(tools = false) {
  const {agents, capabilities} = state.workspace;
  el("workspace-title").textContent = tools ? "项目工具" : "团队";
  for (const panel of document.querySelectorAll(".workspace-setting")) panel.hidden = tools;
  for (const panel of document.querySelectorAll("[data-project-tool]")) panel.hidden = !tools;
  if (!tools) {
    renderSquadSettings();
    renderAgentProfiles();
    configFeedback("squad-feedback");
    configFeedback("agent-feedback");
    return;
  }
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
  if (!human) {
    preciseTime(meta, message.created_at, "回复完成于");
  } else if (message.created_at) {
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
  const name = workspaceGroup ? defaultGroupName : (room?.name || chat?.agent || "聊天");
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
  const workspaceCatalog = catalogView && !workspaceId;
  const overview = catalogView && (workspaceCatalog || active.view === "projects");
  const detail = !catalogView && active.view === "issue";
  if (!detail || state.detail?.issue.id !== active.key) clearDiscussion();
  const tools = active.view === "tools";
  const settings = !workspaceCatalog && (active.view === "workspace" || tools);
  const chatting = !workspaceCatalog && active.view === "chat";
  const issueView = !catalogView && !settings && !chatting;
  const projectView = Boolean(projectId && issueView || workspaceId && overview);
  el("projects-view").hidden = !overview;
  el("projects-title").textContent = workspaceCatalog ? "工作区" : "项目";
  el("list-view").hidden = !issueView || detail;
  el("detail-view").hidden = !detail;
  el("workspace-view").hidden = !settings;
  el("chat-view").hidden = !chatting;
  el("breadcrumb").hidden = chatting;
  el("new-workspace-home").hidden = !workspaceCatalog;
  el("section-nav").hidden = settings || workspaceCatalog;
  el("chat-navigation").hidden = !chatting;
  el("project-navigation").hidden = !projectView;
  el("issue-navigation").hidden = !issueView;
  el("demo-issue-actions").hidden = projectMode;
  el("new-project").disabled = !currentWorkspaceId();
  const shellLayout = document.querySelector(".shell").classList;
  shellLayout.toggle("without-section", el("section-nav").hidden);
  shellLayout.toggle("is-chat", chatting);
  renderPrimaryNavigation(workspaceCatalog ? {view: "projects"} : active);
  for (const [id, branch] of projectBranches) {
    setProjectIssueSelection(branch.list, detail && id === projectId ? active.key : undefined);
  }
  if (chatting && state.chatKey !== active.key) {
    state.chatKey = active.key;
    state.messages = [];
    renderChat(active, true);
  }
  const crumb = el("breadcrumb");
  crumb.replaceChildren();
  if (overview) node("strong", el("projects-title").textContent, crumb);
  if (issueView) link(crumb, "工单", "#issues");
  if (detail) {
    node("span", "/", crumb).setAttribute("aria-hidden", "true");
    node("strong", issueKey(active.key), crumb);
  }
  if (settings) node("strong", tools ? "项目工具" : "团队", crumb);
  if (workspaceCatalog) return;
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
    if (chatting && projectId) {
      location.replace(workspaceChatUrl(location.hash));
      return;
    }
    renderPrimaryNavigation(active);
    if (summary.project) {
      const context = node("span", null);
      link(context, summary.project.name, projectUrl(projectId));
      node("span", " / ", context).setAttribute("aria-hidden", "true");
      crumb.prepend(context);
      renderWorkspaceChoices();
      el("new-project").disabled = !currentWorkspaceId();
    }
    if (projectView) void loadProjects(currentWorkspaceId(), generation, overview);
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
    } else if (settings) {
      renderWorkspace(tools);
    } else if (issueView) {
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
const createIssueSubmit = form.querySelector("button[type=submit]");
const closeCreate = el("close-create");
const linkGroupDialog = el("link-group-dialog");
const linkGroupForm = el("link-group-form");
const linkGroupSubmit = linkGroupForm.querySelector("button[type=submit]");
const closeLinkGroup = el("close-link-group");

async function loadIssueGroups(picker, hint, emptyLabel) {
  picker.replaceChildren();
  option(picker, emptyLabel, "");
  option(picker, defaultGroupName + "（转入待归档消息）", "default");
  hint.textContent = "自建群导入关联时已有的聊天记录。";
  picker.disabled = true;
  try {
    const {groups = []} = await api("chat-threads", {});
    for (const group of groups) option(picker, group.name, group.id);
  } catch {
    hint.textContent = "群聊列表加载失败。关闭窗口后可重新打开。";
  } finally {
    picker.disabled = false;
  }
}

function issueGroupPayload(group) {
  if (group === "default") return {from_group: true};
  return group ? {group_id: group} : {};
}

async function openLinkGroup(control, issueId) {
  if (linkGroupDialog.open || linkGroupForm.elements.group.disabled) return;
  linkGroupForm.dataset.issueId = issueId;
  configFeedback("link-group-error");
  control.disabled = linkGroupSubmit.disabled = true;
  const groups = loadIssueGroups(linkGroupForm.elements.group, el("link-group-hint"), "选择群聊");
  linkGroupDialog.showModal();
  try {
    await groups;
    if (linkGroupDialog.open) linkGroupForm.elements.group.focus();
  } finally {
    control.disabled = linkGroupSubmit.disabled = false;
  }
}

closeLinkGroup.onclick = () => linkGroupDialog.close();
linkGroupDialog.oncancel = event => {
  if (closeLinkGroup.disabled) event.preventDefault();
};
linkGroupForm.onsubmit = async event => {
  event.preventDefault();
  const group = linkGroupForm.elements.group.value;
  if (!group || linkGroupSubmit.disabled) return;
  const {issueId} = linkGroupForm.dataset;
  if (route().view !== "issue" || route().key !== issueId) {
    linkGroupDialog.close();
    return;
  }
  const before = location.hash;
  const draft = el("issue-panel").querySelector('textarea[name="message"]')?.value;
  closeLinkGroup.disabled = linkGroupSubmit.disabled = true;
  configFeedback("link-group-error");
  try {
    await api("link-group", {task_id: issueId, ...issueGroupPayload(group)});
    linkGroupDialog.close();
    if (before === location.hash) {
      state.tab = "discussion";
      await load();
      const message = el("issue-panel").querySelector('textarea[name="message"]');
      if (before === location.hash && draft && message) message.value = draft;
    }
  } catch (error) {
    configFeedback("link-group-error", error.message, true);
  } finally {
    closeLinkGroup.disabled = linkGroupSubmit.disabled = false;
  }
};

async function openCreateIssue(project) {
  if (dialog.open || form.elements.group.disabled) return;
  form.reset();
  form.dataset.projectId = project?.id || "";
  const context = el("issue-project");
  context.hidden = !project;
  context.textContent = project ? "项目 " + project.name : "";
  const select = form.elements.assignee;
  select.replaceChildren();
  option(select, "先不指派", "");
  if (state.workspace?.squad) option(select, "Squad · " + state.workspace.squad.name, "squad:" + state.workspace.squad.name);
  for (const agent of state.workspace?.agents || []) option(select, "Agent · " + agent, "agent:" + agent);
  createIssueSubmit.disabled = true;
  el("create-error").hidden = true;
  const groups = loadIssueGroups(form.elements.group, el("create-group-hint"), "不关联群聊");
  dialog.showModal();
  try {
    await groups;
  } finally {
    createIssueSubmit.disabled = false;
  }
}
el("new-issue").onclick = () => openCreateIssue();
closeCreate.onclick = () => dialog.close();
dialog.oncancel = event => {
  if (closeCreate.disabled) event.preventDefault();
};
form.onsubmit = async event => {
  event.preventDefault();
  if (createIssueSubmit.disabled) return;
  const {title, prompt, assignee, group, no_memory} = Object.fromEntries(new FormData(form));
  const target = form.dataset.projectId;
  const error = el("create-error");
  createIssueSubmit.disabled = closeCreate.disabled = true;
  error.hidden = true;
  try {
    const payload = {title, prompt, ...issueGroupPayload(group)};
    if (no_memory) payload.no_memory = true;
    if (assignee) {
      const [type, id] = assignee.split(":");
      payload[type] = id;
    }
    const result = await api("create", payload, target ? projectPath(target) + "api/" : apiBase);
    dialog.close();
    const issue = issueUrl(result.task.id);
    if (target && target !== projectId) location.assign(projectUrl(target, issue));
    else location.hash = issue;
  } catch (failure) {
    error.textContent = failure.message;
    error.hidden = false;
  } finally { createIssueSubmit.disabled = closeCreate.disabled = false; }
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
  if (polling || !state.workspace?.capabilities?.issue_runs || document.hidden || dialog.open || linkGroupDialog.open) return;
  const active = route();
  if (["workspace", "tools", "chat"].includes(active.view) || (active.view === "issue" && !state.detail)) return;
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
    if (discussionView) updateDiscussion(records);
    if (el("inspector").contains(document.activeElement)) return;
    if (discussionView) {
      state.detail = records;
      renderIssueHeading(records);
      renderInspector(records);
    } else if (el("issue-panel").contains(document.activeElement)) {
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
el("default-group-name").textContent = defaultGroupName;
if (projectMode) document.querySelector(".brand").href = "/";
void loadWorkspaces();
