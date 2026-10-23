import { test, before, after } from 'node:test';
import assert from 'node:assert/strict';
import { readFile, mkdtemp, writeFile, mkdir } from 'node:fs/promises';
import path from 'node:path';
import net from 'node:net';
import { chromium } from 'playwright';
import { MidsceneEngine } from '../engine.mjs';
import { existsSync } from 'node:fs';
import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { createHash } from 'node:crypto';
import { runtimeLimits, configureClientTimeout } from '../runtime-limits.mjs';
import { createServer } from 'node:http';

process.env.MIDSCENE_MODEL_NAME = 'deepseek-flash-free';
process.env.MIDSCENE_MODEL_FAMILY = 'deepseek';
process.env.MIDSCENE_MODEL_API_KEY = 'fixture-only-no-api-call';
process.env.MIDSCENE_MODEL_BASE_URL = 'https://unused.invalid/v1';

let browser, cdpUrl, snapshotScript, root;
before(async () => {
  const server = net.createServer();
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const port = server.address().port; await new Promise(resolve => server.close(resolve));
  browser = await chromium.launch({ channel: 'chrome', headless: true, args: [`--remote-debugging-port=${port}`] });
  cdpUrl = `http://127.0.0.1:${port}`;
  const source = await readFile(new URL('../../kuaishou_upload/observation.py', import.meta.url), 'utf8');
  snapshotScript = source.match(/SNAPSHOT = r"""([\s\S]*?)"""/)[1];
  const temp = new URL('../test-runs/', import.meta.url); await mkdir(temp, { recursive: true });
  root = await mkdtemp(new URL('engine-', temp));
});
after(async () => { if (browser) await browser.close(); });

async function fixture(body, inner = '<button>普通按钮</button>') {
  const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  await context.route('**/*', route => route.fulfill({ contentType: 'text/html; charset=utf-8', body:
    route.request().url().endsWith('/inner') ? inner : `<header>TARGET-ACCOUNT</header>${body}` }));
  const page = await context.newPage();
  await page.goto('https://kdj.kuaishou.com/home/content/content-management');
  const session = await context.newCDPSession(page);
  const { targetInfo } = await session.send('Target.getTargetInfo'); await session.detach();
  const file = path.join(root, 'fixture.mp4'); await writeFile(file, 'local-fixture-only');
  const engine = new MidsceneEngine({ cdpUrl, targetId: targetInfo.targetId, allowedHosts: ['kdj.kuaishou.com'],
    expectedAccount: 'TARGET-ACCOUNT', snapshotScript, files: { video: file }, cacheFile: path.join(root, 'cache') });
  try { await engine.initialize(); }
  catch (error) { await engine.close(); await context.close(); throw error; }
  return { engine, page, id: targetInfo.targetId, cleanup: async () => { await engine.close(); await context.close(); } };
}

test('request and navigation budgets share defaults and validate configuration',()=>{
  assert.deepEqual(runtimeLimits({}),{requestMs:0,navigationMs:0});
  assert.deepEqual(runtimeLimits({MIDSCENE_MODEL_TIMEOUT:'45000'}),{requestMs:0,navigationMs:0});
  assert.deepEqual(runtimeLimits({MIDSCENE_WAIT_FOR_MODEL:'false',MIDSCENE_MODEL_TIMEOUT:'240000'}),{requestMs:240000,navigationMs:270000});
  assert.throws(()=>runtimeLimits({MIDSCENE_WAIT_FOR_MODEL:'false',MIDSCENE_MODEL_TIMEOUT:'90000',MIDSCENE_NAVIGATION_TIMEOUT:'90000'}),/必须大于/);
  for(const value of ['', '0', '600001','1e3','12.5'])assert.throws(()=>runtimeLimits({MIDSCENE_WAIT_FOR_MODEL:'false',MIDSCENE_MODEL_TIMEOUT:value}));
});

test('clear upload entry reaches a verified form without a model call',async()=>{
  const f=await fixture(`<button id="entry" onclick="document.body.insertAdjacentHTML('beforeend','<label>版权<input></label>')">内容上传</button><button>提交审核</button>`);
  try {
    f.engine.agent=()=>{throw Error('明确入口不应调用模型');};
    const result=await f.engine.ensureUploadForm(f.id);
    assert.equal(result.verified,true);assert.equal(result.strategy,'guarded_dom');
    assert.equal(f.engine.requestAttempts,0);assert.equal(f.engine.navigation,false);
  }finally{await f.cleanup();}
});

