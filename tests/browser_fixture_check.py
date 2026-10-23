"""在本地 HTML 上验证 CDP 回读和文件选择，不访问快手、不调用模型。"""
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from support import make_materials
from kuaishou_upload.browser import Decision, PlatformBrowser, SNAPSHOT, field_candidates
from kuaishou_upload.schema import load_job
from kuaishou_upload.browser import content_id
from kuaishou_upload.config import Settings
from kuaishou_upload.database import UploadDatabase
from kuaishou_upload.scan import scan_directory
from kuaishou_upload.tools import UploadTask


async def check(root):
    os.environ["BROWSER_USE_CONFIG_DIR"] = str(root / "config")
    os.environ["TIMEOUT_BrowserStartEvent"] = "120"
    from browser_use import Browser
    job = load_job(make_materials(root / "assets"))
    browser = Browser(headless=True, enable_default_extensions=False, user_data_dir=root / "profile")
    adapter = PlatformBrowser(job, root)
    try:
        await browser.start()
        adapter.page = await browser.new_page((Path(__file__).parent / "fixtures" / "upload_entry.html").as_uri())
        await adapter.wait_page()
        async def snap():
            return json.loads(await adapter.page.evaluate(SNAPSHOT))
        # 仅本地 fixture 绕过域名检查，真实适配器仍只接受 kdj.kuaishou.com。
        adapter.snapshot = snap
        await adapter.open_upload_entry()
        assert (await adapter.page.get_url()).endswith("upload_form.html")
        assert await adapter.page.evaluate("() => sessionStorage.getItem('tourDismissed')") == "1"
        async def node(selector):
            await snap()
            ref = await adapter.page.evaluate("(s) => document.querySelector(s).dataset.ksUploadRef", selector)
            return next(n for n in (await snap())["controls"] if n["ref"] == ref)
        mapping = {"剧名": "#title", "作品简介": "#synopsis", "集数标题": "#episode", "备案号": "#filing",
                   "版权类型": "#proof-type", "版权开始日期": "#from", "版权结束日期": "#until",
                   "允许转授权": 'input[name="sublicense"][value="0"]', "售卖方式": "#selling",
                   "单集付费": 'input[name="paid"][value="免费"]'}
        for key, selector in mapping.items():
            candidates = field_candidates(await snap(), key, adapter.fields[key])
            assert len(candidates) == 1, f"自动定位 {key}: {candidates}"
            await adapter.act(Decision(action="fill", ref=candidates[0]["ref"], field=key), "prepare", await snap())
        await adapter.verify_fields()
        # 模拟恢复时重新映射控件，只读核对元数据，不再次填写。
        adapter.filled.clear()
        for key, selector in mapping.items():
            adapter.record_readback(await node(selector), key)
        await adapter.verify_fields()
        assert await adapter.page.evaluate("() => window.inputEvents") == "1", "恢复核对不应写页面"
        assert await adapter.page.evaluate("() => window.inputEvents") == "1", "没有触发 input 事件"
        # 框架把已填字段清空时，下一步前复核必须拦截。
        await adapter.page.evaluate("() => document.querySelector('#filing').value=''")
        try:
            await adapter.verify_fields()
            raise AssertionError("未发现字段清空")
        except ValueError as error:
            assert "备案号" in str(error)
        await adapter.write_field(await node("#filing"), "备案号")
        for role, selector in {"video": "#video", "copyright": "#proof", "cover": "#cover"}.items():
            n = await node(selector)
            await adapter.act(Decision(action="upload", ref=n["ref"], asset=role), "prepare", await snap())
            count = await adapter.page.evaluate("(s) => document.querySelector(s).files.length", selector)
            assert count == "1", role
        # 下拉选项必须是真实可见且与资料值完全一致，并可从触发器回读。
        adapter.fields["分类"] = "爱情"
        trigger = await node("#category")
        await adapter.act(Decision(action="click", ref=trigger["ref"], field="分类"), "prepare", await snap())
        option = await node('[role="option"]')
        await adapter.act(Decision(action="choose", ref=option["ref"], field="分类"), "prepare", await snap())
        await adapter.verify_fields()
        submit = next(n for n in (await snap())["controls"] if n["tag"] == "button")
        try:
            await adapter.act(Decision(action="click", ref=submit["ref"]), "prepare", await snap())
            raise AssertionError("模型获得了提交权限")
        except ValueError:
            pass
        assert await adapter.page.evaluate("() => window.didSubmit") in {"false", "False"}
        before_lookup = dict(adapter.filled)
        async def lookup_page(_url):
            return await browser.new_page((Path(__file__).parent / "fixtures" / "lookup.html").as_uri())
        adapter.browser = SimpleNamespace(new_page=lookup_page, close_page=browser.close_page,
                                          get_or_create_cdp_session=browser.get_or_create_cdp_session)
        assert await adapter.lookup_series() == "LOCAL1"
        assert adapter.filled == before_lookup, "查重不能污染上传表单字段绑定"

        # 自动路径：无模型参与，填表、选证明/封面、下一步、选视频、核对后停止于 test_prepared。
        materials = root / "auto-assets"
        make_materials(materials, test_mode=True)
        settings = Settings(materials, root / "auto-state", root / "auto-ledger.xlsx", stable_seconds=0)
        database = UploadDatabase(settings.root / "uploads.sqlite3")
        try:
            sha, auto_job = next(iter(scan_directory(settings, database).jobs.items()))
            auto_browser = PlatformBrowser(auto_job, settings.root)
            auto_browser.page = await browser.new_page((Path(__file__).parent / "fixtures" / "upload_form.html").as_uri() + "?autoflow=1")
            task = UploadTask(auto_job, sha, settings, database, {}, browser=auto_browser)
            async def local_snapshot():
                state = json.loads(await auto_browser.page.evaluate(SNAPSHOT))
                cid = content_id(state["url"])
                if cid and auto_browser.form_verified:
                    await task.checkpoint(contentId=cid, formVerified=True, cardCount=1, draftUrl=state["url"])
                return state
            async def local_verify(state):
                assert state["contentId"] == "LOCAL1" and auto_browser.form_verified
                assert auto_browser.video_complete(await local_snapshot(), auto_job.files["video"].name)
            auto_browser.snapshot = local_snapshot  # 仅本地 fixture 的 file:// 页面。
            auto_browser.verify_prepared = local_verify
            task.checked = task.bootstrapped = True
            await auto_browser.wait_page()
            await task.auto_prepare()
            assert task.awaiting_upload, task.status()
            await task.wait_for_upload()
            assert task.record["status"] == "test_prepared", task.status()
            assert set(task.state["uploadedAssets"]) == {"video", "cover", "copyright"}
            assert await auto_browser.page.evaluate("() => window.didSubmit") in {"false", "False"}
        finally:
            database.close()
        print("PASS：自动关闭引导、点击内容上传、按标签自动填表、日期/单选/下拉回读、附件白名单及提交限制")
        print("PASS：独立页查重不污染表单，自动准备全流程无需模型，结束于 test_prepared")
    finally:
        await browser.kill()


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as directory:
        asyncio.run(check(Path(directory)))
