// Exercise this exact production worker, with HTTPS entirely intercepted to a
// synthetic page. No user's profile, platform request or file upload is used.
import { chromium } from 'playwright';
import net from 'node:net';
import { spawn } from 'node:child_process';
import { readFile, mkdir, writeFile } from 'node:fs/promises';
import readline from 'node:readline';
import path from 'node:path';
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';

const root = path.dirname(fileURLToPath(import.meta.url));
process.loadEnvFile(process.env.MIDSCENE_ENV_FILE || 'E:/workSpace/brower-agent-test/.env');
const stamp = new Date().toISOString().replaceAll(':', '-');
const run = path.join(root, 'test-runs', `live-worker-${stamp}`); await mkdir(run, { recursive: true });
const secret = process.env.MIDSCENE_MODEL_API_KEY || process.env.OPENAI_API_KEY;
const redact = e => String(e?.stack || e).replaceAll(secret || '___no_secret___', '[redacted]');
const report = { time: new Date().toISOString(), engine: '@midscene/web@1.16.5', productionRequests: 0,
  uploadAttempts: 0, fileCapabilities: [], tasks: [], status: 'running' };
let browser, current;
async function connectWorker(params) {
  const proc = spawn(process.execPath, [path.join(root, 'worker.mjs')], { cwd: root, windowsHide: true,
    stdio: ['pipe', 'pipe', 'pipe'], env: process.env });
  const lines = readline.createInterface({ input: proc.stdout });
  const iterator = lines[Symbol.asyncIterator](); let counter = 0, tail = '';
  proc.stderr.on('data', b => { tail = (tail + redact(b)).slice(-3000); });
  const exited = new Promise(resolve => proc.once('exit', resolve));
  const rpc = {
    call: async (method, p = {}) => {
      const id = ++counter; proc.stdin.write(JSON.stringify({ id, method, params: p }) + '\n');
      const line = await Promise.race([iterator.next(), new Promise((_, reject) => {
        const timer = setTimeout(() => { proc.kill(); reject(Error('Worker deadline exceeded: ' + tail)); }, 150000); timer.unref();
      })]);
      if (line.done) throw Error('Worker exited: ' + tail);
      const value = JSON.parse(line.value); assert.equal(value.id, id);
      if (value.error) throw Error(value.error.code + ': ' + value.error.message);
      return value.result;
    },
    close: async () => { try { if (proc.exitCode === null) await rpc.call('close'); } finally { proc.stdin.end(); if (proc.exitCode === null) await exited; lines.close(); } },
    kill: () => proc.kill(),
  };
  try { await rpc.call('init', params); return rpc; } catch (error) { proc.kill(); throw error; }
}
try {
  const server = net.createServer(); await new Promise(r => server.listen(0, '127.0.0.1', r));
  const port = server.address().port; await new Promise(r => server.close(r));
  browser = await chromium.launch({ channel: 'chrome', headless: true, args: [`--remote-debugging-port=${port}`] });
  const context = await browser.newContext({ viewport: { width: 1280, height: 900 }, deviceScaleFactor: 1 });
  const html = `<!doctype html><meta charset="utf-8"><style>body{font:22px 'Microsoft Yahei';padding:40px}button,input{font:22px 'Microsoft Yahei';padding:16px;margin:16px}section{padding:30px;border:1px solid #ccc}</style><header>DEMO-ACCOUNT</header><h1>付费短剧测试台</h1><section id="list"><p>暂无数据</p><button onclick="document.querySelector('#list').hidden=true;document.querySelector('#form').hidden=false">内容上传</button></section><section id="form" hidden><h2>版权及备案信息</h2><label>集数标题<input aria-label="集数标题"></label><p>仅验证当前页面定位，不选择文件，不提交。</p><button>提交审核</button></section>`;
  await context.route('**/*', route => route.fulfill({ contentType: 'text/html; charset=utf-8', body: html }));
  const page = await context.newPage();
  const source = await readFile(path.join(root, '../kuaishou_upload/observation.py'), 'utf8');
  const snapshotScript = source.match(/SNAPSHOT = r"""([\s\S]*?)"""/)[1];
  await page.goto('https://kdj.kuaishou.com/__midscene_fixture__');
  const cdp = await context.newCDPSession(page); const { targetInfo } = await cdp.send('Target.getTargetInfo'); await cdp.detach();
  for (const mode of ['cold', 'warm-new-worker']) {
    if (mode !== 'cold') await page.reload();
    current = await connectWorker({ cdpUrl: `http://127.0.0.1:${port}`, targetId: targetInfo.targetId,
      allowedHosts: ['kdj.kuaishou.com'], expectedAccount: 'DEMO-ACCOUNT', files: {}, snapshotScript,
      cacheFile: path.join(run, 'cache') });
    const result = await current.call('ensure_upload_form', { pageId: targetInfo.targetId });
    assert.equal(result.verified, true);
    assert.equal(await page.locator('#form').isVisible(), true);
    const binding = await current.call('field_binding', { pageId: targetInfo.targetId, name: '集数标题', value: '', selected: false });
    const snapshot = await current.call('snapshot', { pageId: targetInfo.targetId });
    assert.equal(snapshot.controls.find(n => n.ref === binding.ref)?.tag, 'input');
    // Trusted program writes a different value each time, never an AI-generated value.
    const value = mode === 'cold' ? '缓存首条验证' : '缓存第二条验证';
    await current.call('evaluate', { pageId: targetInfo.targetId, source: '(ref,value)=>{const e=document.querySelector(`[data-ks-upload-ref="${ref}"]`);e.value=value;return e.value}', args: [binding.ref, value] });
    assert.equal(await page.locator('input').inputValue(), value);
    const metrics = await current.call('commit_cache', { pageId: targetInfo.targetId });
    report.tasks.push({ mode, metrics });
    await current.close(); current = null;
    assert.equal(browser.isConnected(), true); assert.equal(context.pages().length, 1);
  }
  report.status = 'production_worker_fixture_verified'; report.sameBrowserAndTab = true;
} catch (error) { report.status = 'failed'; report.error = redact(error); process.exitCode = 1; }
finally {
  if (current) current.kill();
  if (browser) await browser.close();
  await writeFile(path.join(run, 'report.json'), JSON.stringify(report, null, 2));
  console.log(JSON.stringify({ status: report.status, report: path.join(run, 'report.json') }));
}