test('direct click alone is not accepted; unresolved transition uses bounded visual recovery',async()=>{
  const f=await fixture('<button onclick="window.clicks=(window.clicks||0)+1">内容上传</button>');
  try {
    let calls=0;
    f.engine.agents.set(f.id,{aiAct:async(_goal,options)=>{
      calls++;assert.equal(options.abortSignal,undefined);
      if(options.cacheable===false)await f.page.evaluate(()=>document.body.insertAdjacentHTML('beforeend','<label>版权<input></label>'));
    },flushCache:async()=>{},destroy:async()=>{},metrics:{calls:0,totalTokens:0}});
    const result=await f.engine.ensureUploadForm(f.id);
    assert.equal(result.strategy,'midscene_guarded_visual');assert.equal(calls,2);
    assert.equal(await f.page.evaluate(()=>window.clicks),1);
    assert.equal(f.engine.navigation,false);
  }finally{await f.cleanup();}
});

test('real local HTTP timeout follows configured request limit and failed requests are not resent',async()=>{
  const previous={request:process.env.MIDSCENE_MODEL_TIMEOUT,navigation:process.env.MIDSCENE_NAVIGATION_TIMEOUT,wait:process.env.MIDSCENE_WAIT_FOR_MODEL};
  process.env.MIDSCENE_WAIT_FOR_MODEL='false';
  process.env.MIDSCENE_MODEL_TIMEOUT='1000';process.env.MIDSCENE_NAVIGATION_TIMEOUT='5000';
  let requests=0;
  const server=createServer((req,res)=>{requests++;req.resume();const timer=setTimeout(()=>{res.writeHead(200,{'Content-Type':'application/json'});res.end('{}');},2500);timer.unref();});
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  let f;
  try {
    f=await fixture('<p>暂无数据</p>');
    const agent=await f.engine.agent(f.id);
    const {default:OpenAI}=await import('openai');
    const client=await agent.opts.createOpenAIClient(new OpenAI({apiKey:'fixture-only',baseURL:`http://127.0.0.1:${server.address().port}/v1`,maxRetries:0}));
    const request=()=>client.chat.completions.create({model:'fixture-only',messages:[{role:'user',content:'local timeout fixture'}]});
    const started=Date.now();
    await assert.rejects(request(),error=>error.code==='midscene_model_error'&&/单次上限1秒/.test(error.message));
    assert.ok(Date.now()-started<4500,'must not retain the old hard-coded 45-second client timeout');
    await assert.rejects(request(),/单次上限1秒/);
    assert.equal(requests,1);assert.equal(f.engine.requestAttempts,1);
  }finally{
    if(f)await f.cleanup();
    server.closeAllConnections();await new Promise(resolve=>server.close(resolve));
    if(previous.request===undefined)delete process.env.MIDSCENE_MODEL_TIMEOUT;else process.env.MIDSCENE_MODEL_TIMEOUT=previous.request;
    if(previous.navigation===undefined)delete process.env.MIDSCENE_NAVIGATION_TIMEOUT;else process.env.MIDSCENE_NAVIGATION_TIMEOUT=previous.navigation;
    if(previous.wait===undefined)delete process.env.MIDSCENE_WAIT_FOR_MODEL;else process.env.MIDSCENE_WAIT_FOR_MODEL=previous.wait;
  }
});

