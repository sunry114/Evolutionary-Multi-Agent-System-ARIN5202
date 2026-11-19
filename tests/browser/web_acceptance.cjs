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

async function createIssue(page, title, checks = false, noMemory = false) {
  await page.getByRole('button', {name: '新建工单'}).click();
  const dialog = page.getByRole('dialog', {name: '新建工单'});
  await dialog.getByLabel('目标与要求').fill(title);
  if (checks) {
    await dialog.getByLabel('验收文件').fill('report.md');
    await dialog.getByLabel('文件应包含').fill('review deliverable');
  }
  if (noMemory) await dialog.getByLabel('关闭本工单的个人记忆').check();
  await dialog.getByRole('button', {name: '创建工单', exact: true}).click();
  await page.getByRole('heading', {name: title, exact: true}).waitFor();
  return decodeURIComponent(new URL(page.url()).hash.split('/')[1]);
}

async function commentAndChat(page, letters) {
  const panel = page.locator('#issue-panel');
  await panel.getByLabel('工单讨论').fill('评论-' + letters);
  await panel.getByRole('button', {name: '发表评论'}).click();
  await panel.getByText('评论-' + letters, {exact: true}).waitFor();
  await panel.getByLabel('工单讨论').fill('群聊-' + letters);
  await panel.getByRole('button', {name: '发送工单群聊'}).click();
  await panel.getByText('reviewer 回复 群聊-' + letters, {exact: true}).waitFor();
}

