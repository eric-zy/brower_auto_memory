// Deterministic batch operations. Values and paths only come from the scanned job allowlist.
import { createHash } from 'node:crypto';
import { createReadStream } from 'node:fs';

export async function cards(engine, id) {
  const snap = await engine.snapshot(id);
  if (snap.truncated || snap.loading) throw Error('批次页面仍在加载或控件快照不完整');
  const frame = engine.refsForFrame(id, snap.activeFrameId);
  const result = await frame.evaluate(controlLabels => {
    const visible = e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0 && getComputedStyle(e).visibility !== 'hidden'; };
    const label = e => controlLabels[e.dataset.ksUploadRef] || e.labels?.[0]?.innerText || e.getAttribute('aria-label') || e.placeholder || '';
    const inputs = [...document.querySelectorAll('input,textarea')].filter(e => visible(e) && !['radio','checkbox','file','hidden'].includes(e.type) && /集数标题|单集标题|剧集标题|单集名称/.test(label(e)));
    return inputs.map(input => {
      let box = input.parentElement, found;
      for (; box && box !== document.body; box = box.parentElement) {
        if (inputs.filter(e => box.contains(e)).length !== 1) break;
        const paidControls=[...box.querySelectorAll('input[type=radio],input[type=checkbox]')].filter(e=>/单集付费|是否付费|^(免费|付费)$/.test(label(e).trim()));
        if ((/第\s*\d+\s*集/.test(box.innerText) || box.hasAttribute('data-episode-number')) && paidControls.length) { found = box; break; }
      }
      if (!found) throw Error('无法核实单集卡片实际集号，停止批量填表');
      const match = found.innerText.match(/第\s*(\d+)\s*集/);
      const numberInputs = [...found.querySelectorAll('input,select')].filter(e=>/^(集号|单集序号|集数序号)$/.test(label(e).trim()));
      if (numberInputs.length > 1) throw Error('单集卡片集号控件不唯一');
      const numberInput = numberInputs[0];
      const number = Number(numberInput?.value || found.getAttribute('data-episode-number') || match?.[1]);
      const refs = [...found.querySelectorAll('[data-ks-upload-ref]')].map(e => e.dataset.ksUploadRef);
      return { number, numberRef:numberInput?.dataset.ksUploadRef, titleRef: input.dataset.ksUploadRef, value: input.value, refs };
    });
  }, Object.fromEntries(snap.controls.map(n=>[n.ref,n.label])));
  if (new Set(result.map(c => c.number)).size !== result.length) throw Error('单集卡片集号重复');
  return { snap, frame, cards: result };
}

function specs(engine, hashes) {
  const allowed = engine.config.batchFiles || [];
  if (!Array.isArray(hashes) || !hashes.length || new Set(hashes).size !== hashes.length) throw Error('批次文件身份无效');
  return hashes.map(sha256 => {
    const match = allowed.filter(s => s.sha256 === sha256);
    if (match.length !== 1) throw Error('视频不在扫描批次白名单');
    return match[0];
  }).sort((a,b) => a.number - b.number);
}

async function setValue(engine, id, ref, value) {
  const b = await engine.bound(id, ref);
  if (!['input','textarea','select'].includes(b.current.tag) || /file|password|hidden/.test(b.current.type) || b.node.readOnly) throw Error('批次字段不可写');
  await b.locator.evaluate((e, value) => {
    if (e.type === 'radio' || e.type === 'checkbox') e.checked = !!value;
    else {
      const proto = e.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : e.tagName === 'SELECT' ? HTMLSelectElement.prototype : HTMLInputElement.prototype;
      Object.getOwnPropertyDescriptor(proto,'value').set.call(e,String(value));
    }
    e.dispatchEvent(new Event('input',{bubbles:true})); e.dispatchEvent(new Event('change',{bubbles:true})); e.blur();
  }, value);
  const valid = await b.locator.evaluate((e,v) => typeof v === 'boolean' ? e.checked === v : e.value === String(v), value);
  if (!valid) throw Error('批次字段回读不一致');
}

