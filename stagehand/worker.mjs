import readline from 'node:readline';
import {Stagehand} from '@browserbasehq/stagehand';
import {UploadEngine} from './engine.mjs';
import {assertUrl} from './policy.mjs';

const output=process.stdout.write.bind(process.stdout);
const secrets=[process.env.STAGEHAND_API_KEY,process.env.OPENAI_API_KEY].filter(Boolean);
const redact=value=>{let s=String(value);for(const key of secrets)s=s.split(key).join('[redacted]');return s;};
for(const method of ['log','info','debug','warn'])console[method]=(...args)=>process.stderr.write(redact(args.join(' '))+'\n');
let stagehand,engine;
const modelName=process.env.STAGEHAND_MODEL||process.env.OPENAI_MODEL||'deepseek-flash-free';
const model={modelName:modelName.includes('/')?modelName:'openai/'+modelName,
  apiKey:process.env.STAGEHAND_API_KEY||process.env.OPENAI_API_KEY,
  baseURL:process.env.STAGEHAND_BASE_URL||process.env.OPENAI_BASE_URL,
  openaiEndpointFormat:'chat',maxRetries:0,timeout:30000};
async function handle(method,p) {
  if(method==='init') {
    if(engine)throw new Error('Worker已经初始化');
    const url=new URL(p.cdpUrl);
    if(!['127.0.0.1','localhost','[::1]'].includes(url.hostname))throw new Error('仅连接本任务的本机浏览器');
    stagehand=new Stagehand({env:'LOCAL',model,experimental:true,selfHeal:false,
      localBrowserLaunchOptions:{cdpUrl:p.cdpUrl},disablePino:true,verbose:0,
      logger:line=>process.stderr.write(redact(JSON.stringify(line))+'\n')});
    await stagehand.init();
    engine=new UploadEngine(stagehand,{...p,model});return await engine.initialize();
  }
  if(!engine)throw new Error('Worker尚未初始化');
  switch(method) {
    case 'snapshot':return await engine.snapshot(p.pageId);
    case 'evaluate':return await engine.evaluate(p.pageId,p.source,p.args);
    case 'get_url':return (await engine.snapshot(p.pageId)).url;
    case 'goto':return await engine.goto(p.pageId,p.url);
    case 'focus':stagehand.context.setActivePage(engine.page(p.pageId));return true;
    case 'new_page':return await engine.newPage(p.url);
    case 'close_page':await engine.closePage(p.pageId);return true;
    case 'selectors':return await engine.selectors(p.pageId,p.selector);
    case 'click':return await engine.click(p.pageId,p.ref,{final:p.final===true});
    case 'files':return await engine.files(p.pageId,p.ref,p.asset);
    case 'screenshot':return (await engine.page(p.pageId).screenshot()).toString('base64');
    case 'ensure_upload_form':return await engine.ensureUploadForm(p.pageId,{maxSteps:p.maxSteps});
    case 'field_binding':return await engine.fieldBinding(p.pageId,p.name,p.value,p.selected);
    case 'resolve_intent':return await engine.resolveIntent(p.pageId,p.intent,p.aliases);
    case 'close':await stagehand.close();return true;
    default:throw new Error('未知Stagehand RPC操作');
  }
}
const rl=readline.createInterface({input:process.stdin,crlfDelay:Infinity});
// Serial requests prevent snapshot/ref races. On Python timeout this worker is terminated,
// never silently retried while an old page action could still run.
for await(const line of rl) {
  let request;
  try {request=JSON.parse(line);const result=await handle(request.method,request.params||{});output(JSON.stringify({id:request.id,result})+'\n');}
  catch(error){const code=error.code||(/API|LLM|model|401|402|429|quota|credit|ECONNREFUSED|ETIMEDOUT/i.test(error.message)?'model_error':'stagehand_error');output(JSON.stringify({id:request?.id,error:{code,message:redact(error.message)}})+'\n');}
  if(request?.method==='close')break;
}
process.exit(0);
