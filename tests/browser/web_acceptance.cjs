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
const projectDirectory = fs.mkdirSync(path.join(state, 'reports'), {recursive: true});
const secondDirectory = fs.mkdirSync(path.join(state, 'isolated'), {recursive: true});
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
    const response = await fetch(location.pathname + 'api/' + operation, {method: 'POST',
      headers: {'Content-Type': 'application/json', 'X-CSRF-Token': token}, body: JSON.stringify(payload)});
    return {status: response.status, body: await response.json()};
  }, {operation, payload});
}

async function createIssue(page, title, assignee, prompt = '核对资料并记录交付结果。') {
  await page.getByRole('button', {name: '新建工单'}).click();
  const dialog = page.getByRole('dialog', {name: '新建工单'});
  await dialog.getByLabel('主题').fill(title);
  await dialog.getByLabel('目标与要求').fill(prompt);
  if (assignee) await dialog.getByLabel('指派给').selectOption(assignee);
  await dialog.getByRole('button', {name: '创建工单', exact: true}).click();
  await page.getByRole('heading', {name: title, exact: true}).waitFor();
  return decodeURIComponent(new URL(page.url()).hash.split('/')[1]);
}

async function writeComment(panel, content, notify = false) {
  await panel.getByLabel('工单讨论').fill(content);
  await panel.getByRole('button', {name: notify ? '评论并唤醒' : '发表评论'}).click();
  await panel.getByText(content, {exact: true}).waitFor();
}

async function openIssue(page, id, title, url) {
  if (url) await page.goto(url + '#issue/' + id);
  else await page.evaluate(id => {location.hash = '#issue/' + id;}, id);
  await page.getByRole('heading', {name: title, exact: true}).waitFor();
}