async function addCards(engine, id, difference, before) {
  let snap = await engine.snapshot(id);
  let nodes = snap.controls.filter(n => /^(\+\s*)?(添加单集信息|新增集数|添加剧集)$/.test(n.text.trim()) && !n.disabled
    && (['button','a'].includes(n.tag) || n.type === 'button' || n.semanticClickable));
  if (nodes.length !== 1) {
    const located = await engine.resolveIntent(id,'add_episode',['新增集数','添加单集信息','+ 添加单集信息','添加剧集']);
    snap = await engine.snapshot(id);
    nodes = snap.controls.filter(n=>n.ref===located?.ref&&!n.disabled);
  }
  if (nodes.length !== 1) throw Error('未能从当前页面或视觉恢复确认添加单集入口，未选择任何视频');
  await engine.click(id,nodes[0].ref);
  for(let retry=0;retry<20;retry++){
    snap=await engine.snapshot(id);
    if(snap.dialogs.length)break;
    if((await cards(engine,id)).cards.length>before)return;
    await engine.page(id).waitForTimeout(100);
  }
  const dialogText = snap.dialogs.join('\n');
  if (!dialogText) throw Error('添加入口点击后未出现卡片或已识别的弹窗，禁止重复点击');
  if (!/添加单集|添加集数|新增集数/.test(dialogText) || /协议|授权|发布|提交/.test(dialogText)) throw Error('未知添加集数弹窗，停止');
  const fields = snap.controls.filter(n => n.region==='dialog' && ['number','text'].includes(n.type) && !n.disabled && /添加集数|新增集数|集数数量/.test(n.label+' '+n.context));
  if (fields.length !== 1) throw Error('无法唯一核实添加集数输入框');
  await setValue(engine,id,fields[0].ref,String(difference));
  snap = await engine.snapshot(id);
  const confirms = snap.controls.filter(n => n.region === 'dialog' && /^(确定|确认)$/.test(n.text.trim()) && n.tag === 'button' && !n.disabled);
  if (confirms.length !== 1) throw Error('无法唯一核实添加集数确认按钮');
  await engine.click(id,confirms[0].ref);
  for(let retry=0;retry<20;retry++){
    const after=await engine.snapshot(id);
    if(!after.dialogs.length)return;
    await engine.page(id).waitForTimeout(100);
  }
  throw Error('确认添加后弹窗未关闭，结果待核对，禁止再次确认');
}

export async function prepareBatch(engine,id,hashes,totalCards,newSeries=false) {
  const jobs = specs(engine,hashes);
  if (!Number.isInteger(totalCards) || totalCards < jobs.length || totalCards > 600) throw Error('批次卡片数量无效');
  let current = await cards(engine,id);
  for (let attempt=0; current.cards.length < totalCards && attempt < totalCards; attempt++) {
    const before = current.cards.length;
    await addCards(engine,id,totalCards-before,before);
    for (let retry=0; retry<20; retry++) {
      current = await cards(engine,id);
      if (current.cards.length > before) break;
      await engine.page(id).waitForTimeout(100);
    }
    if (current.cards.length <= before) throw Error('添加单集后未出现新卡片，禁止重复添加');
  }
  if (current.cards.length !== totalCards) throw Error('批次卡片数量与预期不一致，不自动删除旧卡片');
  if (newSeries && current.cards.length === jobs.length && current.cards.every(c=>c.numberRef)) {
    const refs=current.cards.map(c=>c.numberRef);
    for (let i=0;i<jobs.length;i++) await setValue(engine,id,refs[i],String(jobs[i].number));
    current=await cards(engine,id);
  }
  const bindings = [];
  // Validate the entire batch BEFORE writing any episode fields.
  for (const job of jobs) if (current.cards.filter(c => c.number === job.number).length !== 1) {
    throw Error(`平台卡片中没有第${job.number}集；已进入新建表单，但不能将原集号静默改为第1集。请核对该剧前序集数及平台集号能力`);
  }
  for (const job of jobs) {
    current = await cards(engine,id);
    const card = current.cards.find(c => c.number === job.number);
    let inCard = current.snap.controls.filter(n => card.refs.includes(n.ref));
    await setValue(engine,id,card.titleRef,job.title);
    // The accessible radio wrapper and its native input represent one option.
    // Only a native input can be written by setValue; never choose both.
    const paid = inCard.filter(n => n.tag==='input' && ['radio','checkbox'].includes(n.type) && (n.type === 'checkbox' ? /单集付费|是否付费/.test(n.label) : [n.text.trim(),n.label.trim(),String(n.value)].includes(job.paid ? '付费' : '免费')));
    if (paid.length !== 1) throw Error(`第${job.number}集付费选项无法唯一核实`);
    await setValue(engine,id,paid[0].ref,true === job.paid || paid[0].type === 'radio');
    const entry = { number:job.number, titleRef:card.titleRef, paidRef:paid[0].ref, paidChecked:true === job.paid || paid[0].type === 'radio' };
    if (job.paid) {
      // Selecting paid can render the price input reactively.
      current=await cards(engine,id);
      const refreshed=current.cards.find(c=>c.number===job.number);
      if(!refreshed)throw Error('切换付费后单集卡片身份已变化');
      inCard=current.snap.controls.filter(n=>refreshed.refs.includes(n.ref));
      const prices = inCard.filter(n => ['text','number'].includes(n.type) && /单集价格|本集价格/.test(n.label+' '+n.context));
      if (prices.length !== 1) throw Error(`第${job.number}集价格无法唯一核实`);
      await setValue(engine,id,prices[0].ref,String(job.price)); entry.priceRef=prices[0].ref;
    }
    bindings.push(entry);
  }
  engine.batchBinding = { hashes:[...hashes].sort(), totalCards, bindings };
  await verifyBatch(engine,id,hashes);
  return { bindings, count:totalCards };
}