test('unlimited client waits for delayed response and still honors explicit caller abort',async()=>{
  const {default:OpenAI}=await import('openai');
  let requests=0,delay=350;
  const server=createServer((req,res)=>{
    requests++;req.resume();
    const timer=setTimeout(()=>{res.writeHead(200,{'Content-Type':'application/json'});res.end(JSON.stringify({id:'local',choices:[{message:{role:'assistant',content:'delayed-ok'}}]}));},delay);timer.unref();
  });
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  try{
    const client=configureClientTimeout(new OpenAI({apiKey:'fixture-only',baseURL:`http://127.0.0.1:${server.address().port}/v1`,timeout:50,maxRetries:0}),0);
    const response=await client.chat.completions.create({model:'fixture-only',messages:[{role:'user',content:'local wait fixture'}]});
    assert.equal(response.choices[0].message.content,'delayed-ok');assert.equal(requests,1);
    delay=3000;
    const controller=new AbortController();
    const pending=client.chat.completions.create({model:'fixture-only',messages:[{role:'user',content:'abort fixture'}]},{signal:controller.signal});
    setTimeout(()=>controller.abort(),80);
    await assert.rejects(pending,/abort/i);
    assert.equal(requests,2);
  }finally{server.closeAllConnections();await new Promise(resolve=>server.close(resolve));}
});

function batchFixtureBody() {
  return `<p>版权 单集信息</p><main id="cards"></main>
    <button onclick="addCard()">添加单集信息</button>
    <label>批量上传视频<input id="bulk" type="file" accept="video/mp4" multiple onchange="window.selectionCalls=(window.selectionCalls||0)+1"></label>
    <script>let cardCount=0;function addCard(){let e=document.createElement('section');e.innerHTML='<h3>第'+(++cardCount)+'集</h3><label>集数标题<input type="text"></label><label>单集付费<input type="checkbox"></label>';document.getElementById('cards').append(e)}addCard();</script>`;
}

async function batchSpecs(f, numbers=[1,2,3]) {
  const jobs=[];
  for (const number of numbers) {
    const file=path.join(root,`batch-${number}.mp4`), bytes=Buffer.from(`BATCH FIXTURE ${number}`);
    await writeFile(file,bytes);
    jobs.push({sha256:createHash('sha256').update(bytes).digest('hex'),path:file,filename:path.basename(file),number,title:`第${number}集`,paid:false,price:0});
  }
  f.engine.config.batchFiles=jobs; return jobs;
}

test('folder batch creates all cards and selects every allowlisted video in ONE file-change, no model call',async()=>{
  const f=await fixture(batchFixtureBody());
  try {
    const jobs=await batchSpecs(f),hashes=jobs.map(j=>j.sha256);
    f.engine.agent=()=>{throw Error('批量操作不应调用模型');};
    const result=await f.engine.prepareBatch(f.id,hashes,3);
    assert.equal(result.count,3);
    assert.deepEqual(await f.page.locator('#cards input[type=text]').evaluateAll(es=>es.map(e=>e.value)),['第1集','第2集','第3集']);
    await f.engine.verifyBatch(f.id,hashes);
    const uploaded=await f.engine.selectBatchFiles(f.id,hashes);
    assert.equal(uploaded.selected,3);
    assert.equal(await f.page.evaluate(()=>window.selectionCalls),1);
    assert.deepEqual(await f.page.locator('#bulk').evaluate(e=>[...e.files].map(f=>f.name)),jobs.map(j=>j.filename));
    await assert.rejects(f.engine.selectBatchFiles(f.id,hashes),/禁止重传/);
    assert.equal(await f.page.evaluate(()=>window.selectionCalls),1);
  } finally {await f.cleanup();}
});

test('Vue-style pointer div/span add control creates cards without relying on CSS names or model calls',async()=>{
  const body=batchFixtureBody().replace('<button onclick="addCard()">添加单集信息</button>',
    '<div class="arbitrary-create-control" style="cursor:pointer" onclick="addCard()"><i>+</i><span>添加单集信息</span></div>');
  const f=await fixture(body);
  try{
    const jobs=await batchSpecs(f),hashes=jobs.map(j=>j.sha256);
    f.engine.agent=()=>{throw Error('明确的非标准控件不应调用模型');};
    const s=await f.engine.snapshot(f.id);
    const nodes=s.controls.filter(n=>n.text==='添加单集信息');
    assert.equal(nodes.length,1);assert.equal(nodes[0].tag,'span');assert.equal(nodes[0].semanticClickable,true);
    await f.engine.prepareBatch(f.id,hashes,3,true);
    assert.equal(await f.page.locator('#cards section').count(),3);
    assert.equal(f.engine.requestAttempts,0);
    await f.engine.selectBatchFiles(f.id,hashes);
    assert.equal(await f.page.evaluate(()=>window.selectionCalls),1);
  }finally{await f.cleanup();}
});

