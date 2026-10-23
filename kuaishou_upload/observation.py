"""Shared DOM observation and bounded model context."""
SNAPSHOT = r"""() => {
 const visible=e=>{const r=e.getBoundingClientRect();const s=getComputedStyle(e);return r.width>0&&r.height>0&&s.visibility!=='hidden'&&s.display!=='none'};
 let counter=Number(document.documentElement.dataset.ksCounter||0);
 const seen=new Set();
 const ref=e=>{let id=e.dataset.ksUploadRef;if(!id||seen.has(id)){id='k'+(++counter);e.dataset.ksUploadRef=id}seen.add(id);return id};
 const label=e=>{const l=e.labels&&e.labels[0];const box=e.closest('.ks-form-item,.el-form-item,[class*=form-item],[class*=formItem]');return (e.getAttribute('aria-label')||l?.innerText||box?.innerText||e.placeholder||e.parentElement?.innerText||'').trim().slice(0,180)};
 const name=e=>{const ids=(e.getAttribute('aria-labelledby')||'').split(/\s+/).filter(Boolean);return (e.getAttribute('aria-label')||ids.map(id=>document.getElementById(id)?.innerText||'').join(' ')||e.labels?.[0]?.innerText||e.innerText||e.title||e.getAttribute('alt')||'').trim().slice(0,160)};
 const region=e=>{const r=e.closest('[role=dialog],form,[role=main],main,header,[role=banner]');return r?(r.getAttribute('aria-label')||r.getAttribute('role')||r.tagName.toLowerCase()):''};
 const elements=[...document.querySelectorAll('input:not([type=hidden]),textarea,select,button,a,[role=button],[role=radio],[role=checkbox],[role=option],[role=combobox],[role=menuitem],label,li')]
   .filter(e=>visible(e)||e.type==='file');
 elements.sort((a,b)=>Number(b.matches('input,textarea,select,button,[role=button],[role=combobox]'))-Number(a.matches('input,textarea,select,button,[role=button],[role=combobox]')));
 const controls=elements.slice(0,600).map(e=>({ref:ref(e),tag:e.tagName.toLowerCase(),type:e.type||e.getAttribute('role')||'',
     accessibleName:name(e),region:region(e),visible:visible(e),
     text:(e.innerText||e.getAttribute('aria-label')||'').trim().slice(0,100),label:label(e),value:e.type==='password'?'[redacted]':(e.value??''),checked:!!e.checked||e.getAttribute('aria-checked')==='true',
     disabled:!!e.disabled,readOnly:!!e.readOnly,accept:e.accept||'',href:e.getAttribute('href')||'',
     placeholder:e.placeholder||'',
     context:(e.closest('.ks-form-item,.el-form-item,[class*=form-item],[class*=formItem]')?.innerText||((e.type==='radio'||e.type==='checkbox')?e.parentElement?.parentElement?.innerText:'')||'').slice(0,160),
     options:e.tagName==='SELECT'?[...e.options].map(o=>({text:o.text,value:o.value})):[],
     inViewport:(()=>{let r=e.getBoundingClientRect();return r.bottom>0&&r.top<innerHeight})()}));
 document.documentElement.dataset.ksCounter=String(counter);
 const rows=[...document.querySelectorAll('tr,[role=row],.ks-table__row')].filter(visible).map(e=>({text:e.innerText.slice(0,700),cells:[...e.querySelectorAll('td,[role=cell]')].map(c=>c.innerText.trim()),links:[...e.querySelectorAll('a[href]')].map(a=>a.href),episodeId:e.getAttribute('data-episode-id')||''})).slice(0,80);
 const uploadRows=[...document.querySelectorAll('tr,[role=row],[class*=upload-item],[class*=uploadItem],.ks-upload-list-item,.el-upload-list__item')].filter(e=>visible(e)&&/\.mp4\b/i.test(e.innerText)).map(e=>({text:e.innerText.slice(0,1200),previews:[...e.querySelectorAll('img')].filter(i=>visible(i)&&i.naturalWidth>0).length}));
 return {url:location.href,title:document.title,text:document.body.innerText.slice(0,16000),controls,rows,uploadRows,
   controlCount:elements.length,truncated:elements.length>600,frameCount:document.querySelectorAll('iframe').length,
   accountText:[...document.querySelectorAll('header,[role=banner],.header,.home__header')].filter(visible).map(e=>e.innerText).join('\n').slice(0,2000),
   loading:[...document.querySelectorAll('[aria-busy=true],[role=progressbar],.ks-loading-mask,.el-loading-mask')].some(visible),
   dialogs:[...document.querySelectorAll('[role=dialog],.ks-dialog,.el-dialog')].filter(visible).map(e=>e.innerText.slice(0,1200)),
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

