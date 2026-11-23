const { spawn } = require('node:child_process');

async function check(options) {
  const { chromium } = require(options.module);
  let serverError;
  const server = options.server.length
    ? spawn(options.server[0], options.server.slice(1), { stdio: 'ignore' })
    : null;
  if (server) {
    server.on('error', error => { serverError = error; });
    server.unref();
  }
  const browser = await chromium.launch({ headless: true, timeout: 6000 });
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 900 }, serviceWorkers: 'block' });
    page.setDefaultTimeout(4000);
    const target = new URL(options.target);
    await page.route(/^https?:/, route => new URL(route.request().url()).origin === target.origin
      ? route.continue() : route.abort());
    const errors = [];
    page.on('pageerror', error => {
      if (errors.length < 5) errors.push(error.message.slice(0, 300));
    });
    const deadline = Date.now() + 5500;
    while (true) {
      if (serverError) throw serverError;
      if (server && server.exitCode !== null) throw new Error(`Server exited with status ${server.exitCode}`);
      try {
        const response = await page.goto(options.target, { timeout: 4000 });
        if (response && !response.ok()) throw new Error(`Page returned HTTP ${response.status()}`);
        break;
      } catch (error) {
        if (!server || Date.now() >= deadline || !error.message.includes('ERR_CONNECTION_REFUSED')) throw error;
        await new Promise(resolve => setTimeout(resolve, 150));
      }
    }
    if (options.selector) await page.locator(options.selector).first().waitFor({ state: 'visible' });
    if (options.screenshot) await page.screenshot({ path: options.screenshot, timeout: 4000 });
    if (errors.length) throw new Error(errors.join('\n'));
    console.log(JSON.stringify({ title: (await page.title()).slice(0, 500), browser: browser.version() }));
  } finally {
    await browser.close();
    if (server && server.exitCode === null && server.signalCode === null) server.kill('SIGTERM');
  }
}

const deadline = setTimeout(() => process.kill(-process.pid, 'SIGKILL'), 24000);
deadline.unref();
check(JSON.parse(process.argv[2])).catch(error => {
  console.error(String(error).slice(0, 2000));
  process.exitCode = 1;
});
