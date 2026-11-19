/* Actual Chromium against the loopback workbench, with isolated SQLite and deterministic agents. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const {spawn} = require('node:child_process');
const {once} = require('node:events');
const {createInterface} = require('node:readline');
const {chromium} = require(process.env.EVO_PLAYWRIGHT_MODULE || 'playwright');

const root = path.resolve(__dirname, '../..');
const output = path.resolve(process.env.EVO_BROWSER_OUTPUT || path.join(root, '.evo/browser-acceptance'));
const state = fs.mkdtempSync(path.join(os.tmpdir(), 'evo-browser-'));
const python = process.env.EVO_TEST_PYTHON || 'python';
const env = {
  ...process.env, PYTHONIOENCODING: 'utf-8', EVO_HARNESS_ADAPTER: 'baseline',
  EVO_APPLICATION_FACTORY: 'workbench_fixture:create_application',
  PYTHONPATH: [path.join(__dirname), process.env.PYTHONPATH].filter(Boolean).join(path.delimiter),
};
let server;
let browser;

function start() {
  server = spawn(python, ['-m', 'evo', '--state', state, 'web', '--port', '0'], {cwd: root, env});
  let errors = '';
  server.stderr.on('data', data => {errors += data;});
  const reader = createInterface({input: server.stdout});
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(Error('Server startup timed out: ' + errors)), 15000);
    reader.on('line', line => {
      if (line.startsWith('Open http://127.0.0.1:')) {
        clearTimeout(timer);
        resolve(line.slice(5));
      }
    });
    server.once('exit', code => {
      clearTimeout(timer);
      reject(Error('Server exited ' + code + ': ' + errors));
    });
  });
}

async function stop() {
  if (server?.exitCode === null && server.signalCode === null) {
    const exited = once(server, 'exit');
    server.kill();
    await exited;
  }
}

async function post(page, operation, payload) {
  return page.evaluate(async ({operation, payload}) => {
    const token = document.querySelector('meta[name=csrf-token]').content;
    const response = await fetch('/api/' + operation, {method: 'POST',
      headers: {'Content-Type': 'application/json', 'X-CSRF-Token': token}, body: JSON.stringify(payload)});
    return {status: response.status, body: await response.json()};
  }, {operation, payload});
}

async function createIssue(page, title, assignee) {
  await page.getByRole('button', {name: '新建工单'}).click();
  const dialog = page.getByRole('dialog', {name: '新建工单'});
  await dialog.getByLabel('目标与要求').fill(title);
  if (assignee) await dialog.getByLabel('指派给').selectOption(assignee);
  await dialog.getByRole('button', {name: '创建工单', exact: true}).click();
  await page.getByRole('heading', {name: title, exact: true}).waitFor();
  return decodeURIComponent(new URL(page.url()).hash.split('/')[1]);
}

async function writeComment(panel, content) {
  await panel.getByLabel('工单讨论').fill(content);
  await panel.getByRole('button', {name: '发表评论'}).click();
  await panel.getByText(content, {exact: true}).waitFor();
}

async function openIssue(page, id, title, url) {
  if (url) await page.goto(url + '#issue/' + id);
  else await page.evaluate(id => {location.hash = '#issue/' + id;}, id);
  await page.getByRole('heading', {name: title, exact: true}).waitFor();
}

async function run() {
  fs.mkdirSync(output, {recursive: true});
  let url = await start();
  browser = await chromium.launch({headless: true, ...(process.env.EVO_BROWSER_EXECUTABLE ?
    {executablePath: process.env.EVO_BROWSER_EXECUTABLE} : {})});
  const page = await browser.newPage({viewport: {width: 1360, height: 900}});
  page.setDefaultTimeout(12000);
  const errors = [];
  const checks = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.goto(url);
  await page.locator('#notice').filter({hasText: 'SQLite'}).waitFor();

  const xss = '<img src=x onerror="window.__injected=true">';
  await createIssue(page, xss);
  assert.equal(await page.locator('img').count(), 0);
  assert.equal(await page.evaluate(() => window.__injected === true), false);
  checks.push('Untrusted issue titles remain plain text');

  const titleA = 'A · 复核样本报告';
  const titleB = 'B · 慢速工单';
  const a = await createIssue(page, titleA);
  const panel = page.locator('#issue-panel');
  const tabs = page.locator('#issue-tabs');
  const inspector = page.locator('#inspector');
  await writeComment(panel, '评论-A-ONLY');
  assert.equal((await post(page, 'task', {task_id: a})).body.executions.length, 0);
  await panel.getByLabel('工单讨论').fill('群聊-A-ONLY');
  await panel.getByRole('button', {name: '发送工单群聊'}).click();
  await panel.getByText('reviewer 回复 群聊-A-ONLY', {exact: true}).waitFor();
  await inspector.getByLabel('指派给').selectOption('squad:default');
  await inspector.getByRole('button', {name: '确认指派'}).click();
  await inspector.getByRole('link', {name: 'Squad · default'}).waitFor();
  await panel.getByText('已完成本轮执行', {exact: true}).waitFor();
  await page.locator('#issue-heading').getByText('待审查', {exact: true}).waitFor();
  let details = (await post(page, 'task', {task_id: a})).body;
  assert.deepEqual(details.executions.map(run => [run.kind, run.agent, run.status]), [
    ['handoff', 'researcher', 'completed'], ['mention', 'reviewer', 'completed'], ['assignment', 'researcher', 'completed'],
  ]);
  assert.equal(details.issue.assignee_id, 'default');
  await tabs.getByRole('button', {name: '产物'}).click();
  await panel.getByRole('button', {name: '查看产物'}).click();
  await panel.getByText('verified local report', {exact: true}).waitFor();
  const artifact = details.artifacts[0];
  await inspector.getByLabel('状态').selectOption('done');
  await inspector.getByRole('button', {name: '更新状态'}).click();
  await page.locator('#issue-heading').getByText('已完成', {exact: true}).waitFor();
  checks.push('Assign a Squad, automatically run its leader, hand off via @mention and complete through issue status');
  await page.screenshot({path: path.join(output, 'detail.png'), fullPage: true});

  const b = await createIssue(page, titleB, 'agent:builder');
  await tabs.getByRole('button', {name: '运行记录'}).click();
  await panel.getByText('builder · 运行中', {exact: true}).waitFor();
  await panel.getByText('正在处理 builder', {exact: true}).waitFor();
  assert.equal((await post(page, 'artifact', {task_id: b, event_id: artifact.event_id})).status, 409);
  const running = (await post(page, 'task', {task_id: b})).body.executions[0];
  assert.equal((await post(page, 'stop-run', {task_id: a, run_id: running.id})).status, 409);
  await panel.getByRole('button', {name: '停止运行'}).click();
  await panel.getByText('builder · 已停止', {exact: true}).waitFor();
  await panel.getByRole('button', {name: '重试运行'}).click();
  await panel.getByText('builder · 已完成', {exact: true}).waitFor();
  details = (await post(page, 'task', {task_id: b})).body;
  assert.deepEqual(details.executions.map(run => [run.status, run.retry_of]), [['completed', running.id], ['stopped', null]]);
  checks.push('View live progress, stop a running job, and retry as a distinct attempt');

  await openIssue(page, a, titleA);
  await writeComment(panel, '@builder 请复查 @builder');
  await panel.getByText('已完成本轮执行', {exact: true}).last().waitFor();
  await tabs.getByRole('button', {name: '运行记录'}).click();
  await panel.getByText('builder · 已完成', {exact: true}).waitFor();
  details = (await post(page, 'task', {task_id: a})).body;
  assert.equal(details.executions.filter(run => run.kind === 'mention' && run.agent === 'builder').length, 1);
  assert.equal(details.issue.assignee_id, 'default');
  assert.equal(details.lane, 'done');
  assert.equal((await post(page, 'review', {task_id: a, decision: 'accepted'})).status, 404);
  assert.equal((await post(page, 'answer', {task_id: a, request_id: 'old'})).status, 404);
  checks.push('A human @mention creates one targeted run and preserves issue ownership and status');

  const oldToken = await page.locator('meta[name=csrf-token]').getAttribute('content');
  await stop();
  url = await start();
  await openIssue(page, a, titleA, url);
  assert.notEqual(await page.locator('meta[name=csrf-token]').getAttribute('content'), oldToken);
  await page.locator('#issue-heading').getByText('已完成', {exact: true}).waitFor();
  await panel.getByText('评论-A-ONLY', {exact: true}).waitFor();
  await openIssue(page, b, titleB);
  assert.doesNotMatch(await panel.innerText(), /评论-A-ONLY/);
  assert.equal((await post(page, 'task', {task_id: b})).body.executions[1].status, 'stopped');
  checks.push('Restart preserves issue state and run history, with comments scoped to the issue');

  const primary = page.getByRole('navigation', {name: '主导航'});
  const conversations = page.getByRole('navigation', {name: '私聊会话'});
  const transcript = page.getByRole('log', {name: '聊天记录'});
  await primary.getByRole('link', {name: '聊天', exact: true}).click();
  await page.getByRole('heading', {name: '工作区群聊', exact: true}).waitFor();
  await page.getByLabel('聊天消息').fill('工作区群聊留档');
  await page.getByRole('button', {name: '发送消息'}).click();
  await transcript.getByText('reviewer 回复 工作区群聊留档', {exact: true}).waitFor();
  assert.doesNotMatch(await transcript.innerText(), /群聊-A-ONLY/);
  await page.screenshot({path: path.join(output, 'chat-group.png')});
  checks.push('Open workspace group chat from primary navigation and keep issue discussions separate');

  const groups = page.getByRole('navigation', {name: '群聊', exact: true});
  const groupDialog = page.getByRole('dialog', {name: '新建群聊'});
  const newGroup = page.getByRole('button', {name: '新建群聊', exact: true});
  await newGroup.click();
  await groupDialog.getByLabel('群聊名称').fill('分析讨论');
  await groupDialog.getByRole('button', {name: '创建群聊'}).click();
  await groupDialog.getByRole('alert').filter({hasText: '请选择至少一个 Agent'}).waitFor();
  await groupDialog.getByRole('checkbox', {name: 'researcher'}).check();
  await groupDialog.getByRole('checkbox', {name: 'builder'}).check();
  await page.screenshot({path: path.join(output, 'group-picker.png')});
  await groupDialog.getByRole('button', {name: '创建群聊'}).click();
  await page.getByRole('heading', {name: '分析讨论', exact: true}).waitFor();
  await page.getByLabel('聊天消息').fill('分析群历史');
  await page.getByRole('button', {name: '发送消息'}).click();
  await transcript.getByText('builder 回复 分析群历史', {exact: true}).waitFor();
  assert.doesNotMatch(await transcript.innerText(), /reviewer|工作区群聊留档/);
  await newGroup.click();
  await groupDialog.getByLabel('群聊名称').fill('交付复核');
  await groupDialog.getByRole('checkbox', {name: 'builder'}).check();
  await groupDialog.getByRole('checkbox', {name: 'reviewer'}).check();
  await groupDialog.getByRole('button', {name: '创建群聊'}).click();
  await page.getByRole('heading', {name: '交付复核', exact: true}).waitFor();
  await page.getByLabel('聊天消息').fill('交付群历史');
  await page.getByRole('button', {name: '发送消息'}).click();
  await transcript.getByText('reviewer 回复 交付群历史', {exact: true}).waitFor();
  assert.doesNotMatch(await transcript.innerText(), /researcher|分析群历史|工作区群聊留档/);
  await groups.getByRole('link', {name: '分析讨论', exact: true}).click();
  await transcript.getByText('builder 回复 分析群历史', {exact: true}).waitFor();
  await page.reload();
  await transcript.getByText('builder 回复 分析群历史', {exact: true}).waitFor();
  await page.screenshot({path: path.join(output, 'group-conversation.png')});
  checks.push('Create named groups with different members and reload their independent chat histories');

  const newChat = page.getByRole('button', {name: '新建聊天', exact: true});
  const agentPicker = page.getByRole('dialog', {name: '选择聊天 Agent'});
  await newChat.click();
  await page.screenshot({path: path.join(output, 'chat-picker.png')});
  await agentPicker.getByRole('button', {name: /^builder/}).click();
  await page.getByRole('heading', {name: 'builder', exact: true}).waitFor();
  await page.getByLabel('聊天消息').fill('工作区私聊');
  await page.getByRole('button', {name: '发送消息'}).click();
  await transcript.getByText('builder 回复 工作区私聊', {exact: true}).waitFor();
  const firstChat = page.url();
  await newChat.click();
  await agentPicker.getByRole('button', {name: /^builder/}).click();
  await transcript.getByText('向 builder 发送第一条消息', {exact: true}).waitFor();
  assert.notEqual(page.url(), firstChat);
  await page.getByLabel('聊天消息').fill('另一个私聊');
  await page.getByRole('button', {name: '发送消息'}).click();
  await transcript.getByText('builder 回复 另一个私聊', {exact: true}).waitFor();
  assert.doesNotMatch(await transcript.innerText(), /工作区私聊|工作区群聊留档/);
  await conversations.getByRole('link', {name: /^工作区私聊/}).click();
  await transcript.getByText('builder 回复 工作区私聊', {exact: true}).waitFor();
  await page.reload();
  await transcript.getByText('builder 回复 工作区私聊', {exact: true}).waitFor();
  await page.screenshot({path: path.join(output, 'chat-private.png')});
  await page.getByRole('link', {name: '工作区群聊', exact: true}).click();
  await transcript.getByText('reviewer 回复 工作区群聊留档', {exact: true}).waitFor();
  assert.doesNotMatch(await transcript.innerText(), /工作区私聊|另一个私聊/);
  checks.push('Choose an agent from the chat submenu, create independent sessions, switch and reload');

  await primary.getByRole('link', {name: '工作区', exact: true}).click();
  await page.getByRole('heading', {name: '工作区', exact: true}).waitFor();
  const settings = page.getByRole('region', {name: '小队配置'});
  const configInput = settings.getByLabel('小队名称');
  const lead = settings.getByLabel('队长', {exact: true});
  const reviewer = settings.getByRole('checkbox', {name: 'reviewer'});
  const saveConfig = settings.getByRole('button', {name: '保存小队配置'});
  assert.equal(await configInput.inputValue(), 'default');
  await configInput.fill('../小队1');
  await saveConfig.click();
  await settings.getByRole('status').filter({hasText: /小队名称须为/}).waitFor();
  assert.equal(await configInput.inputValue(), '../小队1');
  await configInput.fill('小队1');
  await reviewer.uncheck();
  await lead.selectOption('reviewer');
  assert.equal(await reviewer.isChecked(), true);
  assert.equal(await reviewer.isDisabled(), true);
  await settings.getByRole('checkbox', {name: 'researcher'}).uncheck();

  const profiles = page.getByRole('region', {name: 'Agent 专长'});
  const builderEdit = profiles.getByRole('button', {name: '编辑 builder 的专长'});
  const editDialog = page.getByRole('dialog', {name: '编辑 builder 的专长'});
  const specialty = '编写和维护测试文档';
  await builderEdit.click();
  await editDialog.getByLabel('专长').fill(specialty);
  await page.screenshot({path: path.join(output, 'workspace-agent-edit.png')});
  await editDialog.getByRole('button', {name: '保存专长'}).click();
  await profiles.getByRole('status').filter({hasText: 'builder 的专长已保存'}).waitFor();
  await profiles.getByText(specialty, {exact: true}).waitFor();
  assert.deepEqual(await page.evaluate(() => fetch('/api/state').then(response => response.json()).then(state => state.squad)),
    {name: 'default', lead: 'researcher', members: ['researcher', 'builder', 'reviewer']});
  assert.equal(await configInput.inputValue(), '小队1');
  assert.equal(await lead.inputValue(), 'reviewer');

  await saveConfig.click();
  await settings.getByRole('status').filter({hasText: '小队配置已保存'}).waitFor();
  await page.reload();
  await profiles.getByText(specialty, {exact: true}).waitFor();
  assert.equal(await configInput.inputValue(), '小队1');
  assert.equal(await lead.inputValue(), 'reviewer');
  assert.equal(await reviewer.isChecked(), true);
  assert.equal(await settings.getByRole('checkbox', {name: 'researcher'}).isChecked(), false);
  await page.screenshot({path: path.join(output, 'workspace-settings.png')});
  checks.push('Select lead and members and save one agent specialty independently of squad edits');

  await openIssue(page, a, titleA);
  await inspector.getByRole('link', {name: 'Squad · 小队1', exact: true}).click();
  await page.getByRole('heading', {name: '小队1 的工单', exact: true}).waitFor();
  await page.getByRole('link', {name: new RegExp(titleA)}).waitFor();
  await page.evaluate(() => {location.hash = '#owner/squad/default';});
  await page.waitForURL(url => decodeURIComponent(url.hash) === '#owner/squad/小队1');
  await page.getByRole('link', {name: new RegExp(titleA)}).waitFor();
  checks.push('Save a Chinese squad name, preserve existing issues and redirect the old squad bookmark');

  await primary.getByRole('link', {name: '工作区', exact: true}).click();
  await page.getByRole('heading', {name: '工作区', exact: true}).waitFor();
  const dataset = path.join(state, 'memory-case.jsonl');
  const comparison = path.join(state, 'reports', 'memory-comparison.json');
  fs.writeFileSync(dataset, JSON.stringify({id: 'test', prompt: 'Write the report',
    subtask_checks: {s1: {path: 'report.md', contains: 'review deliverable'}}}) + '\n');
  const evaluation = page.locator('details').filter({hasText: 'JSONL 评测'});
  await evaluation.locator('summary').click();
  await evaluation.getByLabel('本地 JSONL 路径').fill(dataset);
  await evaluation.getByLabel('报告路径').fill(comparison);
  await evaluation.getByLabel('记忆策略').selectOption('compare');
  await evaluation.getByRole('button', {name: '运行评测'}).click();
  const reportView = evaluation.locator('#eval-result');
  await reportView.waitFor();
  const report = JSON.parse(await reportView.textContent());
  assert.deepEqual(report, JSON.parse(fs.readFileSync(comparison, 'utf-8')));
  assert.equal(report.memory_on.memory_enabled, true);
  assert.equal(report.memory_off.memory_enabled, false);
  assert.deepEqual(report.memory_on.failure_categories, {approval_denied: 1});
  assert.deepEqual(report.memory_off.failure_categories, {approval_denied: 1});
  checks.push('Web memory comparison writes and displays both arms under the same approval policy');

  await openIssue(page, b, titleB);
  assert.doesNotMatch(await panel.innerText(), /工作区私聊/);
  checks.push('Workspace private messages remain outside issue discussion');

  await page.setViewportSize({width: 390, height: 844});
  await page.screenshot({path: path.join(output, 'mobile.png'), fullPage: true});
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
  await primary.getByRole('link', {name: '工作区', exact: true}).click();
  await profiles.getByText(specialty, {exact: true}).waitFor();
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
  const nameBox = await configInput.boundingBox();
  const leadBox = await lead.boundingBox();
  assert.ok(nameBox.x + nameBox.width < leadBox.x);
  await evaluation.locator('summary').click();
  await page.screenshot({path: path.join(output, 'workspace-mobile.png'), fullPage: true});
  await primary.getByRole('link', {name: '聊天', exact: true}).click();
  await conversations.getByRole('link', {name: /^另一个私聊/}).click();
  await transcript.getByText('builder 回复 另一个私聊', {exact: true}).waitFor();
  assert.equal(await newChat.isVisible(), true);
  assert.equal(await newGroup.isVisible(), true);
  assert.equal(await page.evaluate(() => {
    const composer = document.getElementById('chat-form').getBoundingClientRect();
    return document.documentElement.scrollWidth <= innerWidth && composer.bottom <= innerHeight;
  }), true);
  await page.screenshot({path: path.join(output, 'chat-mobile.png')});
  assert.deepEqual(errors, []);
  checks.push('Mobile workbench and chat composer fit the viewport and emit no JavaScript errors');
  fs.writeFileSync(path.join(output, 'report.json'),
    JSON.stringify({status: 'passed', browser: browser.version(), checks, fixture: 'scripted provider and harness'}, null, 2));
  console.log(JSON.stringify({status: 'passed', checks: checks.length, output}));
}

run().catch(error => { console.error(error); process.exitCode = 1; }).finally(async () => {
  await stop();
  if (browser) await browser.close();
  fs.rmSync(state, {recursive: true, force: true});
});