export async function verifyBatch(engine,id,hashes) {
  const jobs=specs(engine,hashes), binding=engine.batchBinding;
  if (!binding || JSON.stringify([...hashes].sort()) !== JSON.stringify(binding.hashes)) throw Error('缺少当前批次卡片校验记录');
  const current=await cards(engine,id);
  if (current.cards.length !== binding.totalCards) throw Error('批次卡片数量已变化');
  for (const job of jobs) {
    const card=current.cards.find(c=>c.number===job.number), saved=binding.bindings.find(b=>b.number===job.number);
    if (!card || card.titleRef !== saved.titleRef || card.value !== job.title) throw Error('批次集号或标题已变化');
    const paid=current.snap.controls.find(n=>n.ref===saved.paidRef);
    if (!paid || paid.checked !== saved.paidChecked) throw Error('批次付费状态已变化');
    if (job.paid && current.snap.controls.find(n=>n.ref===saved.priceRef)?.value !== String(job.price)) throw Error('批次价格已变化');
  }
  return true;
}

export async function selectBatchFiles(engine,id,hashes) {
  const jobs=specs(engine,hashes);
  if (engine.batchSelectionStarted) throw Error('该批次已选择过文件，结果不明时必须先核对，禁止重传');
  if (!engine.batchBinding || JSON.stringify([...hashes].sort()) !== JSON.stringify(engine.batchBinding.hashes)) throw Error('缺少批次表单核验');
  for (const job of jobs) {
    const digest=createHash('sha256');
    for await (const chunk of createReadStream(job.path)) digest.update(chunk);
    const actual=digest.digest('hex');
    if (actual !== job.sha256) throw Error('扫描后视频发生变化，停止整个批次');
  }
  const snap=await engine.snapshot(id);
  const nodes=snap.controls.filter(n=>n.type==='file'&&!n.disabled&&/视频|video/i.test(n.label));
  const bulk=[];
  for (const n of nodes) {
    const b=await engine.bound(id,n.ref);
    if (await b.locator.evaluate(e=>e.multiple && (!e.accept || e.accept.split(',').some(v=>['.mp4','video/mp4','video/*','*/*'].includes(v.trim().toLowerCase()))))) bulk.push(b);
  }
  if (bulk.length!==1) throw Error('未找到唯一支持多选的视频导入控件，保留批次，未选择任何视频');
  engine.batchSelectionStarted=true;
  await bulk[0].locator.setInputFiles(jobs.map(j=>j.path));
  return { selected:jobs.length, filenames:jobs.map(j=>j.filename) };
}