test('semantic click targets still reject stale pointer state and legal/destructive controls',async()=>{
  const f=await fixture('<div style="cursor:pointer"><span id="add">添加单集信息</span></div><span id="legal" style="cursor:pointer">同意用户协议</span><span style="cursor:pointer">删除</span>');
  try{
    let s=await f.engine.snapshot(f.id);
    const add=s.controls.find(n=>n.text==='添加单集信息');assert.ok(add);
    await f.page.locator('#add').evaluate(e=>e.style.cursor='auto');
    await assert.rejects(f.engine.bound(f.id,add.ref),/已变化/);
    s=await f.engine.snapshot(f.id);
    for(const text of ['同意用户协议','删除']) {
      const node=s.controls.find(n=>n.text===text);assert.ok(node);
      await assert.rejects(f.engine.click(f.id,node.ref),/不允许/);
    }
  }finally{await f.cleanup();}
});

test('BEM content wrappers and aria-modal prompts preserve labels and add a batch in one confirmation',async()=>{
  const f=await fixture(`<p>版权 单集信息</p><main id="cards"></main>
    <div style="cursor:pointer" onclick="setTimeout(openPrompt,80)"><span>添加单集信息</span></div>
    <label>批量上传视频<input type="file" accept="video/mp4" multiple></label>
    <script>
    let count=0;function addCard(){const e=document.createElement('section');e.innerHTML='<h3>第'+(++count)+'集</h3><div class="arbitrary-form-item"><label for="not-an-input-id">单集标题</label><div class="arbitrary-form-item__content"><div><input type="text" placeholder="请输入"><span>0/30</span></div></div></div><div class="arbitrary-form-item"><label>单集价格</label><div class="arbitrary-form-item__content"><label role="radio">免费<input type="radio" name="paid'+count+'" value="true"></label><label role="radio">付费<input type="radio" name="paid'+count+'" value="false"></label></div></div>';document.querySelector('#cards').append(e)}
    function openPrompt(){const d=document.createElement('div');d.setAttribute('aria-modal','true');d.setAttribute('aria-label','添加单集信息');d.innerHTML='<p>添加单集信息</p><div>添加集数<div><input type="text"></div></div><button onclick="this.parentElement.remove()">取消</button><button onclick="confirmPrompt(this.parentElement)">确定</button>';document.body.append(d)}
    function confirmPrompt(d){window.confirms=(window.confirms||0)+1;for(let i=Number(d.querySelector('input').value);i>0;i--)addCard();d.remove()}addCard();
    </script>`);
  try{
    const jobs=await batchSpecs(f,[1,2,3,4]);
    let snap=await f.engine.snapshot(f.id);
    assert.equal(snap.controls.filter(n=>n.tag==='input'&&n.label==='单集标题').length,1);
    f.engine.agent=()=>{throw Error('known current controls need no model');};
    await f.engine.prepareBatch(f.id,jobs.map(j=>j.sha256),4,true);
    assert.equal(await f.page.evaluate(()=>window.confirms),1);
    snap=await f.engine.snapshot(f.id);
    assert.equal(snap.controls.filter(n=>n.tag==='input'&&n.label==='单集标题').length,4);
    assert.equal(snap.dialogs.length,0);assert.equal(f.engine.requestAttempts,0);
    await f.engine.verifyBatch(f.id,jobs.map(j=>j.sha256));
  }finally{await f.cleanup();}
});

test('aria-modal legal prompts remain blocked even without role=dialog',async()=>{
  await assert.rejects(fixture('<div aria-modal="true" aria-label="请确认">用户协议，请勾选同意<button>确定</button></div>'),/协议/);
});

test('paid selection refreshes the card before binding its newly rendered price',async()=>{
  const f=await fixture(batchFixtureBody());
  try{
    const jobs=await batchSpecs(f,[1]);jobs[0].paid=true;jobs[0].price=2;
    await f.page.locator('#cards input[type=checkbox]').evaluate(e=>e.addEventListener('change',()=>{
      if(e.checked&&!e.parentElement.querySelector('input[type=number]')){
        const l=document.createElement('label');l.innerHTML='单集价格<input type="number">';e.parentElement.append(l);
      }
    }));
    await f.engine.prepareBatch(f.id,jobs.map(j=>j.sha256),1,true);
    assert.equal(await f.page.locator('#cards input[type=number]').inputValue(),'2');
    await f.engine.verifyBatch(f.id,jobs.map(j=>j.sha256));
  }finally{await f.cleanup();}
});

