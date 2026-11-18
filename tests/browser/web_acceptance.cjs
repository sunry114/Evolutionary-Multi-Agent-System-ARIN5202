/* Real Chromium/Edge + local SQLite server; no Agent/model calls. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const {spawn, spawnSync} = require('node:child_process');
const {once} = require('node:events');
const {createInterface} = require('node:readline');
const {chromium} = require(process.env.EVO_PLAYWRIGHT_MODULE || 'playwright');

const root = path.resolve(__dirname, '../..');
const output = path.resolve(process.env.EVO_BROWSER_OUTPUT || path.join(root, '.evo/browser-acceptance'));
fs.mkdirSync(output, {recursive: true});
const state = fs.mkdtempSync(path.join(os.tmpdir(), 'evo-browser-'));
const python = process.env.EVO_TEST_PYTHON || 'python';
const env = {...process.env, PYTHONIOENCODING:'utf-8', EVO_APPLICATION_FACTORY:'c14_fixture:create_application', PYTHONPATH:[path.join(__dirname),process.env.PYTHONPATH].filter(Boolean).join(path.delimiter)};
let server, browser;
const checks = [];
async function start() {
  server = spawn(python, ['-m','evo','--state',state,'web','--port','0'], {cwd:root, env, windowsHide:true});
  server.stderr.resume();
  const reader = createInterface({input:server.stdout});
  return new Promise((resolve,reject)=>{
    const timer=setTimeout(()=>reject(Error('Server startup timeout')),15000);
    reader.on('line',line=>{if(line.startsWith('Open http://127.0.0.1:')) {
      clearTimeout(timer); resolve(line.slice(5));}});
    server.once('exit',code=>{clearTimeout(timer);reject(Error('Server exited '+code));});
  });
}
async function stop() {
  if(server && server.exitCode===null) {
    const exited=once(server,'exit');server.kill();await exited;
  }
}
async function run() {
  const seeded=spawnSync(python,[path.join(__dirname,'seed.py'),state],{cwd:root,env,encoding:'utf-8',windowsHide:true});
  assert.equal(seeded.status,0,seeded.stderr);
  browser=await chromium.launch({headless:true,...(process.env.EVO_BROWSER_EXECUTABLE ?
    {executablePath:process.env.EVO_BROWSER_EXECUTABLE}: {})});
  const page=await browser.newPage({viewport:{width:1280,height:900}});
  page.setDefaultTimeout(10000);
  const errors=[];page.on('pageerror',error=>errors.push(error.message));
  let url=await start();
  await page.goto(url);
  await page.locator('#notice').filter({hasText:'SQLite'}).waitFor();
  const tasks=page.locator('#task-list article');
  assert.equal(await tasks.count(),7);
  assert.equal(await page.locator('#run-form button').isEnabled(),true);
  assert.equal(await page.locator('img').count(),0);
  assert.equal(await page.evaluate(()=>window.__xss===true),false);
  checks.push('Real SQLite rendering; XSS text stays inert; run control enabled');
  await page.screenshot({path:path.join(output,'desktop.png'),fullPage:true});

  await page.getByRole('button',{name:'私聊',exact:true}).click();
  assert.equal(await page.locator('#dm-dot').isVisible(),true);
  const approval=page.locator('#dm-rooms .request-room > div').filter({hasText:'delete_file'});
  await approval.getByRole('button',{name:'批准',exact:true}).click();
  await approval.waitFor({state:'detached'});
  const denied=page.locator('#dm-rooms .request-room > div').filter({hasText:'run_python'});
  await denied.getByRole('button',{name:'拒绝',exact:true}).click();await denied.waitFor({state:'detached'});
  const clarification=page.locator('#dm-rooms .request-room > div').filter({hasText:'Which input file?'});
  await clarification.getByRole('textbox',{name:'回复 planner'}).fill('report.md');
  await clarification.getByRole('button',{name:'发送回复',exact:true}).click();await clarification.waitFor({state:'detached'});
  const bad=page.locator('#dm-rooms article').filter({hasText:'Error sample'});
  await bad.getByRole('button',{name:'要求修改',exact:true}).click();
  await page.waitForFunction(()=>document.querySelector('#status').textContent.includes('Invalid request'));
  assert.equal(await bad.getByRole('button',{name:'要求修改',exact:true}).isEnabled(),true);
  checks.push('Approval decision persists; invalid review shows error and re-enables the action');

  const accept=page.locator('#dm-rooms article').filter({hasText:'Accept sample'});
  await accept.getByRole('button',{name:'接受结果',exact:true}).click();
  await accept.waitFor({state:'detached'});
  const revise=page.locator('#dm-rooms article').filter({hasText:'Revise sample'});
  await revise.getByRole('textbox',{name:'审查意见'}).fill('Fix output');
  await revise.getByRole('button',{name:'要求修改',exact:true}).click();
  await revise.waitFor({state:'detached'});
  await page.getByRole('button',{name:'任务',exact:true}).click();
  const parent=tasks.filter({hasText:'Revise sample'});
  await parent.getByRole('textbox',{name:'修订任务'}).fill('Apply feedback');
  await parent.getByRole('button',{name:'创建修订任务',exact:true}).click();
  await page.waitForFunction(()=>document.querySelector('#status').textContent.includes('Revision saved'));
  await parent.getByText('执行：success · 审查：revising',{exact:true}).waitFor();
  assert.equal(await tasks.count(),8);
  await parent.locator('summary').click();
  await parent.getByRole('button',{name:'加载 trace',exact:true}).click();
  await parent.locator('pre').filter({hasText:'revision_linked'}).waitFor();
  await parent.getByRole('button',{name:'加载 trace',exact:true}).click();
  assert.equal(await parent.locator('pre').count(),1);
  assert.equal(await parent.locator('pre').isVisible(),true);
  await parent.getByRole('button',{name:'查看指标',exact:true}).click();
  await parent.locator('pre').filter({hasText:'stored_task_state'}).waitFor();
  checks.push('Accept, revision, persisted trace and stored task metrics');

  for(const [label,id] of [['群聊','group'],['私聊','dm'],['记忆','memory']]) {
    await page.getByRole('button',{name:label,exact:true}).click();
    assert.equal(await page.locator('#'+id).isVisible(),true);
    if(id==='dm')assert.equal(await page.locator('#dm-form button').isEnabled(),true);
  }
  await page.locator('#memory-form button').click();await page.locator('#memory-list').getByText(/暂无匹配经验/).waitFor();
  await page.getByRole('button',{name:'评测',exact:true}).click();
  await page.locator('#eval-form input[name=dataset]').fill('missing.jsonl');
  await page.locator('#eval-form input[name=output]').fill('outside/reports.json');
  await page.locator('#eval-form button').click();
  await page.waitForFunction(()=>document.querySelector('#status').textContent.includes('Invalid request'));
  checks.push('Memory query and eval validation; navigation and live chat controls');
  await page.getByRole('button',{name:'群聊',exact:true}).click();
  assert.equal(await page.locator('#group-form').count(),0);
  await page.getByRole('button',{name:'私聊',exact:true}).click();
  await page.locator('#agents').selectOption('builder');
  await page.locator('#dm-form textarea').fill('Private builder input');await page.locator('#dm-form button').click();
  await page.locator('#dm-list').getByText(/Fixture reply from builder/).waitFor();
  assert.equal(await page.locator('#agents').inputValue(),'builder');
  await page.locator('#agents').selectOption('reviewer');assert.equal(await page.locator('#dm-list .bubble').count(),0);
  await page.locator('#dm-form textarea').fill('Private reviewer input');await page.locator('#dm-form button').click();
  await page.locator('#dm-list').getByText(/Fixture reply from reviewer/).waitFor();
  assert.equal((await page.locator('#dm-list').innerText()).includes('Private builder input'),false);
  await page.locator('#agents').selectOption('builder');
  await page.locator('#dm-form textarea').fill('force-provider-error');await page.locator('#dm-form button').click();
  await page.waitForFunction(()=>document.querySelector('#status').textContent.includes('Agent reply failed'));
  await page.locator('#dm-list').getByText(/force-provider-error/).waitFor();
  assert.equal((await page.locator('#status').innerText()).includes('private-fixture-error'),false);
  checks.push('Real ChatService + SQLite through offline provider: group replies, private histories and safe provider failure');
  await page.getByRole('button',{name:'任务',exact:true}).click();
  const markdown='## Researcher (researcher)\n```text\nFind evidence\n```\n## Builder (builder)\n```text\nCreate files\n```\n## Reviewer (reviewer)\n```text\nCheck results\n```\n## Task 1\n```text\nPrepare imported report.md\n```';
  await page.locator('#md-upload').setInputFiles({name:'scenario.md',mimeType:'text/markdown',buffer:Buffer.from(markdown)});
  await page.waitForFunction(()=>document.querySelector('#run-form textarea[name=prompt]').value==='Prepare imported report.md');
  await page.locator('#run-form button').click();
  assert.equal(await page.locator('#tasks').isVisible(),true);
  assert.equal(await page.locator('#group').isVisible(),false);
  await page.getByRole('button',{name:'群聊',exact:true}).click();
  await page.locator('#group-list').getByText(/Fixture reply from reviewer/).last().waitFor();
  assert.equal(await page.locator('#group-list .bubble time').count(),3);
  await page.getByRole('button',{name:'私聊',exact:true}).click();
  await page.locator('#agents').selectOption('builder');
  await page.locator('#dm-form textarea').fill('Task private question');
  await page.locator('#dm-form button').click();
  await page.locator('#dm-list').getByText(/Fixture reply from builder/).waitFor();
  assert.equal(await page.locator('#dm-list .bubble time').count(),2);
  await page.getByRole('button',{name:'群聊',exact:true}).click();
  assert.equal((await page.locator('#group-list').innerText()).includes('Prepare imported report.md'),false);
  await page.getByRole('button',{name:'任务',exact:true}).click();
  await tasks.filter({hasText:'Prepare imported report.md'}).getByText(/执行：created/).waitFor();
  assert.equal(await page.locator('#run-form input[name=expected_path]').count(),0);
  await tasks.filter({hasText:'Prepare imported report.md'}).getByRole('button',{name:'开始执行'}).click();
  assert.equal(await page.locator('#checks-dialog').isVisible(),true);
  await page.locator('#cancel-checks').click();
  await tasks.filter({hasText:'Prepare imported report.md'}).getByRole('button',{name:'终止任务'}).click();
  assert.equal(await page.locator('#terminate-dialog').isVisible(),true);
  await page.locator('#cancel-terminate').click();
  await tasks.filter({hasText:'Prepare imported report.md'}).getByText(/执行：created/).waitFor();
  await tasks.filter({hasText:'Prepare imported report.md'}).getByRole('button',{name:'终止任务'}).click();
  await page.locator('#confirm-terminate').click();
  await tasks.filter({hasText:'Prepare imported report.md'}).getByText(/执行：blocked/).waitFor();
  await page.getByRole('button',{name:'群聊',exact:true}).click();
  assert.equal(await page.locator('#group-list .bubble').count(),0);
  await page.getByRole('button',{name:'私聊',exact:true}).click();
  assert.equal(await page.locator('#dm-list .bubble').count(),0);
  assert.equal(await page.locator('#dm-form button').isDisabled(),true);
  await page.getByRole('button',{name:'任务',exact:true}).click();
  assert.equal(await tasks.count(),9);
  checks.push('Markdown import, directed group display and confirmed termination clear task chat');
  const oldToken=await page.locator('meta[name="csrf-token"]').getAttribute('content');
  await stop();url=await start();await page.goto(url);
  await page.locator('#notice').filter({hasText:'SQLite'}).waitFor();
  assert.notEqual(await page.locator('meta[name="csrf-token"]').getAttribute('content'),oldToken);
  await tasks.filter({hasText:'Revise sample'}).getByText('执行：success · 审查：revising',{exact:true}).waitFor();
  assert.equal(await tasks.count(),9);
  await page.getByRole('button',{name:'刷新状态',exact:true}).click();
  await tasks.filter({hasText:'Accept sample'}).getByText('执行：success · 审查：accepted',{exact:true}).waitFor();
  await page.getByRole('button',{name:'私聊',exact:true}).click();await page.locator('#agents').selectOption('builder');
  await page.locator('#dm-list').getByText(/Private builder input/).waitFor();
  await page.getByRole('button',{name:'任务',exact:true}).click();
  checks.push('Actual restart retains review/revision, chat histories and human decisions');
  await page.setViewportSize({width:390,height:844});
  await page.screenshot({path:path.join(output,'mobile.png'),fullPage:true});
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth),true,
    'Mobile page must not scroll horizontally');
  assert.deepEqual(errors,[]);
  checks.push('390px mobile layout without horizontal overflow; no browser JavaScript errors');
  await page.locator('#clear-finished').click();
  await page.locator('#tasks-done').getByText('暂无记录').waitFor();
  assert.equal(await tasks.count(),4);
  await page.locator('#run-form textarea[name=prompt]').fill('Next task after clear');
  await page.locator('#run-form button').click();
  assert.equal(await page.locator('#tasks').isVisible(),true);
  await tasks.filter({hasText:'Next task after clear'}).waitFor();
  checks.push('Finished board cache clears without affecting the next task');
  fs.writeFileSync(path.join(output,'report.json'),JSON.stringify({status:'passed',browser:browser.version(),
    checks,fixture:'isolated seeded SQLite; no real Agent results'},null,2));
  console.log(JSON.stringify({status:'passed',checks:checks.length,output}));
}
run().catch(error=>{console.error(error);process.exitCode=1;}).finally(async()=>{
  await stop();if(browser)await browser.close();
  assert.equal(path.dirname(path.resolve(state)),path.resolve(os.tmpdir()));
  assert.ok(path.basename(state).startsWith('evo-browser-'));
  fs.rmSync(state,{recursive:true,force:true});
});
