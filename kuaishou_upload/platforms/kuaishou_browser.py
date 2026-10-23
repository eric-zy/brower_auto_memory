from __future__ import annotations

import asyncio
import base64
import json
import os
import re
from pathlib import Path
from typing import Literal
from urllib.parse import parse_qs, urlsplit

from ..contracts import Decision
from ..observation import SNAPSHOT, compact_snapshot
from ..browser_runtime import BrowserRuntime
from ..locator import safe_candidate, signature

from ..schema import file_hash
from ..config import PLATFORM_URL
from .kuaishou_lookup import lookup_evidence

PLATFORM = "https://kdj.kuaishou.com"
FINAL_LABELS = {"提交", "发布", "提交审核", "确认发布", "确认提交"}
REVIEW = re.compile(r"待审核|审核中|审核通过|审核不通过|已发布|已上架|已驳回")


# 模型只能看到紧凑 DOM；不能提供 JavaScript、任意文本值、本地路径或任意 URL。


def content_id(url: str) -> str:
    return parse_qs(urlsplit(url).query).get("miniSeriesId", [""])[0]


def safe_control(node: dict, phase: str, fields: dict, dialogs: list[str]) -> bool:
    text = node["text"].strip()
    if text in FINAL_LABELS or re.search(r"提交|发布|删除|撤回|下架|同意.*协议", text):
        return False
    if node.get("href"):
        target = urlsplit(node["href"])
        if target.hostname and target.hostname != "kdj.kuaishou.com":
            return False
        if node["href"].startswith(("javascript:", "data:")):
            return False
    if phase == "entry":
        return text in {"知道了", "内容上传", "付费短剧"}
    if phase == "discover":
        return text in {"内容管理", "付费短剧", "短剧管理", "搜索", "查询", "详情", "查看", fields["剧名"]}
    if text in {"确定", "确认"}:
        # 确认只能处理已知的集数/裁剪/上传/下一步提示，不能接受未知协议或最终发布确认。
        return any(re.search(r"添加单集|添加集数|图片剪裁|图片裁剪|批量上传|审核后仅支持修改", d)
                   and not re.search(r"确认发布|确认提交|服务协议|用户协议", d) for d in dialogs)
    allowed = {"内容管理", "付费短剧", "内容上传", "新增集数", "上传", "下一步", "上一步", "返回",
               "+ 添加单集信息", "添加单集信息", "批量上传视频", "开始上传", "免费", "付费", "是", "否"}
    return text in allowed or text in {str(v) for v in fields.values()}


FIELD_ALIASES = {
    "剧名": ("剧名", "短剧名称", "短剧标题", "作品名称"), "作品简介": ("作品简介", "短剧简介", "内容简介", "简介"),
    "集数标题": ("集数标题", "单集标题", "剧集标题", "单集名称"), "备案号": ("备案号", "备案编号"),
    "版权类型": ("版权类型", "版权证明类型", "版权证明材料"),
    "版权开始日期": ("版权开始日期", "授权开始日期"), "版权结束日期": ("版权结束日期", "授权结束日期"),
    "允许转授权": ("允许转授权", "转授权权利", "转授权"), "售卖方式": ("售卖方式", "售卖模式"),
    "单集付费": ("单集付费", "是否付费"), "单集价格": ("单集价格", "本集价格"),
}


def field_candidates(snap, name, value, *, read_only=False):
    result = []
    for node in snap["controls"]:
        if node["tag"] not in {"input", "textarea", "select"} or (not read_only and (node.get("disabled") or node.get("readOnly"))):
            continue
        if node["type"] in {"file", "password", "hidden", "button", "submit"}:
            continue
        label = re.sub(r"\s+", "", node.get("label", "") + " " + node.get("context", ""))
        aliases = FIELD_ALIASES.get(name, (name,))
        if not any(alias in label for alias in aliases):
            continue
        if isinstance(value, bool) and node["type"] == "radio":
            choices = {str(node.get("value", "")).lower(), node.get("text", "").strip(), node.get("label", "").strip()}
            wanted = ({"付费"} if value else {"免费"}) if name == "单集付费" else (
                {"是", "允许", "允许转授权", "true", "1", "yes"} if value else {"否", "不允许", "不允许转授权", "false", "0", "no"})
            if not choices.intersection(wanted):
                continue
        elif isinstance(value, bool) and node["type"] != "checkbox":
            continue
        if node["tag"] == "select" and not any(str(value) in {option["text"], option["value"]} for option in node.get("options", [])):
            continue
        result.append(node)
    return result