test('batch refuses unknown hashes, changed files, changed card titles and silent episode renumbering',async()=>{
  const f=await fixture(batchFixtureBody());
  try {
    const jobs=await batchSpecs(f),hashes=jobs.map(j=>j.sha256);
    await assert.rejects(f.engine.prepareBatch(f.id,['f'.repeat(64)],1),/白名单/);
    await f.engine.prepareBatch(f.id,hashes,3);
    await f.page.locator('#cards input[type=text]').nth(1).fill('错误标题');
    await assert.rejects(f.engine.verifyBatch(f.id,hashes),/标题已变化/);
    await f.engine.prepareBatch(f.id,hashes,3);
    await writeFile(jobs[1].path,'CHANGED AFTER SCAN');
    await assert.rejects(f.engine.selectBatchFiles(f.id,hashes),/视频发生变化/);
    assert.equal(await f.page.evaluate(()=>window.selectionCalls||0),0);
  } finally {await f.cleanup();}
  const sparse=await fixture(batchFixtureBody());
  try {
    const jobs=await batchSpecs(sparse,[3,4,5,6]);
    await assert.rejects(sparse.engine.prepareBatch(sparse.id,jobs.map(j=>j.sha256),4),/没有第5集/);
    assert.equal(await sparse.page.evaluate(()=>window.selectionCalls||0),0);
  } finally {await sparse.cleanup();}
});

test('single-file input is not misreported as bulk upload support',async()=>{
  const f=await fixture(batchFixtureBody());
  try {
    const jobs=await batchSpecs(f,[1,2]),hashes=jobs.map(j=>j.sha256);
    await f.engine.prepareBatch(f.id,hashes,2);
    await f.page.locator('#bulk').evaluate(e=>e.multiple=false);
    await assert.rejects(f.engine.selectBatchFiles(f.id,hashes),/支持多选/);
    assert.equal(await f.page.evaluate(()=>window.selectionCalls||0),0);
  } finally {await f.cleanup();}
});

test('new series keeps nonfirst episode numbers when the page exposes explicit writable episode numbers',async()=>{
  const f=await fixture(batchFixtureBody());
  try {
    const jobs=await batchSpecs(f,[3,4]),hashes=jobs.map(j=>j.sha256);
    await f.page.evaluate(()=>{
      addCard();
      for(const section of document.querySelectorAll('#cards section')) {
        const label=document.createElement('label');label.innerHTML='集号<input type="number" value="'+section.querySelector('h3').textContent.match(/\d+/)[0]+'">';section.append(label);
      }
    });
    await f.engine.prepareBatch(f.id,hashes,2,true);
    await f.engine.verifyBatch(f.id,hashes);
    assert.deepEqual(await f.page.locator('#cards input[type=number]').evaluateAll(es=>es.map(e=>e.value)),['3','4']);
    await f.engine.selectBatchFiles(f.id,hashes);
    assert.equal(await f.page.evaluate(()=>window.selectionCalls),1);
  } finally {await f.cleanup();}
});