async function checkMarkdown(body, rawHtml) {
  await body.getByRole('heading', {name: 'Markdown 验收'}).waitFor();
  assert.equal(await body.locator('strong').textContent(), '粗体');
  assert.equal(await body.locator('pre code').textContent(), '<b>代码文本</b>\n');
  assert.equal(await body.locator('blockquote').textContent().then(text => text.trim()), '引用文本');
  assert.equal(await body.getByRole('listitem').textContent(), '列表项');
  await body.getByRole('cell', {name: '通过'}).waitFor();
  assert.equal(await body.getByRole('cell', {name: '通过'}).evaluate(cell => getComputedStyle(cell).textAlign), 'right');
  const link = body.getByRole('link', {name: '正常链接'});
  assert.equal(await link.getAttribute('href'), 'https://example.com/notes');
  assert.equal(await link.getAttribute('rel'), 'noopener noreferrer');
  assert.equal(await body.getByRole('link', {name: '危险链接'}).count(), 0);
  assert.equal(await body.locator('img').count(), 0);
  await body.getByText(rawHtml, {exact: true}).waitFor();
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
  await page.getByRole('heading', {name: '项目', exact: true}).waitFor();
  assert.equal(await page.getByRole('button', {name: '新建工单'}).isVisible(), false);
  await page.getByRole('button', {name: '新建项目', exact: true}).click();
  const setup = page.getByRole('dialog', {name: '新建项目'});
  await setup.getByLabel('项目名称').fill('报告项目');
  await setup.getByLabel('本机目录').fill(projectDirectory);
  await setup.getByRole('button', {name: '创建项目'}).click();
  await page.locator('#workspace-name').getByText('报告项目', {exact: true}).waitFor();
  const projectPath = new URL(page.url()).pathname;
  await page.locator('#notice').filter({hasText: 'SQLite'}).waitFor();
  checks.push('Create a project from the overview and bind its existing local directory');

  const preview = await browser.newPage();
  try {
    const template = fs.readFileSync(path.join(root, 'src/evo/interfaces/web/assets/index.html'), 'utf8');
    await preview.setContent(template);
    const launch = preview.getByRole('link', {name: '打开本地工作台'});
    await launch.waitFor();
    assert.equal(await launch.getAttribute('href'), 'http://127.0.0.1:8769/');
    assert.equal(await preview.locator('.shell').isVisible(), false);
    assert.equal(await page.locator('#local-launch').isVisible(), false);
  } finally {
    await preview.close();
  }
  checks.push('Opening the source template shows a launch link instead of an unstyled workbench');

  const xss = '<img src=x onerror="window.__injected=true">';
  await createIssue(page, xss);
  assert.equal(await page.locator('img').count(), 0);
  assert.equal(await page.evaluate(() => window.__injected === true), false);
  checks.push('Untrusted issue titles remain plain text');
  const markdownSample = ['Markdown 格式样本', '', '## Markdown 验收', '', '**粗体** 和 `行内代码`', '',
    '> 引用文本', '', '- 列表项', '', '```html', '<b>代码文本</b>', '```', '',
    '| 项目 | 状态 |', '| --- | ---: |', '| 交付 | 通过 |', '',
    '[正常链接](https://example.com/notes) [危险链接](javascript:blocked)', '', xss, '',
    '![外部图片](https://example.com/image.png)'].join('\n');

  const titleA = 'A · 复核样本报告';
  const titleB = 'B · 慢速工单';
  const promptA = '核对样本的原始记录和汇总表。\n提交证据及差异说明。';
  const a = await createIssue(page, titleA, undefined, promptA);
  const description = page.locator('#issue-heading p');
  assert.equal(await description.textContent(), promptA);
  await page.getByRole('navigation', {name: '主导航'}).getByRole('link', {name: '工单', exact: true}).click();
  const issueTitles = page.locator('#issue-list .title-cell');
  await issueTitles.filter({hasText: titleA}).waitFor();
  const search = page.getByRole('searchbox', {name: '搜索工单'});
  for (const query of ['复核样本报告', '原始记录和汇总表']) {
    await search.fill(query);
    assert.deepEqual(await issueTitles.allTextContents(), [titleA]);
  }
  await search.clear();
  await page.locator('#issue-list').getByRole('link', {name: new RegExp(titleA)}).click();
  await page.getByRole('heading', {name: titleA, exact: true}).waitFor();
  checks.push('Create an issue with a separate title and full description, then find it by either field');
  const panel = page.locator('#issue-panel');
  const tabs = page.locator('#issue-tabs');
  const inspector = page.locator('#inspector');
  await writeComment(panel, '评论-A-ONLY');
  assert.equal((await post(page, 'task', {task_id: a})).body.executions.length, 0);
  await panel.getByLabel('工单讨论').fill('群聊-A-ONLY');
  await panel.getByRole('button', {name: '发送工单群聊'}).click();
  await panel.getByText('reviewer 回复 群聊-A-ONLY', {exact: true}).waitFor();
  await panel.getByLabel('工单讨论').fill(markdownSample);
  await panel.getByRole('button', {name: '发送工单群聊'}).click();
  await checkMarkdown(panel.locator('.markdown-body').filter({hasText: /reviewer 回复.*Markdown 格式样本/}), xss);
  assert.ok((await post(page, 'task', {task_id: a})).body.messages.some(message => message.content === markdownSample));
  checks.push('Issue replies render Markdown while stored message text is preserved');
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

  const b = await createIssue(page, titleB, 'agent:builder', '慢速工单测试。请生成并归档报告。');
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
  await panel.locator('.feed-content strong').filter({hasText: /^已完成本轮执行$/}).waitFor();
  await panel.getByText('查看工具输出预览（已截断）', {exact: true}).click();
  const toolPreview = await panel.locator('details[open] pre').textContent();
  assert.match(toolPreview, /^工具诊断样本/);
  assert.equal(Buffer.byteLength(toolPreview, 'utf-8'), 8192);
  details = (await post(page, 'task', {task_id: b})).body;
  assert.deepEqual(details.executions.map(run => [run.status, run.retry_of]), [['completed', running.id], ['stopped', null]]);
  checks.push('View live progress, stop a running job, and retry as a distinct attempt');

  const failedTitle = '模型连接测试';
  const failedIssue = await createIssue(page, failedTitle, 'agent:builder', '模拟模型连接中断');
  await page.locator('#issue-heading').getByText('已阻塞', {exact: true}).waitFor();
  await panel.getByText('API Error: 模型连接中断', {exact: true}).waitFor();
  await panel.getByText('Claude Code 错误末尾', {exact: true}).waitFor();
  await tabs.getByRole('button', {name: '运行记录'}).click();
  await panel.getByText('builder · 执行失败', {exact: true}).waitFor();
  await panel.getByText('Execution failed: cli_failed', {exact: true}).first().waitFor();
  await panel.getByText('Claude Code 错误末尾', {exact: true}).waitFor();
  await panel.getByText('Claude Code API retry: connection_error', {exact: true}).waitFor();
  checks.push('Show the full Claude Code error in discussion and execution logs while the issue remains blocked');

  await openIssue(page, a, titleA);
  await writeComment(panel, '@builder 请复查 @builder', true);
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
  await openIssue(page, a, titleA, url + projectPath);
  assert.equal(await description.textContent(), promptA);
  assert.notEqual(await page.locator('meta[name=csrf-token]').getAttribute('content'), oldToken);
  await page.locator('#issue-heading').getByText('已完成', {exact: true}).waitFor();
  await panel.getByText('评论-A-ONLY', {exact: true}).waitFor();
  await openIssue(page, b, titleB);
  assert.doesNotMatch(await panel.innerText(), /评论-A-ONLY/);
  assert.equal((await post(page, 'task', {task_id: b})).body.executions[1].status, 'stopped');
  await openIssue(page, failedIssue, failedTitle);
  await panel.getByText('Claude Code 错误末尾', {exact: true}).waitFor();
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

  await primary.getByRole('link', {name: '工单', exact: true}).click();
  await page.getByRole('button', {name: '新建工单'}).click();
  const issueDialog = page.getByRole('dialog', {name: '新建工单'});
  await issueDialog.getByLabel('主题').fill('从分析讨论创建工单');
  await issueDialog.getByLabel('目标与要求').fill('汇总分析讨论群里的任务和交付要求。');
  await issueDialog.getByLabel('关联群聊').selectOption({label: '分析讨论'});
  await issueDialog.getByRole('button', {name: '创建工单'}).click();
  await page.getByRole('heading', {name: '从分析讨论创建工单', exact: true}).waitFor();
  await panel.getByText('builder 回复 分析群历史', {exact: true}).waitFor();
  assert.doesNotMatch(await panel.innerText(), /交付群历史|工作区群聊留档/);
  await panel.getByRole('link', {name: '分析讨论', exact: true}).click();
  await transcript.getByText('builder 回复 分析群历史', {exact: true}).waitFor();
  checks.push('Choose a named group during issue creation and retain a link to the original chat');

  await primary.getByRole('link', {name: '工单', exact: true}).click();
  await createIssue(page, '先建工单再关联群聊');
  const discussionDraft = panel.getByLabel('工单讨论');
  await discussionDraft.fill('正在编辑的评论');
  await panel.getByRole('button', {name: '关联群聊', exact: true}).click();
  const linkGroup = page.getByRole('dialog', {name: '关联群聊'});
  await linkGroup.getByRole('combobox').selectOption({label: '交付复核'});
  await linkGroup.getByRole('button', {name: '关联群聊', exact: true}).click();
  await panel.getByText('reviewer 回复 交付群历史', {exact: true}).waitFor();
  assert.equal(await discussionDraft.inputValue(), '正在编辑的评论');
  await page.reload();
  await panel.getByText('reviewer 回复 交付群历史', {exact: true}).waitFor();
  await panel.getByRole('link', {name: '交付复核', exact: true}).click();
  await transcript.getByText('reviewer 回复 交付群历史', {exact: true}).waitFor();
  checks.push('Link an existing issue to a group, preserve the comment draft, and keep the association after reload');

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
  await page.getByLabel('聊天消息').fill(markdownSample);
  await page.getByRole('button', {name: '发送消息'}).click();
  await checkMarkdown(transcript.locator('.markdown-body').filter({hasText: /builder 回复.*Markdown 格式样本/}), xss);
  assert.equal(await page.evaluate(() => window.__injected === true), false);
  checks.push('Chat replies render Markdown with safe links and literal raw HTML');
  await conversations.getByRole('link', {name: /^工作区私聊/}).click();
  await transcript.getByText('builder 回复 工作区私聊', {exact: true}).waitFor();
  await page.reload();
  await transcript.getByText('builder 回复 工作区私聊', {exact: true}).waitFor();
  await page.screenshot({path: path.join(output, 'chat-private.png')});
  await page.getByRole('link', {name: '工作区群聊', exact: true}).click();
  await transcript.getByText('reviewer 回复 工作区群聊留档', {exact: true}).waitFor();
  assert.doesNotMatch(await transcript.innerText(), /工作区私聊|另一个私聊/);
  checks.push('Choose an agent from the chat submenu, create independent sessions, switch and reload');

  await primary.getByRole('link', {name: '项目设置', exact: true}).click();
  await page.getByRole('heading', {name: '项目设置', exact: true}).waitFor();
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
  assert.deepEqual(await page.evaluate(() => fetch(location.pathname + 'api/state').then(response => response.json()).then(state => state.squad)),
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

  await primary.getByRole('link', {name: '项目设置', exact: true}).click();
  await page.getByRole('heading', {name: '项目设置', exact: true}).waitFor();
  const dataset = path.join(state, 'memory-case.jsonl');
  const comparison = path.join(state, 'projects', projectPath.split('/')[2], 'reports', 'memory-comparison.json');
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

  const workspaceMenu = primary.locator('#workspace-switch');
  const workspaces = page.getByRole('navigation', {name: '项目列表'});
  const selectedWorkspace = page.locator('#workspace-name');
  async function switchWorkspace(name) {
    await workspaceMenu.click();
    await workspaces.getByRole('link', {name, exact: true}).click();
    await selectedWorkspace.getByText(name, {exact: true}).waitFor();
  }
  await primary.getByRole('link', {name: '聊天', exact: true}).click();
  const existingGroup = new URL(await groups.getByRole('link', {name: '分析讨论', exact: true}).getAttribute('href'), page.url());
  const existingGroupId = existingGroup.hash.split('/').at(-1);
  await workspaceMenu.click();
  await page.screenshot({path: path.join(output, 'workspace-switcher.png')});
  await page.getByRole('button', {name: '新建项目', exact: true}).click();
  const workspaceModal = page.getByRole('dialog', {name: '新建项目'});
  await workspaceModal.getByLabel('项目名称').fill('报告项目');
  await workspaceModal.getByLabel('本机目录').fill(secondDirectory);
  await workspaceModal.getByRole('button', {name: '创建项目'}).click();
  await workspaceModal.getByRole('alert').getByText('该项目名称或目录已绑定。').waitFor();
  await workspaceModal.getByLabel('项目名称').fill('验收工作区');
  await workspaceModal.getByRole('button', {name: '创建项目'}).click();
  await selectedWorkspace.getByText('验收工作区', {exact: true}).waitFor();
  const privatePath = new URL(page.url()).pathname;
  assert.match(privatePath, /^\/p\/[0-9a-f]{32}\/$/);
  assert.equal((await post(page, 'task', {task_id: a})).status, 409);
  assert.equal((await post(page, 'messages', {group_id: existingGroupId})).status, 409);
  assert.equal((await page.request.get(new URL('/p/' + '0'.repeat(32) + '/api/state', url).href)).status(), 404);

  await primary.getByRole('link', {name: '项目设置', exact: true}).click();
  await profiles.getByRole('button', {name: '管理 Agent'}).click();
  const roster = page.getByRole('dialog', {name: '管理 Agent'});
  await roster.getByRole('button', {name: '移除', exact: true}).first().click();
  await roster.getByRole('button', {name: '添加 Agent'}).click();
  await roster.getByLabel('Agent 名称').last().fill('private-agent');
  await roster.getByLabel('专长').last().fill('只负责验收工作区');
  await roster.getByRole('button', {name: '保存 Agent', exact: true}).click();
  await profiles.getByText('只负责验收工作区', {exact: true}).waitFor();
  assert.equal(await lead.inputValue(), 'builder');
  await primary.getByRole('link', {name: '聊天', exact: true}).click();
  await transcript.getByText('向 工作区群聊 发送第一条消息', {exact: true}).waitFor();
  assert.equal(await groups.getByRole('link', {name: '分析讨论', exact: true}).count(), 0);
  assert.equal(await conversations.getByRole('link').count(), 0);
  await newGroup.click();
  await groupDialog.getByLabel('群聊名称').fill('分析讨论');
  await groupDialog.getByRole('checkbox', {name: 'private-agent'}).check();
  await groupDialog.getByRole('button', {name: '创建群聊'}).click();
  await page.getByRole('heading', {name: '分析讨论', exact: true}).waitFor();
  const newGroupHash = new URL(page.url()).hash;
  await page.getByLabel('聊天消息').fill('仅限验收工作区');
  await page.getByRole('button', {name: '发送消息'}).click();
  await transcript.getByText('private-agent 回复 仅限验收工作区', {exact: true}).waitFor();
  checks.push('Create and switch workspaces and scope agents, chats, and issue lookups to the workspace URL');

  await primary.getByRole('link', {name: '工单', exact: true}).click();
  const isolatedTitle = '仅验收区工单';
  const isolatedIssue = await createIssue(page, isolatedTitle, 'agent:private-agent');
  await switchWorkspace('报告项目');
  assert.equal((await post(page, 'task', {task_id: isolatedIssue})).status, 409);
  await openIssue(page, b, titleB);
  await switchWorkspace('验收工作区');
  await openIssue(page, isolatedIssue, isolatedTitle);
  await panel.getByText('已完成本轮执行', {exact: true}).waitFor();
  await stop();
  url = await start();
  await page.goto(new URL(privatePath + newGroupHash, url).href);
  await selectedWorkspace.getByText('验收工作区', {exact: true}).waitFor();
  await transcript.getByText('private-agent 回复 仅限验收工作区', {exact: true}).waitFor();
  await switchWorkspace('报告项目');
  await groups.getByRole('link', {name: '分析讨论', exact: true}).click();
  await transcript.getByText('builder 回复 分析群历史', {exact: true}).waitFor();
  await openIssue(page, b, titleB);
  checks.push('Keep workspace workers and chat histories isolated across switching and server restart');

  await page.setViewportSize({width: 390, height: 844});
  await page.screenshot({path: path.join(output, 'mobile.png'), fullPage: true});
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
  await primary.getByRole('link', {name: '项目设置', exact: true}).click();
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
  await page.setViewportSize({width: 320, height: 760});
  await workspaceMenu.click();
  await workspaces.getByRole('link', {name: '验收工作区', exact: true}).waitFor();
  assert.equal(await workspaces.evaluate(list => list.getBoundingClientRect().right <= innerWidth), true);
  await workspaceMenu.press('Escape');
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