class PlatformBrowser(BrowserRuntime):
    allowed_hosts = frozenset({'kdj.kuaishou.com'})
    intents = {
        'open_upload': ('内容上传', '上传内容', '上传短剧', '上传作品'),
        'open_list': ('付费短剧', '短剧管理', '内容管理'),
        'search': ('搜索', '查询'),
        'search_field': ('短剧标题', '剧名', '回车键确认', '搜索作品'),
        'next': ('下一步', '继续'),
        'add_episode': ('新增集数', '添加单集信息', '+ 添加单集信息', '添加剧集'),
    }
    def __init__(self, job, root: Path, *, headless=False, expected_account="", recovery_steps=2):
        super().__init__(root, expected_account=expected_account, recovery_steps=recovery_steps)
        self.job, self.root = job, root
        self.headless = headless
        self.browser = self.page = None
        self.checkpoint = None
        self.fields = job.fields() if job else {}
        self.filled, self.uploaded = {}, set()
        self.form_verified = False
        self.expected_card_count = 1
        self.last_snapshot = None
        self.phase_name = ""
        self.pending_choices = {}
        self.attempts = {}
        self.failure_snapshot = None
        self._owned_processes = []

    async def open(self):
        os.environ.setdefault("BROWSER_USE_CONFIG_DIR", str(self.root / "browser-config"))
        os.environ.setdefault("TIMEOUT_BrowserStartEvent", "120")
        os.environ.setdefault("TIMEOUT_BrowserLaunchEvent", "90")
        from browser_use import Browser

        pool = self.session_pool
        if pool and (pool.root != self.root.resolve() or pool.expected_account != self.expected_account or pool.failed):
            raise RuntimeError('共享浏览器身份不一致或连接已失效，停止批次')
        try:
            if pool and pool.browser:
                self.browser, self.page = pool.browser, pool.page
                if self.page is None:
                    raise RuntimeError('共享浏览器没有已验证的任务标签页')
                print('[浏览器] 复用当前会话和标签页', flush=True)
                await self.emit('browser_session_reused', targetId=self.page._target_id)
            else:
                self.browser = Browser(user_data_dir=self.root / "browser-profile", headless=self.headless,
                                       enable_default_extensions=False, captcha_solver=False, keep_alive=True)
                if pool:
                    pool.browser = self.browser
                if pool:
                    await pool.start_browser(self.browser)
                else:
                    from ..browser_process import start_owned_browser
                    await start_owned_browser(self.browser, self._owned_processes)
                self.page = await self.browser.get_current_page()
                if self.page is None:
                    self.page = await self.browser.new_page()
                if pool:
                    pool.page = self.page
            await self.page.goto(PLATFORM_URL)
            await self.wait_page()
        except BaseException:
            if pool:
                pool.failed = True
            raise

    async def close(self):
        if self.browser and self.session_pool is None:
            from ..browser_process import close_owned_processes
            try:
                await self.browser.kill()
            finally:
                await close_owned_processes(self._owned_processes)

    async def wait_page(self):
        for _ in range(30):
            value = await self.page.evaluate("() => document.readyState")
            if value == "complete":
                return
            await asyncio.sleep(0.5)
        raise TimeoutError("页面在 15 秒内未完成加载")

    @staticmethod
    def is_upload_form(snap):
        return bool(re.search(r"版权|备案|单集信息", snap["text"]) and any(
            node["tag"] in {"input", "textarea", "select"} for node in snap["controls"]))

    async def dismiss_onboarding(self):
        snap = await self.snapshot()
        if "点击短剧标题" in snap["text"] or "查看详情页面" in snap["text"]:
            buttons = [n for n in snap["controls"] if n["text"].strip() == "知道了" and n["tag"] == "button" and not n["disabled"]]
            if len(buttons) == 1:
                await self.act(Decision(action="click", ref=buttons[0]["ref"]), "entry", snap)

    async def open_upload_entry(self):
        await self.dismiss_onboarding()
        opened_menu = False
        clicked_entry = False
        for _ in range(60):
            snap = await self.snapshot()
            if self.is_upload_form(snap):
                await self.confirm_intents(snap)
                return snap
            if clicked_entry:
                await asyncio.sleep(0.5)
                continue
            button = await self.resolve_intent(snap, 'open_upload', recover=opened_menu)
            if button:
                print("[入口] 点击“内容上传”", flush=True)
                await self.click_intent(button, 'open_upload', 'entry', snap)
                clicked_entry = True
                continue
            # 管理平台可能只挂载账号及侧栏，内容路由尚未进入。
            # 只尝试一次准确的菜单项，并在点击后重新获取引用。
            if not opened_menu and not snap.get("dialogs"):
                menu = await self.resolve_intent(snap, 'open_list', recover=False)
                if menu:
                    opened_menu = True
                    print("[入口] 内容区未就绪，点击“付费短剧”菜单", flush=True)
                    await self.click_intent(menu, 'open_list', 'entry', snap)
                    continue
                # A list without a matching sidebar can still recover an icon entry.
                if _ >= 4:
                    button = await self.resolve_intent(snap, 'open_upload')
                    if button:
                        await self.click_intent(button, 'open_upload', 'entry', snap)
                        clicked_entry = True
            await asyncio.sleep(0.5)
        else:
            excerpt = " ".join(snap.get("text", "").split())[:180]
            raise TimeoutError(("已点击入口但未出现上传表单。" if clicked_entry else "仍未出现“内容上传”入口；内容区可能未加载。")
                               + f"URL={snap['url']}；状态={snap.get('pageState', 'unknown')}；控件={len(snap['controls'])}；"
                               + f"截断={snap.get('truncated', False)}；iframe={snap.get('frameCount', 0)}；页面={excerpt}")

    async def lookup_series(self):
        """在当前列表标签页查重；业务层据结果决定进入详情或上传表单。"""
        filled = dict(self.filled)
        snap = None
        evidence = None
        self.failure_snapshot = None
        try:
            await self.wait_page()
            await self.dismiss_onboarding()
            for _ in range(30):
                snap = await self.snapshot()
                if urlsplit(snap['url']).path.rstrip('/') != urlsplit(PLATFORM_URL).path:
                    raise ValueError('平台查重只允许在当前内容管理列表进行，不能填写详情或上传表单')
                field = await self.resolve_intent(snap, 'search_field', recover=_ >= 4)
                button = await self.resolve_intent(snap, 'search', recover=_ >= 4)
                inputs, buttons = ([field] if field else []), ([button] if button else [])
                if inputs and buttons:
                    break
                await asyncio.sleep(0.5)
            if len(inputs) != 1 or len(buttons) != 1:
                raise ValueError("无法唯一定位列表的短剧标题搜索框或搜索按钮")
            await self.write_field(inputs[0], "剧名")
            current = await self.snapshot()
            button = await self.resolve_intent(current, 'search')
            await self.click_intent(button, 'search', 'discover', current)
            previous = None
            for _ in range(30):
                await asyncio.sleep(0.5)
                snap = await self.snapshot()
                evidence = lookup_evidence(snap, self.fields["剧名"], search_ref=inputs[0]['ref'])
                ids, empty = evidence['ids'], evidence['empty']
                stable = (tuple(ids), empty)
                if stable == previous and (ids or empty):
                    if len(ids) > 1:
                        raise ValueError("平台存在多个同名作品，请先核对目标 ID")
                    await self.confirm_intents(snap)
                    await self.emit('lookup_verified', title=self.fields['剧名'], contentId=ids[0] if ids else None,
                                    queryVerified=evidence['queryVerified'], candidates=evidence['candidates'])
                    return ids[0] if ids else None
                previous = stable
            raise TimeoutError("搜索后无法取得匹配作品或明确无结果的证据；"
                               + f"查询已核实={bool(evidence and evidence['queryVerified'])}；"
                               + f"作品行数={len(evidence['candidates']) if evidence else 0}")
        except Exception:
            # Preserve the list evidence without navigating away on failure.
            self.failure_snapshot = snap
            await self.emit('lookup_failed', title=self.fields['剧名'], url=snap.get('url') if snap else None,
                            evidence=evidence)
            raise
        finally:
            # The search field must never count as a completed metadata field.
            self.filled = filled

    async def snapshot(self, *, allow_login=False):
        url = await self.page.get_url()
        self.validate_url(url)
        snap = json.loads(await self.page.evaluate(SNAPSHOT))
        await self.observe_state(snap, allow_login=allow_login)
        self.last_snapshot = snap
        if self.checkpoint:
            updates = {"currentUrl": snap["url"]}
            if content_id(snap["url"]) and self.phase_name == "prepare" and self.form_verified:
                updates.update(contentId=content_id(snap["url"]), formVerified=True, cardCount=self.expected_card_count)
                video_page = any(n["type"] == "file" and ("video" in n.get("accept", "") or "视频" in n["label"]) for n in snap["controls"])
                if video_page or snap.get("uploadRows") or "step=1" in snap["url"]:
                    updates["draftUrl"] = snap["url"]
            await self.checkpoint(**updates)
        return snap

    async def element(self, ref):
        if not re.fullmatch(r"k[0-9]+", ref):
            raise ValueError("无效页面引用")
        elements = await self.page.get_elements_by_css_selector(f'[data-ks-upload-ref="{ref}"]')
        if len(elements) != 1:
            raise RuntimeError("页面引用已失效，停止并重新核对页面")
        return elements[0]

    async def write_field(self, node, key):
        if key not in self.fields:
            raise ValueError(f"未知字段，禁止模型补值: {key}")
        value = self.fields[key]
        if node["type"] == "radio":
            choices = {str(node.get("value", "")).lower(), node.get("text", "").strip(), node.get("label", "").strip()}
            accepted = ({"付费"} if value else {"免费"}) if key == "单集付费" else (
                {"是", "允许", "允许转授权", "true", "1", "yes"} if value else
                {"否", "不允许", "不允许转授权", "false", "0", "no"})
            if not isinstance(value, bool) or not choices.intersection(accepted):
                raise ValueError(f"radio 选项与资料不匹配: {key}")
        result = json.loads(await self.page.evaluate(r"""(ref,value) => {
 const e=document.querySelector('[data-ks-upload-ref="'+ref+'"]');if(!e||e.disabled||e.readOnly)throw Error('字段不存在、被禁用或只读；自定义下拉应选择选项');
 if(e.type==='file')throw Error('文件必须通过白名单上传');
 if(e.type==='checkbox'||e.type==='radio'){if(typeof value!=='boolean')throw Error('需要布尔值');e.checked=e.type==='radio'?true:value;}
 else if(e.tagName==='SELECT'){const o=[...e.options].filter(o=>o.text.trim()===String(value)||o.value===String(value));if(o.length!==1)throw Error('选项不唯一');e.value=o[0].value;}
 else {const p=e.tagName==='TEXTAREA'?HTMLTextAreaElement.prototype:HTMLInputElement.prototype;
 const setter=Object.getOwnPropertyDescriptor(p,'value')?.set;if(!setter)throw Error('不是可写输入框');setter.call(e,String(value));}
 e.dispatchEvent(new Event('input',{bubbles:true}));e.dispatchEvent(new Event('change',{bubbles:true}));e.blur();
 return {value:e.value,checked:e.checked,text:e.tagName==='SELECT'?e.selectedOptions[0]?.text:''};
}""", node["ref"], value))
        if isinstance(value, bool):
            valid = result["checked"] == (True if node["type"] == "radio" else value)
        else:
            valid = str(value) in {str(result["value"]), str(result.get("text", ""))}
        if not valid:
            raise ValueError(f"字段回读不一致: {key}")
        self.filled[key] = {"ref": node["ref"], "value": value, "kind": "value",
                            "checkedExpected": True if node["type"] == "radio" else value}
        print(f"字段 {key} ← {str(value)[:20]}", flush=True)

    async def verify_fields(self):
        required = set(self.fields) - {"添加集数"}
        if self.job.manifest.sellingMode == "free":
            required -= {"单集价格"}
        if not required.issubset(self.filled):
            raise ValueError("必填字段未完成: " + "、".join(sorted(required - self.filled.keys())))
        current = json.loads(await self.page.evaluate(SNAPSHOT))
        if not getattr(self, 'batch_hashes', None) and self.expected_card_count == 1 and self.job.manifest.episodes[0].number != 1:
            raise ValueError('已进入新建表单；非首集需要文件夹批次核实真实平台集号，不能作为第1集导入')
        episode_inputs = [node for node in current["controls"] if node["tag"] == "input" and node["type"] == "text"
                          and re.search(r"集数标题|单集标题|剧集标题|单集名称", node["label"] + " " + node.get("context", ""))]
        matching = [n for n in episode_inputs if n["value"] == self.job.manifest.episodes[0].title]
        if len(episode_inputs) != self.expected_card_count or len(matching) != 1 or any(not n["value"].strip() for n in episode_inputs):
            raise ValueError("卡片数量、当前集标题或已有卡片无法核对，停止，不删除旧集或创建多余空卡")
        # 在“下一步”之前重新读取全部已填字段；页面清空或替换节点时停止。
        for key in required:
            entry = self.filled[key]
            result = json.loads(await self.page.evaluate(r"""ref => {
 const e=document.querySelector('[data-ks-upload-ref="'+ref+'"]');
 return e?{value:e.value,checked:!!e.checked||e.getAttribute('aria-checked')==='true',text:e.tagName==='SELECT'?e.selectedOptions[0]?.text:(e.innerText||'').trim()}:null;
}""".replace("ref =>", "(ref) =>"), entry["ref"]))
            expected = entry["value"]
            if entry.get("kind") == "selection":
                if result is None or not result["checked"]:
                    raise ValueError(f"下一步前复核失败，选择被清空: {key}")
                continue
            if result is None or (isinstance(expected, bool) and result["checked"] != entry["checkedExpected"]) or (
                not isinstance(expected, bool) and str(expected) not in {str(result["value"]), str(result.get("text", ""))}
            ):
                raise ValueError(f"下一步前复核失败，字段丢失或被清空: {key}")
        self.form_verified = True

    def record_readback(self, node, key):
        """模型只映射字段位置，实际值必须由 DOM 精确匹配；此函数不写页面。"""
        if key not in self.fields:
            raise ValueError("未知回读字段")
        expected = self.fields[key]
        kind = "value"
        if isinstance(expected, bool):
            if node["type"] == "checkbox":
                valid = node["checked"] == expected
            else:
                choices = {str(node["value"]).lower(), node["label"].strip(), node["text"].strip()}
                accepted = ({"付费"} if expected else {"免费"}) if key == "单集付费" else (
                    {"是", "允许", "true", "1"} if expected else {"否", "不允许", "false", "0"})
                valid = node["type"] == "radio" and node["checked"] and bool(choices.intersection(accepted))
                kind = "selection"
        elif node["type"] in {"checkbox", "radio"}:
            valid = node["checked"] and expected in {node["text"].strip(), node["label"].strip(), node["value"]}
            kind = "selection"
        else:
            actual = {node["value"], node["text"].strip()}
            actual.update(option["text"] for option in node.get("options", []) if option["value"] == node["value"])
            valid = str(expected) in actual
        if not valid:
            raise ValueError(f"恢复时平台字段与资料不一致: {key}")
        self.filled[key] = {"ref": node["ref"], "value": expected, "kind": kind,
                            "checkedExpected": expected}

    async def act(self, decision, phase, snap):
        if decision.action == "block":
            raise RuntimeError(decision.reason or "模型报告页面阻塞")
        if decision.action == "scroll":
            await self.page.evaluate("() => {const e=document.querySelector('.home__main')||document.scrollingElement;e.scrollTop+=innerHeight*0.7}")
            return
        nodes = [n for n in snap["controls"] if n["ref"] == decision.ref]
        if len(nodes) != 1:
            raise ValueError("模型引用了快照中不存在的页面元素")
        node = nodes[0]
        if await self.page.get_url() != snap["url"]:
            raise RuntimeError("模型规划期间页面已经导航，停止，禁止对旧快照执行动作")
        current = json.loads(await self.page.evaluate(r"""(ref) => {
 const e=document.querySelector('[data-ks-upload-ref="'+ref+'"]');
 if(!e)return null;const r=e.getBoundingClientRect(),s=getComputedStyle(e);
 const visible=r.width>0&&r.height>0&&s.visibility!=='hidden'&&s.display!=='none';
 const hit=document.elementFromPoint(Math.min(innerWidth-1,Math.max(0,r.x+r.width/2)),Math.min(innerHeight-1,Math.max(0,r.y+r.height/2)));
 const ids=(e.getAttribute('aria-labelledby')||'').split(/\s+/).filter(Boolean);
 const accessibleName=(e.getAttribute('aria-label')||ids.map(id=>document.getElementById(id)?.innerText||'').join(' ')||e.labels?.[0]?.innerText||e.innerText||e.title||e.getAttribute('alt')||'').trim().slice(0,160);
 return {tag:e.tagName.toLowerCase(),type:e.type||e.getAttribute('role')||'',text:(e.innerText||e.getAttribute('aria-label')||'').trim().slice(0,100),accessibleName,disabled:!!e.disabled,visible,receivesEvents:!!hit&&(hit===e||e.contains(hit)),href:e.getAttribute('href')||''};
}""", node["ref"]))
        if current is None or current["tag"] != node["tag"] or current["type"] != node["type"] or current["disabled"]:
            raise RuntimeError("页面引用已变化或不可用，停止执行旧动作")
        if decision.action in {"click", "choose"} and current["text"] != node["text"]:
            raise RuntimeError("按钮或选项文案已变化，停止执行旧动作")
        if 'accessibleName' in current and 'accessibleName' in node and current['accessibleName'] != node['accessibleName']:
            raise RuntimeError('控件可访问名称已变化，请重新定位')
        if decision.action in {'click', 'choose'}:
            if current.get('visible') is False or current.get('receivesEvents') is False:
                raise RuntimeError('目标控件被遮挡或不可见，重新观察后再定位')
            if current.get('href', '') != node.get('href', ''):
                raise RuntimeError('目标链接已变化，停止执行旧动作')
            if current.get('href'):
                from urllib.parse import urljoin
                self.validate_url(urljoin(snap['url'], current['href']))
        if node.get('semanticClickable'):
            actionable = await self.page.evaluate("""(ref) => {const e=document.querySelector('[data-ks-upload-ref="'+ref+'"]');return !!e&&e.matches('div,span')&&!e.closest('button,a,li,label,[role=button],[role=menuitem],[role=radio],[role=checkbox],[role=option],[aria-disabled=true]')&&!!(e.onclick||getComputedStyle(e).cursor==='pointer')}""", node['ref'])
            if actionable not in ('True','true'):
                raise RuntimeError('控件点击属性已变化，请重新观察')
        if re.search(r"服务协议|用户协议|免责声明|我已阅读", " ".join(str(node.get(k, "")) for k in ("text", "label", "context"))):
            raise ValueError("遇到协议确认控件，需要用户自行处理")
        attempt = (phase, snap.get("url", ""), decision.action, decision.ref, decision.field)
        self.attempts[attempt] = self.attempts.get(attempt, 0) + 1
        if self.attempts[attempt] > 2:
            raise RuntimeError("同一动作重试超过一次，保存现场并停止")
        if decision.action == "fill":
            if phase == "discover" and (decision.field != "剧名" or not re.search(r"搜索|剧名|名称", node["label"])):
                raise ValueError("查重阶段只能填写剧名搜索框")
            await self.write_field(node, decision.field)
        elif decision.action == "click":
            if re.search(r"提交|发布|删除|撤回|下架", node["text"]):
                raise ValueError("最终提交或破坏性动作不可由模型触发")
            dropdown = (phase == "prepare" and decision.field in self.fields
                        and (node.get("readOnly") or node["type"] == "combobox")
                        and node["tag"] in {"input", "div", "span"})
            resolved = self.consume_intent_grant(decision, node, snap)
            if resolved and decision.intent == 'next':
                await self.verify_fields()
            if not dropdown and not resolved and not safe_control(node, phase, self.fields, snap["dialogs"]):
                raise ValueError(f"此阶段不允许点击: {node['text']}")
            if dropdown:
                self.pending_choices[decision.field] = node["ref"]
            if node["text"].strip() == "下一步":
                await self.verify_fields()
            if node["text"].strip() in {"免费", "付费", "是", "否"}:
                # 布尔/付费选择必须通过关联 input 的 fill 回读，禁止仅凭字面点选。
                raise ValueError("请定位关联 radio/checkbox，使用对应字段的 fill 动作")
            await (await self.element(decision.ref)).click()
            await asyncio.sleep(0.5)
        elif decision.action == "choose":
            if phase != "prepare" or decision.field not in self.fields or not node["inViewport"]:
                raise ValueError("不允许选择此字段/不可见选项")
            expected = self.fields[decision.field]
            texts = {node["text"].strip(), node["label"].strip(), str(node["value"])}
            if isinstance(expected, bool):
                wanted = ({"付费"} if expected else {"免费"}) if decision.field == "单集付费" else (
                    {"是", "允许", "true", "1"} if expected else {"否", "不允许", "false", "0"})
            else:
                wanted = {str(expected)}
            if not texts.intersection(wanted) or node["text"].strip() in FINAL_LABELS:
                raise ValueError("页面选项与固定资料不匹配")
            if node["type"] not in {"radio", "checkbox", "option"} and node["tag"] != "li":
                raise ValueError("目标不是已知选项控件")
            await (await self.element(decision.ref)).click()
            await asyncio.sleep(0.3)
            if node["type"] in {"radio", "checkbox"}:
                selected = await self.page.evaluate("(ref) => {const e=document.querySelector('[data-ks-upload-ref=\"'+ref+'\"]');return !!e&&(!!e.checked||e.getAttribute('aria-checked')==='true')}", node["ref"])
                if selected != "True" and selected != "true":
                    raise ValueError("选择后未读到选中状态")
                self.filled[decision.field] = {"ref": node["ref"], "value": expected, "kind": "selection"}
            else:
                trigger = self.pending_choices.get(decision.field)
                if not trigger:
                    raise ValueError("缺少已记录的下拉触发器")
                actual = await self.page.evaluate("(ref) => {const e=document.querySelector('[data-ks-upload-ref=\"'+ref+'\"]');return e?(e.value||(e.innerText||'').trim()):''}", trigger)
                if actual != str(expected):
                    raise ValueError("下拉选择回读不一致")
                self.filled[decision.field] = {"ref": trigger, "value": expected, "kind": "value"}
        elif decision.action == "upload":
            if phase != "prepare" or decision.asset not in self.job.files or node["type"] != "file":
                raise ValueError("不允许上传该文件或目标不是文件输入框")
            role = decision.asset
            file = self.job.files[role]
            if file_hash(file) != self.job.asset_hashes[role]:
                raise ValueError(f"预检后文件已被修改: {role}")
            accept = node.get("accept", "").lower()
            mime = {".mp4": "video/mp4", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                    ".webp": "image/webp", ".pdf": "application/pdf"}[file.suffix.lower()]
            if accept and not any(token.strip() in {file.suffix.lower(), mime, mime.split('/')[0] + '/*', '*/*'}
                                  for token in accept.split(',')):
                raise ValueError(f"上传控件不接受该附件类型: {role}")
            labels = {"video": r"视频|video", "copyright": r"版权|制作协议|权属|证明", "cover": r"封面|cover"}
            if not re.search(labels[role], node["label"], re.I):
                raise ValueError(f"无法确认 {role} 的上传位置，请核对页面标签")
            if role in self.uploaded:
                raise ValueError("同一文件本轮已选择，禁止重复上传")
            await (await self.element(decision.ref)).set_input_files([str(file)])
            self.uploaded.add(role)
            await asyncio.sleep(0.5)
        else:
            raise ValueError("不支持的动作")

    @staticmethod
    def upload_rows_for(snap, filename):
        pattern = re.compile(r'(?<![\w.-])' + re.escape(filename) + r'(?![\w.-])', re.I)
        return [row for row in snap.get('uploadRows', []) if pattern.search(row['text'])]

    @staticmethod
    def video_complete(snap, filename):
        # 只认可包含该视频文件名的上传行，不能把其他文件的 100% 或网站 Logo 当作证据。
        return any(re.search(r"100\s*%|上传成功|上传完成", row["text"])
                   and row["previews"] > 0 for row in PlatformBrowser.upload_rows_for(snap, filename))

    async def verify_prepared(self, state):
        url = state.get("draftUrl") or state.get("currentUrl", "")
        if urlsplit(url).hostname != "kdj.kuaishou.com" or content_id(url) != str(state.get("contentId")):
            raise ValueError("草稿 URL 与作品 ID 不匹配")
        if await self.page.get_url() != url:
            await self.page.goto(url)
            await self.wait_page()
        snap = await self.snapshot()
        if not self.form_verified:
            raise ValueError("恢复任务需要先使用 fill_form(read_only=True) 回读元数据并验证")
        if not state.get("formVerified") or state.get("cardCount") != self.expected_card_count or not self.video_complete(snap, self.job.files["video"].name):
            raise ValueError("待提交草稿无法复核，请在平台检查；禁止重复上传或直接提交")
        return snap

    async def submit(self, state):
        if self.job.manifest.testMode:
            raise ValueError("模拟模式允许准备表单与附件，禁止点击最终提交")
        snap = await self.verify_prepared(state)
        matches = [n for n in snap["controls"] if n["text"].strip() in {"提交", "提交审核", "发布"}
                   and n["tag"] == "button" and n["inViewport"] and not n["disabled"]]
        if len(matches) != 1:
            raise ValueError("无法唯一定位最终提交按钮，保持提交结果待核对状态")
        await (await self.element(matches[0]["ref"])).click()  # 只点击一次，不自动确认未知弹窗。
        for _ in range(15):
            await asyncio.sleep(2)
            snap = await self.snapshot()
            failure = re.search(r"提交失败|资质不全|内容违规|不能修改当前内容的资料", snap["text"])
            if failure:
                raise RuntimeError(failure.group(0))
            result = self.find_episode(snap)
            if result and content_id(snap["url"]) == str(state["contentId"]):
                return {**result, "contentId": state["contentId"]}
        return await self.reconcile(state)

    def find_episode(self, snap):
        ep = self.job.manifest.episodes[0]
        for row in snap["rows"]:
            cells = row.get("cells", [])
            lines = [line.strip() for line in row["text"].splitlines()]
            number = re.search(r"第0*(\d+)集", row["text"])
            observed_number = int(number.group(1)) if number else (
                int(cells[0]) if cells and cells[0].isdigit() else None)
            if ep.title in cells + lines and observed_number == ep.number and REVIEW.search(row["text"]):
                return {"episodeId": row["episodeId"], "reviewStatus": REVIEW.search(row["text"]).group(0),
                        "platformEvidence": row["text"]}
        return None

    async def open_detail(self, cid):
        if not re.fullmatch(r"[A-Za-z0-9_-]+", str(cid)):
            raise ValueError("作品 ID 格式无效")
        await self.page.goto(f"{PLATFORM}/home/content/content-management/detail?miniSeriesId={cid}")
        await self.wait_page()
        for _ in range(30):
            snap = await self.snapshot()
            if self.job.manifest.seriesTitle in snap["text"] or any(n["value"] == self.job.manifest.seriesTitle for n in snap["controls"]):
                return
            await asyncio.sleep(0.5)
        raise TimeoutError("作品详情在 15 秒内未加载目标剧名，停止以免误操作")

    async def reconcile(self, state):
        if not state.get("contentId"):
            return None
        await self.open_detail(state["contentId"])
        snap = await self.snapshot()
        if self.job.manifest.seriesTitle not in snap["text"]:
            return None
        result = self.find_episode(snap)
        return {**result, "contentId": state["contentId"]} if result else None

    async def screenshot(self, folder, label):
        if not self.page:
            return None
        title = re.sub(r'[<>:"/\\|?*\s,，]', "_", self.job.manifest.seriesTitle)[:60]
        path = folder / f"{title}_第{self.job.manifest.episodes[0].number}集_{label}.png"
        path.write_bytes(base64.b64decode(await self.page.screenshot()))
        return str(path)