test('frame-aware snapshot, current refs, declared files and single owner tab survive disconnect', async () => {
  const f = await fixture('<iframe src="/inner" style="width:800px;height:600px"></iframe>',
    '<p>单集信息 版权</p><label>视频<input type="file" accept="video/mp4"></label><button>下一步</button>');
  try {
    const s = await f.engine.snapshot(f.id);
    assert.equal(s.frameCount, 1);
    const input = s.controls.find(n => n.type === 'file');
    assert.match(input.frameId, /^frame-/); assert.match(input.frameUrl, /inner$/);
    await assert.rejects(f.engine.files(f.id, input.ref, 'copyright'), /白名单/);
    await f.engine.files(f.id, input.ref, 'video');
    assert.equal(await f.page.frame({ url: /inner$/ }).locator('input').evaluate(e => e.files[0].name), 'fixture.mp4');
    assert.throws(() => f.engine.page('OTHER-TAB'), /未知/);
    await f.engine.close();
    assert.equal(browser.isConnected(), true); assert.equal(f.page.isClosed(), false);
  } finally { await f.cleanup(); }
});
test('stale refs, illegal destinations, hidden overlays and final capabilities fail closed', async () => {
  const f = await fixture('<button id="normal">内容上传</button><button id="final">提交审核</button>');
  try {
    const s = await f.engine.snapshot(f.id), button = s.controls.find(n => n.text === '内容上传');
    await assert.rejects(f.engine.goto(f.id, 'https://other.invalid/'), /授权域名/);
    const final = s.controls.find(n => n.text === '提交审核');
    await assert.rejects(f.engine.click(f.id, final.ref), /不允许/);
    await f.page.locator('#normal').evaluate(e => { e.textContent = '删除'; });
    await assert.rejects(f.engine.bound(f.id, button.ref), /已变化/);
    await f.page.locator('#normal').evaluate(e => { e.textContent = '内容上传'; });
    const fresh = await f.engine.snapshot(f.id);
    await f.page.evaluate(() => { const e = document.createElement('div'); e.style.cssText = 'position:fixed;inset:0;z-index:999;background:#fff'; document.body.append(e); });
    await assert.rejects(f.engine.click(f.id, fresh.controls.find(n => n.text === '内容上传').ref), /Timeout|intercepts|timeout/i);
  } finally { await f.cleanup(); }
});
test('native Agent including cache replay receives guarded Tap/Scroll, no fill/file/navigation-code tools', async () => {
  const f = await fixture('<button id="entry" onclick="document.body.dataset.clicked=\'yes\'">内容上传</button><button>提交审核</button>');
  try {
    const agent = await f.engine.agent(f.id);
    let httpCalls = 0;
    const denied = Object.assign(Error('402 quota exhausted'), { status: 402 });
    const client = await agent.opts.createOpenAIClient({ chat: { completions: { create: async () => { httpCalls++; throw denied; } } } });
    assert.equal(client.timeout,f.engine.limits.requestMs);
    await assert.rejects(client.chat.completions.create({}), /402/);
    await assert.rejects(client.chat.completions.create({}), /402/);
    assert.equal(httpCalls, 1, 'SDK semantic retries must not repeat an HTTP quota failure');
    const names = agent.fullActionSpace.map(a => a.name);
    assert.ok(names.includes('Tap')); assert.ok(names.includes('Scroll'));
    for (const forbidden of ['Input', 'Navigate', 'KeyboardPress', 'RegisterFileChooserAccept', 'EvaluateJavaScript']) assert.ok(!names.includes(forbidden), forbidden);
    const tap = agent.fullActionSpace.find(a => a.name === 'Tap');
    const rect = await f.page.locator('#entry').boundingBox(), center = [rect.x + rect.width / 2, rect.y + rect.height / 2];
    await assert.rejects(tap.call({ locate: { center } }), /受控导航/);
    f.engine.navigation = true;
    await tap.call({ locate: { center } });
    assert.equal(await f.page.evaluate(() => document.body.dataset.clicked), 'yes');
    const r = await f.page.getByText('提交审核', { exact: true }).boundingBox();
    await assert.rejects(tap.call({ locate: { center: [r.x + r.width / 2, r.y + r.height / 2] } }), /唯一.*控件/);
  } finally { await f.cleanup(); }
});
test('agreement or account change cannot promote a cache', async () => {
  const f = await fixture('<p>暂无数据</p>');
  try {
    let writes = 0; f.engine.agents.set(f.id, { flushCache: async () => writes++, destroy: async () => {} });
    await f.page.locator('header').evaluate(e => { e.textContent = 'OTHER-ACCOUNT'; });
    await assert.rejects(f.engine.commitCache(f.id), /账号/); assert.equal(writes, 0);
  } finally { await f.cleanup(); }
  await assert.rejects(fixture('<div role="dialog">用户协议，请勾选同意</div>'), /协议/);
});
test('cached click without actual form cannot be accepted; bounded recovery is required', async () => {
  const f = await fixture('<p>暂无数据</p>');
  try {
    let calls = 0;
    f.engine.agents.set(f.id, { aiAct: async (_goal, opts) => {
      calls++;
      if (opts.cacheable === false) await f.page.evaluate(() => {
        document.body.insertAdjacentHTML('beforeend', '<label>版权<input aria-label="版权"></label>');
      });
    }, flushCache: async () => {}, destroy: async () => {}, metrics: { calls: 0, totalTokens: 0 } });
    const result = await f.engine.ensureUploadForm(f.id);
    assert.equal(result.verified, true); assert.equal(calls, 2); assert.equal(f.engine.navigation, false);
  } finally { await f.cleanup(); }
});
test('episode inventory crosses 10/11 boundary and only reports complete after terminal evidence', async () => {
  const body = `<p>上传集数/总集数：11/20</p><table><tbody id="rows"></tbody></table><button aria-label="下一页" onclick="show(2)">下一页</button><script>
    function show(p){document.querySelector('#rows').innerHTML=(p===1?Array.from({length:10},(_,i)=>i+1):[11]).map(n=>'<tr><td>第'+n+'集</td><td>已上传</td></tr>').join('');document.querySelector('button').disabled=p===2}show(1)</script>`;
  const f = await fixture(body);
  try {
    await f.page.goto('https://kdj.kuaishou.com/home/content/content-management/detail?miniSeriesId=TEST');
    const inventory = await f.engine.readInventory(f.id, 'TEST');
    assert.equal(inventory.complete, true); assert.equal(inventory.pages, 2); assert.equal(inventory.count, 11);
    assert.ok(inventory.page.rows.some(r => r.text.includes('第11集')));
    await assert.rejects(f.engine.readInventory(f.id, 'OTHER'), /ID/);
  } finally { await f.cleanup(); }
});

