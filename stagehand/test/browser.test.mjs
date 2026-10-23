import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import net from 'node:net';
import http from 'node:http';
import readline from 'node:readline';
import {spawn} from 'node:child_process';
import {fileURLToPath} from 'node:url';
import {chromium} from 'playwright';
import {Stagehand} from '@browserbasehq/stagehand';
import {UploadEngine} from '../engine.mjs';
import {BUILTIN_TOOLS} from '../policy.mjs';

test('Stagehand real-browser regression: cross-origin frame, guarded fallback, file routing and verified replay', {timeout:90000}, async t=>{
  const temp=await fs.mkdtemp(path.join(os.tmpdir(),'stagehand-test-'));
  const socket=net.createServer();await new Promise(r=>socket.listen(0,'127.0.0.1',r));const port=socket.address().port;await new Promise(r=>socket.close(r));
  let modelCalls=0,modelHandler;
  const modelServer=http.createServer(async(req,res)=>{
    modelCalls++;
    if(!modelHandler){res.writeHead(500);res.end('No model calls allowed in deterministic fixture tests');return;}
    try {let body='';for await(const chunk of req)body+=chunk;const message=await modelHandler(JSON.parse(body));
      res.writeHead(200,{'content-type':'application/json'});res.end(JSON.stringify({id:'fixture-'+modelCalls,object:'chat.completion',created:1,model:'gpt-4.1-mini',choices:[{index:0,message,finish_reason:message.tool_calls?'tool_calls':'stop'}],usage:{prompt_tokens:1,completion_tokens:1,total_tokens:2}}));
    } catch(error){res.writeHead(500);res.end(String(error.stack));}
  });
  await new Promise(r=>modelServer.listen(0,'127.0.0.1',r));
  const profile=path.join(temp,'profile');
  let context,stagehand;
  try {
    context=await chromium.launchPersistentContext(profile,{headless:true,executablePath:process.env.TEST_CHROME_PATH||'C:/Program Files/Google/Chrome/Application/chrome.exe',
      args:[`--remote-debugging-port=${port}`,'--disable-background-networking','--no-first-run'],viewport:{width:1280,height:1000}});
    await context.route('**/*',async route=>{
      const u=new URL(route.request().url());let body='';
      if(u.hostname==='fixture.test')body=`<header>测试账号</header><button id="side" onclick="document.querySelector('iframe').src='https://embedded.fixture.test/list'">内容</button><iframe style="width:1000px;height:800px;border:0" src="${u.pathname==='/fallback'?'about:blank':'https://embedded.fixture.test/list'}"></iframe>`;
      else if(u.hostname==='embedded.fixture.test'&&u.pathname==='/list')body='<h1>内容管理</h1><label>短剧标题<input placeholder="短剧标题"></label><button>搜索</button><button id="upload" onclick="location.href=\'/form\'">内容上传</button>';
      else if(u.hostname==='embedded.fixture.test'&&u.pathname==='/form')body='<h1>版权与备案 单集信息</h1><label>剧名<input id="title"></label><label>视频<input id="video" type="file" accept="video/mp4"></label><button id="final" onclick="window.didSubmit=true">提交审核</button>';
      else return route.abort();
      await route.fulfill({status:200,contentType:'text/html; charset=utf-8',body});
    });
    const pw=context.pages()[0];await pw.goto('https://fixture.test/main');
    const version=await(await fetch(`http://127.0.0.1:${port}/json/version`)).json();
    const model={modelName:'openai/gpt-4.1-mini',apiKey:'local-fixture-only',baseURL:`http://127.0.0.1:${modelServer.address().port}/v1`,openaiEndpointFormat:'chat'};
    stagehand=new Stagehand({env:'LOCAL',model,experimental:true,selfHeal:false,disablePino:true,verbose:0,
      localBrowserLaunchOptions:{cdpUrl:version.webSocketDebuggerUrl},logger:()=>{}});
    await stagehand.init();
    const page=stagehand.context.pages().find(p=>p.url().startsWith('https://fixture.test/'));
    assert.ok(page,'must attach to correct task tab');
    const observation=await fs.readFile(new URL('../../kuaishou_upload/observation.py',import.meta.url),'utf8');
    const snapshotScript=observation.match(/SNAPSHOT = r"""([\s\S]*?)"""/)[1];
    const video=path.join(temp,'video.mp4');await fs.writeFile(video,'LOCAL PLACEHOLDER ONLY');
    const engine=new UploadEngine(stagehand,{targetId:page.targetId(),allowedHosts:['fixture.test','embedded.fixture.test'],snapshotScript,files:{video},cacheFile:path.join(temp,'cache.json'),model});
    await engine.initialize();const id=page.targetId();
    const settle=async predicate=>{let last;for(let i=0;i<40;i++){const s=await engine.snapshot(id);last=s;if(predicate(s))return s;await new Promise(r=>setTimeout(r,100));}throw new Error('fixture did not settle: '+JSON.stringify({frames:last.frames,text:last.text,controls:last.controls.map(n=>n.text)}));};
    let s=await settle(s=>s.controls.some(n=>n.text==='内容上传'));
    assert.ok(s.frames.some(f=>f.url==='https://embedded.fixture.test/list'&&f.status==='observed'));
    assert.equal(new Set(s.controls.map(n=>n.ref)).size,s.controls.length,'refs must be unique across root/child frames');
    const observe=stagehand.observe.bind(stagehand);
    stagehand.observe=async()=>[{selector:'xpath=//iframe//button[@id="upload"]',description:'内容上传',method:'click',arguments:[]}];
    const first=await engine.ensureUploadForm(id);
    assert.equal(first.strategy,'observe_act');assert.equal(first.verified,true);
    s=await settle(s=>s.controls.some(n=>n.type==='file'));
    const file=s.controls.find(n=>n.type==='file');
    await engine.files(id,file.ref,'video');
    const frame=engine.refs.get(file.ref).frame;
    assert.equal(await frame.evaluate('document.querySelector("#video").files[0].name'),'video.mp4');
    const title=s.controls.find(n=>n.tag==='input'&&n.type==='text');
    await engine.evaluate(id,'(ref,value)=>{document.querySelector(`[data-ks-upload-ref="${ref}"]`).value=value;return true;}',[title.ref,'本地测试标题']);
    assert.equal(await frame.evaluate('document.querySelector("#title").value'),'本地测试标题');
    const final=s.controls.find(n=>n.text==='提交审核');
    await assert.rejects(engine.click(id,final.ref,{navigation:true}),/允许范围/);
    assert.equal(await frame.evaluate('Boolean(window.didSubmit)'),false);
    await assert.rejects(engine.files(id,file.ref,'copyright'),/白名单/);
    await page.goto('https://fixture.test/main');await settle(s=>s.controls.some(n=>n.text==='内容上传'));
    stagehand.observe=async()=>{throw new Error('verified replay must not call model');};
    assert.equal((await engine.ensureUploadForm(id)).strategy,'verified_replay');
    await page.goto('https://fixture.test/fallback');await settle(s=>s.controls.some(n=>n.text==='内容'));
    engine.cache={};stagehand.observe=async()=>[];
    const nativeAgent=stagehand.agent.bind(stagehand);let fallbackUsed=false;
    stagehand.agent=config=>{
      assert.deepEqual(Object.keys(config.tools).sort(),['click_upload_control','inspect_upload_page','scroll_upload_page','wait_upload_page'].sort());
      // Construct the actual SDK agent too: proves the custom tool schema is accepted.
      const instance=nativeAgent(config);assert.equal(typeof instance.execute,'function');
      return {execute:async options=>{
        fallbackUsed=true;assert.deepEqual(options.excludeTools,BUILTIN_TOOLS);
        const preparation=await options.callbacks.prepareStep();assert.deepEqual(preparation.activeTools,Object.keys(config.tools));
        let view=await config.tools.inspect_upload_page.execute({});
        await config.tools.click_upload_control.execute({ref:view.controls.find(n=>n.text==='内容').ref});
        await settle(s=>s.controls.some(n=>n.text==='内容上传'));
        view=await config.tools.inspect_upload_page.execute({});
        await config.tools.click_upload_control.execute({ref:view.controls.find(n=>n.text==='内容上传').ref});
      }};
    };
    const result=await engine.ensureUploadForm(id);assert.equal(result.strategy,'guarded_agent');assert.ok(fallbackUsed);
    assert.equal(result.verified,true);assert.equal(modelCalls,0,'fixture must never call a real or fake model endpoint');
    const cache=JSON.parse(await fs.readFile(path.join(temp,'cache.json'),'utf8'));
    assert.ok(!JSON.stringify(cache).includes('"ref"'),'cache stores semantic identity, not stale refs');
    // Run the actual Stagehand Agent loop against an isolated OpenAI-compatible
    // fixture server. No provider API or API key is used.
    stagehand.agent=nativeAgent;engine.cache={};
    await page.goto('https://fixture.test/fallback');await settle(s=>s.controls.some(n=>n.text==='内容'));
    modelHandler=async body=>{
      const names=(body.tools||[]).map(t=>t.function.name);
      assert.ok(names.every(n=>['inspect_upload_page','click_upload_control','wait_upload_page','scroll_upload_page','done'].includes(n)), 'agent must never receive unrestricted SDK tools');
      let name,args;
      if(names.includes('done')){name='done';args={reasoning:'Fixture form verified',taskComplete:true};}
      else {
        const current=await engine.snapshot(id);
        if(engine.formReady(current))return {role:'assistant',content:'Verified form reached'};
        const target=current.controls.find(n=>n.text==='内容上传')||current.controls.find(n=>n.text==='内容');
        name='click_upload_control';args={ref:target.ref};
      }
      return {role:'assistant',content:null,tool_calls:[{id:'tool-'+modelCalls,type:'function',function:{name,arguments:JSON.stringify(args)}}]};
    };
    const actual=await engine.ensureUploadForm(id);
    assert.equal(actual.verified,true);assert.equal(actual.strategy,'guarded_agent');assert.ok(modelCalls>=3);
    // Exercise the actual worker stdio protocol on the same isolated browser.
    const child=spawn(process.execPath,[fileURLToPath(new URL('../worker.mjs',import.meta.url))],{windowsHide:true,
      stdio:['pipe','pipe','pipe'],env:{...process.env,STAGEHAND_API_KEY:'local-fixture-only',STAGEHAND_MODEL:'openai/gpt-4.1-mini',STAGEHAND_BASE_URL:model.baseURL}});
    const pending=new Map();let counter=0,stderr='';
    child.stderr.on('data',b=>{stderr=(stderr+b.toString()).slice(-4000);});
    const lines=readline.createInterface({input:child.stdout});
    lines.on('line',line=>{let r;try{r=JSON.parse(line);}catch{return;}const p=pending.get(r.id);if(p){pending.delete(r.id);clearTimeout(p.timer);r.error?p.reject(new Error(r.error.message)):p.resolve(r.result);}});
    const rpc=(method,params={})=>new Promise((resolve,reject)=>{const requestId=++counter;const timer=setTimeout(()=>reject(new Error('worker timeout: '+stderr)),15000);pending.set(requestId,{resolve,reject,timer});child.stdin.write(JSON.stringify({id:requestId,method,params})+'\n');});
    try {
      const opened=await rpc('init',{cdpUrl:version.webSocketDebuggerUrl,targetId:id,allowedHosts:['fixture.test','embedded.fixture.test'],snapshotScript,files:{video},cacheFile:path.join(temp,'worker-cache.json')});
      assert.equal(opened.pageId,id);
      const observed=await rpc('snapshot',{pageId:id});assert.ok(observed.frames.some(f=>f.url.includes('/form')));
      const input=observed.controls.find(n=>n.tag==='input'&&n.type==='text');
      assert.equal(await rpc('evaluate',{pageId:id,source:'(ref)=>document.querySelector(`[data-ks-upload-ref="${ref}"]`).id',args:[input.ref]}),'title');
      await rpc('close');
    } finally {lines.close();for(const p of pending.values())clearTimeout(p.timer);child.kill();}
    stagehand.observe=observe;
  } finally {
    if(stagehand)await stagehand.close().catch(()=>{});
    if(context)await context.close().catch(()=>{});
    await new Promise(r=>modelServer.close(r));
    const absolute=path.resolve(temp);
    if(path.dirname(absolute)===path.resolve(os.tmpdir())&&path.basename(absolute).startsWith('stagehand-test-'))await fs.rm(absolute,{recursive:true,force:true,maxRetries:5});
  }
});
