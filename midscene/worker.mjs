import readline from 'node:readline';
import path from 'node:path';
import { runtimeLimits } from './runtime-limits.mjs';

const output = process.stdout.write.bind(process.stdout);
const secrets = [process.env.OPENAI_API_KEY, process.env.MIDSCENE_MODEL_API_KEY].filter(Boolean);
const redact = value => secrets.reduce((s, secret) => s.split(secret).join('[redacted]'), String(value));
for (const method of ['log', 'info', 'debug', 'warn', 'error']) console[method] = (...args) => process.stderr.write(redact(args.join(' ')) + '\n');
process.env.MIDSCENE_MODEL_NAME ||= process.env.OPENAI_MODEL || 'deepseek-flash-free';
process.env.MIDSCENE_MODEL_API_KEY ||= process.env.OPENAI_API_KEY;
process.env.MIDSCENE_MODEL_BASE_URL ||= process.env.OPENAI_BASE_URL;
if (!process.env.MIDSCENE_MODEL_FAMILY && /deepseek/i.test(process.env.MIDSCENE_MODEL_NAME)) process.env.MIDSCENE_MODEL_FAMILY = 'deepseek';
if (process.env.MIDSCENE_MODEL_FAMILY === 'deepseek') process.env.MIDSCENE_MODEL_REASONING_ENABLED ||= 'true';
const limits = runtimeLimits();
process.env.MIDSCENE_MODEL_TIMEOUT = String(limits.requestMs);
if (!limits.requestMs) {
  for (const slot of ['PLANNING','LOCATE','INSIGHT']) process.env[`MIDSCENE_${slot}_MODEL_TIMEOUT`] = '0';
}
process.env.MIDSCENE_MODEL_RETRY_COUNT ||= '0';
process.env.MIDSCENE_PREFERRED_LANGUAGE = 'Chinese';
const { MidsceneEngine } = await import('./engine.mjs');
let engine;
async function handle(method, p) {
  if (method === 'init') {
    if (engine) throw Error('Worker已经初始化');
    process.env.MIDSCENE_RUN_DIR = path.join(path.dirname(p.cacheFile), 'runs', `${Date.now()}-${process.pid}`);
    engine = new MidsceneEngine(p); return engine.initialize();
  }
  if (!engine) throw Error('Worker尚未初始化');
  switch (method) {
    case 'snapshot': return engine.snapshot(p.pageId);
    case 'evaluate': return engine.evaluate(p.pageId, p.source, p.args);
    case 'get_url': return (await engine.snapshot(p.pageId)).url;
    case 'goto': return engine.goto(p.pageId, p.url);
    case 'new_page': return engine.newPage(p.url);
    case 'close_page': await engine.closePage(p.pageId); return true;
    case 'focus': await engine.page(p.pageId).bringToFront(); return true;
    case 'selectors': return engine.selectors(p.pageId, p.selector);
    case 'click': return engine.click(p.pageId, p.ref, { final: p.final === true });
    case 'files': return engine.files(p.pageId, p.ref, p.asset);
    case 'prepare_batch': return engine.prepareBatch(p.pageId, p.hashes, p.totalCards, p.newSeries === true);
    case 'verify_batch': return engine.verifyBatch(p.pageId, p.hashes);
    case 'batch_files': return engine.selectBatchFiles(p.pageId, p.hashes);
    case 'screenshot': return (await engine.page(p.pageId).screenshot()).toString('base64');
    case 'ensure_upload_form': return engine.ensureUploadForm(p.pageId);
    case 'field_binding': return engine.fieldBinding(p.pageId, p.name, p.value, p.selected);
    case 'resolve_intent': return engine.resolveIntent(p.pageId, p.intent, p.aliases);
    case 'commit_cache': return engine.commitCache(p.pageId);
    case 'read_inventory': return engine.readInventory(p.pageId, p.contentId);
    case 'close': await engine.close(); return true;
    default: throw Error('未知Midscene RPC操作');
  }
}
for await (const line of readline.createInterface({ input: process.stdin, crlfDelay: Infinity })) {
  let request;
  try { request = JSON.parse(line); output(JSON.stringify({ id: request.id, result: await handle(request.method, request.params || {}) }) + '\n'); }
  catch (error) {
    const known = ['wrong_domain', 'login', 'challenge', 'agreement', 'account_mismatch', 'permission_denied', 'page_error', 'form_not_verified', 'midscene_model_error'];
    const code = known.includes(error.code) ? error.code
      : /API|LLM|model|401|402|429|quota|credit|ECONNREFUSED|ETIMEDOUT|timeout/i.test(error.message) ? 'midscene_model_error' : 'midscene_error';
    output(JSON.stringify({ id: request?.id, error: { code, message: redact(error.message) } }) + '\n');
  }
  if (request?.method === 'close') break;
}
process.exit(0);