async function assignAndStart(page) {
  const inspector = page.locator('#inspector');
  await inspector.getByLabel('指派给').selectOption('squad:default');
  await inspector.getByRole('button', {name: '确认指派'}).click();
  await inspector.getByRole('link', {name: 'Squad · default'}).waitFor();
  await inspector.getByRole('button', {name: '启动工单'}).click();
  await inspector.getByRole('button', {name: '处理审批或澄清'}).waitFor();
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
  const titleB = 'B · 独立工单';
  const a = await createIssue(page, titleA, true, true);
  await commentAndChat(page, 'A-ONLY');
  await assignAndStart(page);
  const {events} = (await post(page, 'trace', {task_id: a})).body;
  assert.equal(events.find(event => event.event_type === 'run_config').payload.memory_enabled, false);
  const panel = page.locator('#issue-panel');
  const tabs = page.locator('#issue-tabs');
  const inspector = page.locator('#inspector');
  await tabs.getByRole('button', {name: '分派协商'}).click();
  await panel.getByText('由复核员独立核对证据', {exact: true}).waitFor();
  await panel.getByText('提名 · researcher → reviewer', {exact: true}).waitFor();
  await panel.getByText(/最终职责.*附上独立证据/s).waitFor();
  await page.screenshot({path: path.join(output, 'detail.png'), fullPage: true});
  await tabs.getByRole('button', {name: '运行记录'}).click();
  await panel.getByText('发起人工请求', {exact: true}).waitFor();
  checks.push('Create, assign to Squad, start, inspect reassignment rationale and scoped run log');

  const b = await createIssue(page, titleB, true);
  await commentAndChat(page, 'B-ONLY');
  await assignAndStart(page);
  const detailsA = (await post(page, 'task', {task_id: a})).body;
  const detailsB = (await post(page, 'task', {task_id: b})).body;
  assert.equal(detailsA.approvals.length, 1);
  assert.equal(detailsB.approvals.length, 1);
  const requestA = detailsA.approvals[0];
  const requestB = detailsB.approvals[0];
  assert.notEqual(requestA.id, requestB.id);
  assert.equal((await post(page, 'answer', {task_id: a, request_id: requestB.id, approved: true})).status, 409);

  await openIssue(page, a, titleA);
  await panel.getByText('评论-A-ONLY', {exact: true}).waitFor();
  assert.doesNotMatch(await panel.innerText(), /B-ONLY/);
  await inspector.getByRole('button', {name: '处理审批或澄清'}).click();
  await panel.getByText(requestA.payload.arguments.path, {exact: false}).waitFor();
  assert.doesNotMatch(await panel.innerText(), new RegExp(requestB.payload.arguments.path));
  await panel.getByRole('button', {name: '批准', exact: true}).click();
  await inspector.getByRole('button', {name: '继续执行'}).click();
  await inspector.getByRole('button', {name: '验收交付'}).waitFor();
  assert.equal((await post(page, 'task', {task_id: b})).body.approvals[0].status, 'pending');
  checks.push('Issue discussions and approvals are isolated, including forged cross-issue answers');

  await tabs.getByRole('button', {name: '产物'}).click();
  await panel.getByRole('button', {name: '查看产物'}).click();
  await panel.getByText('original review deliverable', {exact: true}).waitFor();
  const originalArtifact = (await post(page, 'task', {task_id: a})).body.artifacts[0];
  assert.equal((await post(page, 'artifact', {task_id: b, event_id: originalArtifact.event_id})).status, 409);

  await inspector.getByRole('button', {name: '验收交付'}).click();
  await panel.getByLabel('审查意见').fill('补充复核注释');
  await panel.getByRole('button', {name: '退回修改'}).click();
  await panel.getByLabel('修订要求').fill('保留原验收条件，补充注释');
  await panel.getByRole('button', {name: '创建修订'}).click();
  await inspector.getByRole('button', {name: '启动修订'}).click();
  await inspector.getByRole('button', {name: '验收交付'}).waitFor();
  assert.equal(decodeURIComponent(new URL(page.url()).hash.split('/')[1]), a);
  await tabs.getByRole('button', {name: '产物'}).click();
  const previews = panel.getByRole('button', {name: '查看产物'});
  assert.equal(await previews.count(), 2);
  await previews.nth(0).click();
  await previews.nth(1).click();
  await panel.getByText('original review deliverable', {exact: true}).waitFor();
  await panel.getByText('revised review deliverable', {exact: true}).waitFor();
  checks.push('Original and revised artifact snapshots remain available under one issue ID');

  await inspector.getByRole('button', {name: '验收交付'}).click();
  await panel.getByRole('button', {name: '接受交付'}).click();
  await inspector.getByText('交付已验收', {exact: true}).waitFor();
  assert.equal((await post(page, 'task', {task_id: a})).body.current.review_status, 'accepted');
  await page.getByRole('navigation', {name: '负责人', exact: true}).getByRole('link', {name: 'Agent reviewer'}).click();
  await page.getByRole('heading', {name: 'reviewer 的工单'}).waitFor();
  await page.locator('#issue-list').getByRole('link', {name: new RegExp(titleA)}).waitFor();
  assert.equal(await page.locator('#issue-list .issue-row').count(), 2);
  await page.getByRole('navigation', {name: '主导航'}).getByRole('link', {name: '工单列表'}).click();
  await page.getByRole('heading', {name: '工单', exact: true}).waitFor();
  await page.getByRole('searchbox', {name: '搜索工单'}).fill('复核样本');
  assert.equal(await page.locator('#issue-list .issue-row').count(), 1);
  checks.push('Accept the revised delivery and find it by execution owner or search');
  await page.getByRole('searchbox', {name: '搜索工单'}).fill('');
  await page.screenshot({path: path.join(output, 'desktop.png'), fullPage: true});

  const oldToken = await page.locator('meta[name=csrf-token]').getAttribute('content');
  await stop();
  url = await start();
  await openIssue(page, a, titleA, url);
  assert.notEqual(await page.locator('meta[name=csrf-token]').getAttribute('content'), oldToken);
  await inspector.getByText('交付已验收', {exact: true}).waitFor();
  await panel.getByText('评论-A-ONLY', {exact: true}).waitFor();
  await openIssue(page, b, titleB);
  await panel.getByText('评论-B-ONLY', {exact: true}).waitFor();
  assert.doesNotMatch(await panel.innerText(), /A-ONLY/);
  await inspector.getByRole('button', {name: '处理审批或澄清'}).waitFor();
  assert.equal((await post(page, 'task', {task_id: b})).body.approvals[0].id, requestB.id);
  checks.push('Server restart preserves deep links, discussion, revision review and the other pending approval');

  await page.getByRole('navigation', {name: '主导航'}).getByRole('link', {name: '工作区'}).click();
  await page.getByRole('heading', {name: '工作区', exact: true}).waitFor();
  const privateChat = page.locator('details').filter({hasText: '成员私聊'});
  await privateChat.locator('summary').click();
  await privateChat.getByLabel('私聊成员').selectOption('builder');
  await privateChat.getByLabel('私聊消息').fill('工作区私聊');
  await privateChat.getByRole('button', {name: '发送私聊'}).click();
  await privateChat.getByText('builder 回复 工作区私聊', {exact: true}).waitFor();
  await privateChat.getByLabel('私聊成员').selectOption('reviewer');
  assert.equal(await privateChat.getByText('工作区私聊', {exact: false}).count(), 0);

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
  await panel.getByText('评论-B-ONLY', {exact: true}).waitFor();
  assert.doesNotMatch(await panel.innerText(), /工作区私聊/);
  checks.push('Workspace private messages remain outside issue discussion');

  await page.setViewportSize({width: 390, height: 844});
  await page.screenshot({path: path.join(output, 'mobile.png'), fullPage: true});
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
  assert.deepEqual(errors, []);
  checks.push('Mobile workbench fits the viewport and emits no JavaScript errors');
  fs.writeFileSync(path.join(output, 'report.json'),
    JSON.stringify({status: 'passed', browser: browser.version(), checks, fixture: 'scripted provider and harness'}, null, 2));
  console.log(JSON.stringify({status: 'passed', checks: checks.length, output}));
}

run().catch(error => { console.error(error); process.exitCode = 1; }).finally(async () => {
  await stop();
  if (browser) await browser.close();
  fs.rmSync(state, {recursive: true, force: true});
});
