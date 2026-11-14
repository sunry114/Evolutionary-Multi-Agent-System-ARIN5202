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
const env = {...process.env, PYTHONIOENCODING:'utf-8', EVO_APPLICATION_FACTORY:''};
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
  assert.equal(await tasks.count(),4);
  assert.equal(await page.locator('#run-form button').isDisabled(),true);
  assert.equal(await page.locator('img').count(),0);
  assert.equal(await page.evaluate(()=>window.__xss===true),false);
  checks.push('Real SQLite rendering; XSS text stays inert; run disabled');
  await page.screenshot({path:path.join(output,'desktop.png'),fullPage:true});

  await page.getByRole('button',{name:'收件箱',exact:true}).click();
  assert.equal(await page.getByRole('button',{name:'批准',exact:true}).count(),0);
  const bad=page.locator('#inbox-list article').filter({hasText:'Error sample'});
  await bad.getByRole('button',{name:'要求修改',exact:true}).click();
  await page.waitForFunction(()=>document.querySelector('#status').textContent.includes('Invalid request'));
  assert.equal(await bad.getByRole('button',{name:'要求修改',exact:true}).isEnabled(),true);
  checks.push('Invalid review shows error and re-enables the action; approval is read-only');

  const accept=page.locator('#inbox-list article').filter({hasText:'Accept sample'});
  await accept.getByRole('button',{name:'接受结果',exact:true}).click();
  await accept.waitFor({state:'detached'});
  const revise=page.locator('#inbox-list article').filter({hasText:'Revise sample'});
  await revise.getByRole('textbox',{name:'审查意见'}).fill('Fix output');
  await revise.getByRole('button',{name:'要求修改',exact:true}).click();
  await revise.waitFor({state:'detached'});
  await page.getByRole('button',{name:'任务',exact:true}).click();
  const parent=tasks.filter({hasText:'Revise sample'});
  await parent.getByRole('textbox',{name:'修订任务'}).fill('Apply feedback');
  await parent.getByRole('button',{name:'创建修订任务',exact:true}).click();
  await page.waitForFunction(()=>document.querySelector('#status').textContent.includes('Revision saved'));
  await parent.getByText('执行：success · 审查：revising',{exact:true}).waitFor();
  assert.equal(await tasks.count(),5);
  await parent.locator('summary').click();
  await parent.getByRole('button',{name:'加载 trace',exact:true}).click();
  await parent.locator('pre').filter({hasText:'revision_linked'}).waitFor();
  await parent.getByRole('button',{name:'加载 trace',exact:true}).click();
  assert.equal(await parent.locator('pre').count(),1);
  assert.equal(await parent.locator('pre').isVisible(),true);
  checks.push('Accept, request changes, create linked revision and load persisted trace');

  for(const [label,id] of [['群聊','group'],['私聊','dm'],['记忆','memory']]) {
    await page.getByRole('button',{name:label,exact:true}).click();
    assert.equal(await page.locator('#'+id).isVisible(),true);
    if(id!=='memory')assert.equal(await page.locator('#'+id+'-form button').isDisabled(),true);
  }
  checks.push('Navigation and unavailable chat controls');
  const oldToken=await page.locator('meta[name="csrf-token"]').getAttribute('content');
  await stop();url=await start();await page.goto(url);
  await page.locator('#notice').filter({hasText:'SQLite'}).waitFor();
  assert.notEqual(await page.locator('meta[name="csrf-token"]').getAttribute('content'),oldToken);
  await tasks.filter({hasText:'Revise sample'}).getByText('执行：success · 审查：revising',{exact:true}).waitFor();
  assert.equal(await tasks.count(),5);
  await page.getByRole('button',{name:'刷新状态',exact:true}).click();
  await tasks.filter({hasText:'Accept sample'}).getByText('执行：success · 审查：accepted',{exact:true}).waitFor();
  checks.push('Actual server-process restart + reload/refresh retains decisions and revision');
  await page.setViewportSize({width:390,height:844});
  await page.screenshot({path:path.join(output,'mobile.png'),fullPage:true});
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth),true,
    'Mobile page must not scroll horizontally');
  assert.deepEqual(errors,[]);
  checks.push('390px mobile layout without horizontal overflow; no browser JavaScript errors');
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
