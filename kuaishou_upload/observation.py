"""Shared DOM observation and bounded model context."""
SNAPSHOT = r"""() => {
 const visible=e=>{const r=e.getBoundingClientRect();const s=getComputedStyle(e);return r.width>0&&r.height>0&&s.visibility!=='hidden'&&s.display!=='none'};
 const semanticClickable=e=>{
   if(!e.matches('div,span')||e.closest('button,a,li,label,[role=button],[role=menuitem],[role=radio],[role=checkbox],[role=option],[aria-disabled=true]'))return false;
   const text=(e.innerText||'').trim();if(!text||text.length>100||(!e.onclick&&getComputedStyle(e).cursor!=='pointer'))return false;
   return ![...e.children].some(c=>visible(c)&&(c.innerText||'').trim().replace(/^[+\s]+/,'')===text.replace(/^[+\s]+/,'')&&(c.onclick||getComputedStyle(c).cursor==='pointer'));
 };
 let counter=Number(document.documentElement.dataset.ksCounter||0);
 const seen=new Set();
 const ref=e=>{let id=e.dataset.ksUploadRef;if(!id||seen.has(id)){id='k'+(++counter);e.dataset.ksUploadRef=id}seen.add(id);return id};
 const modal=e=>e.closest('[role=dialog],[role=alertdialog],[aria-modal=true],dialog[open],.ks-dialog,.el-dialog');
 // Match a field container token, never its BEM __content / __error descendants.
 const fieldBox=e=>{for(let p=e;p&&p!==document.body;p=p.parentElement){if([...p.classList].some(c=>/(?:^|[-_])form[-_]?item$/i.test(c)))return p}return null};
 const label=e=>{const l=e.labels&&e.labels[0];const box=fieldBox(e);return (e.getAttribute('aria-label')||l?.innerText||box?.querySelector('label')?.innerText||box?.innerText||e.placeholder||modal(e)?.innerText||e.parentElement?.innerText||'').trim().slice(0,180)};
 const name=e=>{const ids=(e.getAttribute('aria-labelledby')||'').split(/\s+/).filter(Boolean);return (e.getAttribute('aria-label')||ids.map(id=>document.getElementById(id)?.innerText||'').join(' ')||e.labels?.[0]?.innerText||e.innerText||e.title||e.getAttribute('alt')||'').trim().slice(0,160)};
 const region=e=>{if(modal(e))return 'dialog';const r=e.closest('form,[role=main],main,header,[role=banner]');return r?(r.getAttribute('aria-label')||r.getAttribute('role')||r.tagName.toLowerCase()):''};
 const elements=[...new Set([...document.querySelectorAll('input:not([type=hidden]),textarea,select,button,a,[role=button],[role=radio],[role=checkbox],[role=option],[role=combobox],[role=menuitem],label,li'),
   ...[...document.querySelectorAll('div,span')].filter(semanticClickable)])]
   .filter(e=>visible(e)||e.type==='file');
 elements.sort((a,b)=>Number(b.matches('input,textarea,select,button,[role=button],[role=combobox]'))-Number(a.matches('input,textarea,select,button,[role=button],[role=combobox]')));
 const controls=elements.slice(0,600).map(e=>({ref:ref(e),tag:e.tagName.toLowerCase(),type:e.type||e.getAttribute('role')||'',
     accessibleName:name(e),region:region(e),visible:visible(e),semanticClickable:semanticClickable(e),
     text:(e.innerText||e.getAttribute('aria-label')||'').trim().slice(0,100),label:label(e),value:e.type==='password'?'[redacted]':(e.value??''),checked:!!e.checked||e.getAttribute('aria-checked')==='true',
     disabled:!!e.disabled,readOnly:!!e.readOnly,accept:e.accept||'',href:e.getAttribute('href')||'',
     placeholder:e.placeholder||'',
     context:(fieldBox(e)?.innerText||modal(e)?.innerText||((e.type==='radio'||e.type==='checkbox')?e.parentElement?.parentElement?.innerText:'')||'').slice(0,160),
     options:e.tagName==='SELECT'?[...e.options].map(o=>({text:o.text,value:o.value})):[],
     inViewport:(()=>{let r=e.getBoundingClientRect();return r.bottom>0&&r.top<innerHeight})()}));
 document.documentElement.dataset.ksCounter=String(counter);
 const rows=[...document.querySelectorAll('tr,[role=row],.ks-table__row')].filter(visible).map(e=>({text:e.innerText.slice(0,700),cells:[...e.querySelectorAll('td,[role=cell]')].map(c=>c.innerText.trim()),links:[...e.querySelectorAll('a[href]')].map(a=>a.href),episodeId:e.getAttribute('data-episode-id')||''})).slice(0,80);
 const uploadRows=[...document.querySelectorAll('tr,[role=row],[class*=upload-item],[class*=uploadItem],.ks-upload-list-item,.el-upload-list__item')].filter(e=>visible(e)&&/\.mp4\b/i.test(e.innerText)).map(e=>({text:e.innerText.slice(0,1200),previews:[...e.querySelectorAll('img')].filter(i=>visible(i)&&i.naturalWidth>0).length}));
 return {url:location.href,title:document.title,text:document.body.innerText.slice(0,16000),controls,rows,uploadRows,
   controlCount:elements.length,truncated:elements.length>600,frameCount:document.querySelectorAll('iframe').length,
   accountText:[...document.querySelectorAll('header,[role=banner],.header,.home__header')].filter(visible).map(e=>e.innerText).join('\n').slice(0,2000),
   loading:[...document.querySelectorAll('[aria-busy=true],[role=progressbar],.ks-loading-mask,.el-loading-mask')].some(visible),
   dialogs:[...document.querySelectorAll('[role=dialog],[role=alertdialog],[aria-modal=true],dialog[open],.ks-dialog,.el-dialog')].filter(e=>visible(e)&&!e.parentElement?.closest('[role=dialog],[role=alertdialog],[aria-modal=true],dialog[open],.ks-dialog,.el-dialog')).map(e=>e.innerText.slice(0,1200)),
   previews:[...document.images].filter(e=>visible(e)&&e.naturalWidth>0).length};
}"""


def compact_snapshot(snap):
    controls = []
    for node in snap["controls"]:
        if node["tag"] in {"label", "a", "li"} and node["type"] not in {"option", "radio", "checkbox"}:
            continue
        controls.append({**{key: node.get(key) for key in ("ref", "tag", "type", "accessibleName", "region", "disabled", "readOnly", "checked", "accept", "inViewport")},
                         "label": node.get("label", "")[:100], "text": node.get("text", "")[:70],
                         "value": str(node.get("value", ""))[:140], "options": node.get("options", [])[:20]})
    return {"url": snap["url"], "title": snap.get("title", ""), "pageState": snap.get('pageState'),
            'truncated': snap.get('truncated', False), 'controlCount': snap.get('controlCount', len(snap['controls'])),
            'frameCount': snap.get('frameCount', 0), "text": snap["text"][:6000],
            "controls": controls[:180], "rows": snap.get("rows", [])[:5],
            "uploadRows": snap.get("uploadRows", [])[:5], "dialogs": [item[:500] for item in snap.get("dialogs", [])]}