const pythonExe = process.env.MIDSCENE_TEST_PYTHON || fileURLToPath(new URL('../../.venv/Scripts/python.exe', import.meta.url));
test('real Python transport starts the worker, reads the same CDP tab and closes cleanly', { skip: !existsSync(pythonExe) }, async () => {
  const f = await fixture(batchFixtureBody());
  try {
    await batchSpecs(f);
    const code = `import asyncio,json,sys,pathlib\nfrom kuaishou_upload.midscene_transport import MidsceneTransport\nasync def main():\n p=json.load(sys.stdin)\n t=MidsceneTransport(pathlib.Path(p['cacheFile']).parent,timeout=15)\n try:\n  await t.start()\n  init=await t.call('init',**p)\n  snap=await t.call('snapshot',pageId=init['pageId'])\n  hashes=[s['sha256'] for s in p['batchFiles']]\n  await t.call('prepare_batch',pageId=init['pageId'],hashes=hashes,totalCards=3,newSeries=True)\n  await t.call('verify_batch',pageId=init['pageId'],hashes=hashes)\n  selected=await t.call('batch_files',pageId=init['pageId'],hashes=hashes)\n  print(json.dumps({'pageId':init['pageId'],'accountText':snap['accountText'],'selected':selected['selected']},ensure_ascii=False),flush=True)\n finally:\n  await t.close()\nasyncio.run(asyncio.wait_for(main(),25))`;
    const proc = spawn(pythonExe, ['-u', '-c', code], { cwd: fileURLToPath(new URL('../../', import.meta.url)),
      windowsHide: true, stdio: ['pipe', 'pipe', 'pipe'], env: { ...process.env, PYTHONIOENCODING: 'utf-8' } });
    let output = '', error = '';
    proc.stdout.on('data', b => { output += b; }); proc.stderr.on('data', b => { error += b; });
    proc.stdin.end(JSON.stringify(f.engine.config));
    const exitCode = await new Promise((resolve, reject) => { proc.on('error', reject); proc.on('exit', resolve); });
    assert.equal(exitCode, 0, error);
    assert.equal(JSON.parse(output).pageId, f.id); assert.match(output, /TARGET-ACCOUNT/);
    assert.equal(JSON.parse(output).selected,3);
    assert.equal(await f.page.evaluate(()=>window.selectionCalls),1);
    assert.equal(f.page.isClosed(), false); assert.equal(browser.isConnected(), true);
    assert.ok(!/unclosed transport|I\/O operation on closed pipe/.test(error), error);
  } finally { await f.cleanup(); }
});
