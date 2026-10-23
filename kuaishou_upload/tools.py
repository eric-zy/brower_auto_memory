"""Platform-neutral business tool surface; implementations are selected by registry."""
from __future__ import annotations
import json
from .contracts import *
from .database import FINISHED
from .platforms import get_adapter


def UploadTask(job, sha256, settings, database, scan_summary, *, browser=None):
    return get_adapter(settings.platform).create_task(job, sha256, settings, database, scan_summary, browser=browser)


def build_tools(context: UploadTask):
    """仅注册业务工具和 done，移除通用 click/evaluate 等可绕过提交边界的动作。"""
    from browser_use import Tools
    from browser_use.agent.views import ActionResult

    tools = Tools()
    for name in list(tools.registry.registry.actions):
        if name != "done":
            tools.exclude_action(name)

    async def invoke(method, params):
        try:
            result = await method(params)
            if context.bootstrapped and context.state.get("formVerified") and context.record["content_id"] and "video" in context.state.get("uploadedAssets", []):
                context.awaiting_upload = True
            output = {"ok": True, **result}
        except Exception as exc:
            result = await context.fail(exc)
            output = {"ok": False, **result}
        handoff = context.awaiting_upload or context.record["status"] in FINISHED
        return ActionResult(extracted_content=json.dumps({**output, "handoff_to_program": handoff}, ensure_ascii=False),
                            is_done=handoff, success=True if handoff else None)

    @tools.action("读取本轮目录扫描摘要与当前任务资料。只处理当前任务，程序自动领取下一集。", param_model=ScanParams)
    async def scan_directory(params: ScanParams):
        return await invoke(context.scan_directory, params)

    @tools.action("读取页面、搜索剧名、受控导航并确认查重。ref 使用本工具返回的 k 引用，不能用普通 DOM 索引。", param_model=InspectParams)
    async def inspect_platform(params: InspectParams):
        return await invoke(context.inspect_platform, params)

    @tools.action("填写固定资料，或只读回查字段；字段映射只含名称和 k 引用，实际值由程序读取。verify/next_ref 执行全量核对。", param_model=FormParams)
    async def fill_form(params: FormParams):
        return await invoke(context.fill_form, params)

    @tools.action("将本任务 video/copyright/cover 白名单文件传给对应 file input，一次选择，不重复上传。", param_model=UploadParams)
    async def upload_files(params: UploadParams):
        return await invoke(context.upload_files, params)

    @tools.action("查询附件进度或核对上次提交结果。模拟模式上传完成后保存 test_prepared 并结束，不最终提交。轮询间隔至少 30 秒。", param_model=JobParams)
    async def get_upload_status(params: JobParams):
        return await invoke(context.get_upload_status, params)

    @tools.action("复核当前集；真实模式 ready 后提交一次，模拟模式只保存 test_prepared、不点击提交。", param_model=JobParams)
    async def submit_upload(params: JobParams):
        return await invoke(context.submit_upload, params)

    return tools
