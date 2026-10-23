from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field

from ..browser import Decision, PlatformBrowser, compact_snapshot, content_id, field_candidates
from ..database import DONE, FINISHED, UNCERTAIN
from ..state import now


from ..contracts import *
from ..platforms import get_adapter

class KuaishouUploadTask:
    """业务工具的共享上下文。模型无法改变任务资料、文件白名单或提交状态。"""

    def __init__(self, job, sha256, settings, database, scan_summary, *, browser=None):
        self.job, self.sha256, self.settings, self.database = job, sha256, settings, database
        self.scan_summary = scan_summary
        self.job_id = database.task_id(sha256)
        if database.scope != (settings.platform, settings.account_id):
            raise ValueError("设置与数据库任务身份不一致")
        self.browser = browser or get_adapter(settings.platform).create_browser(job, settings)
        self.browser.checkpoint = self.checkpoint
        self.browser.expected_card_count = int(self.record["details"].get("cardCount") or 1)
        self.reconcile_only = self.record["status"] in UNCERTAIN or bool(self.record["details"].get("submissionAttempted"))
        self.checked = False
        self.last_poll = 0.0
        self.requires_user = False
        self.failed = False
        self.bootstrapped = False
        self.existing_series = False
        self.awaiting_upload = False
        self.trace = None
        self.stage = "starting"

    @property
    def record(self):
        return self.database.get(self.sha256)

    @property
    def state(self):
        record = self.record
        return {**record["details"], "contentId": record["content_id"], "episodeId": record["episode_id"]}

    async def checkpoint(self, **updates):
        changes = {"details": updates}
        if updates.get("contentId"):
            previous = self.record["content_id"]
            if previous and previous != str(updates["contentId"]):
                raise ValueError("当前页面作品 ID 与任务不同，停止")
            self.database.remember_series(self.record["series_key"], updates["contentId"])
            changes["content_id"] = str(updates["contentId"])
        self.database.update(self.sha256, **changes)

    def check_job(self, job_id):
        if job_id != self.job_id:
            raise ValueError("只允许操作本轮选定的任务 ID")

    def require_write(self):
        if self.reconcile_only:
            raise ValueError("上次提交结果不明，本轮只核对平台，不允许重新上传或提交")
        if self.failed or self.record["status"] in FINISHED:
            raise ValueError("任务已停止或已完成，禁止继续写入平台")
        if not self.checked:
            raise ValueError("请先 inspect_platform(confirm_target) 取得明确查重结果")

    async def resolve_control(self, snap, intent, labels):
        if hasattr(self.browser, 'resolve_intent'):
            return await self.browser.resolve_intent(snap, intent)
        # Compatibility for injected/test backends implementing the old contract.
        nodes = [n for n in snap['controls'] if n.get('text', '').strip() in labels
                 and (n.get('tag') == 'button' or n.get('type') == 'button') and not n.get('disabled')]
        return nodes[0] if len(nodes) == 1 else None

    async def click_control(self, snap, node, intent):
        if hasattr(self.browser, 'click_intent'):
            await self.browser.click_intent(node, intent, 'prepare', snap)
            expected = {'metadata_form'} if intent == 'add_episode' else {'video_upload', 'dialog'}
            after = await self.browser.wait_for_states(expected)
            if after.get('pageState') != 'dialog':
                await self.browser.confirm_intents(after)
        else:
            await self.browser.act(Decision(action='click', ref=node['ref']), 'prepare', snap)

    async def open(self):
        await self.browser.open()

    async def start(self):
        record = self.record
        # 同一剧前一集可能刚创建了平台作品，领取下一集时再读取最新映射。
        target = record["content_id"] or self.database.series_id(record["series_key"])
        if target:
            self.database.update(self.sha256, content_id=target)
            await self.browser.open_detail(target)
        return await self.browser.snapshot()

    async def set_stage(self, stage):
        self.stage = stage
        self.database.update(self.sha256, details={"stage": stage})
        if self.trace:
            self.trace.stage = stage
            self.trace.event("stage")
        print(f"[阶段] {stage}", flush=True)

    async def bootstrap(self):
        if self.reconcile_only:
            await self.set_stage("核对上次提交")
            await self.get_upload_status(JobParams(job_id=self.job_id))
            return
        cid = self.record["content_id"]
        if not cid:
            await self.set_stage("打开内容上传")
            await self.browser.open_upload_entry()
            await self.set_stage("平台查重")
            cid = await self.browser.lookup_series()
        if cid:
            self.database.update(self.sha256, content_id=cid)
            await self.browser.open_detail(cid)
            await self.inspect_platform(InspectParams(job_id=self.job_id))
            if self.record["status"] in FINISHED:
                return
            await self.inspect_platform(InspectParams(job_id=self.job_id, action="confirm_target"))
            self.existing_series = True
            if self.state.get("draftUrl"):
                await self.inspect_platform(InspectParams(job_id=self.job_id, action="return_to_upload"))
            else:
                snap = await self.browser.snapshot()
                if not self.browser.is_upload_form(snap):
                    button = await self.resolve_control(snap, 'add_episode', {'新增集数', '添加单集信息', '+ 添加单集信息'})
                    if button is None:
                        raise ValueError("已有作品查重通过，但无法唯一定位新增集数入口")
                    await self.click_control(snap, button, 'add_episode')
        else:
            if self.job.manifest.episodes[0].number != 1:
                raise ValueError("平台未找到已有作品，不能从非第1集新建")
            self.checked = True
            self.database.update(self.sha256, message="平台已明确无同名作品，进入上传表单")
        self.bootstrapped = True
        self.browser.phase_name = "prepare"
        await self.auto_prepare()

    def remaining_fields(self):
        required = set(self.job.fields()) - {"添加集数"}
        if self.job.manifest.sellingMode == "free":
            required.discard("单集价格")
        return sorted(required - self.browser.filled.keys())

    async def auto_prepare(self):
        if not self.bootstrapped or self.record["status"] in FINISHED or self.reconcile_only:
            return
        self.require_write()
        await self.set_stage("常规填表与附件")
        episode_fields = {"集数标题", "单集付费", "单集价格"}
        for name in self.remaining_fields():
            snap = await self.browser.snapshot()
            read_only = self.existing_series and name not in episode_fields
            nodes = field_candidates(snap, name, self.job.fields()[name], read_only=read_only)
            if len(nodes) != 1:
                continue  # 下拉、多选及歧义字段交给模型，不猜测位置。
            if read_only:
                self.browser.record_readback(nodes[0], name)
            else:
                await self.browser.act(Decision(action="fill", ref=nodes[0]["ref"], field=name), "prepare", snap)
        for role, pattern in {"copyright": r"版权|制作协议|权属|证明", "cover": r"封面|cover"}.items():
            if role in self.state.get("uploadedAssets", []):
                continue
            snap = await self.browser.snapshot()
            nodes = [n for n in snap["controls"] if n["type"] == "file" and re.search(pattern, n["label"], re.I)]
            if len(nodes) == 1:
                await self.upload_files(UploadParams(job_id=self.job_id, files=[AssetBinding(role=role, ref=nodes[0]["ref"])]))
        snap = await self.browser.snapshot()
        metadata_assets = {"copyright", "cover"}.issubset(self.state.get("uploadedAssets", []))
        if not self.remaining_fields() and metadata_assets and not self.state.get("nextClicked"):
            button = await self.resolve_control(snap, 'next', {'下一步'})
            if button is not None:
                await self.browser.verify_fields()
                await self.checkpoint(formVerified=True, cardCount=self.browser.expected_card_count, nextClicked=True)
                await self.click_control(snap, button, 'next')
        snap = await self.browser.snapshot()
        if any("审核后仅支持修改" in text for text in snap.get("dialogs", [])):
            buttons = [n for n in snap["controls"] if n["tag"] == "button" and n["text"].strip() in {"确定", "确认"}]
            if len(buttons) == 1:
                await self.browser.act(Decision(action="click", ref=buttons[0]["ref"]), "prepare", snap)
                if hasattr(self.browser, 'wait_for_states'):
                    after = await self.browser.wait_for_states({'video_upload'})
                    await self.browser.confirm_intents(after)
                else:
                    await asyncio.sleep(0.5)
        snap = await self.browser.snapshot()
        if self.state.get("formVerified") and self.record["content_id"]:
            nodes = [n for n in snap["controls"] if n["type"] == "file" and re.search(r"视频|video", n["label"], re.I)]
            if len(nodes) == 1 and "video" not in self.state.get("uploadedAssets", []):
                await self.upload_files(UploadParams(job_id=self.job_id, files=[AssetBinding(role="video", ref=nodes[0]["ref"])]))
            self.awaiting_upload = "video" in self.state.get("uploadedAssets", []) and bool(nodes or snap.get("uploadRows"))
        await self.checkpoint(filledFields=list(self.browser.filled))

    async def wait_for_upload(self):
        await self.set_stage("等待视频上传")
        deadline = time.monotonic() + self.settings.upload_timeout
        while True:
            status = await self.get_upload_status(JobParams(job_id=self.job_id))
            print(f"[上传进度] {status['status']}；完成证据：{status.get('video_complete', False)}", flush=True)
            if self.record["status"] in FINISHED:
                return
            if self.record["status"] == "ready":
                await self.submit_upload(JobParams(job_id=self.job_id))
                return
            if time.monotonic() >= deadline:
                raise TimeoutError(f"视频上传等待超过 {self.settings.upload_timeout:g} 秒，保留原附件状态")

    async def finish(self, status, message, **result):
        details = {key: value for key, value in result.items() if key not in {"contentId", "episodeId"}}
        if status == "submitted":
            details.setdefault("submittedAt", now())
        updates = {"status": status, "message": message, "details": details, "ledger_dirty": 1}
        if result.get("contentId"):
            self.database.remember_series(self.record["series_key"], result["contentId"])
            updates["content_id"] = str(result["contentId"])
        if result.get("episodeId"):
            updates["episode_id"] = str(result["episodeId"])
        self.database.update(self.sha256, **updates)
        label = "模拟准备完成" if status == "test_prepared" else ("提交结果" if status in DONE else "异常现场")
        screenshots = self.state.get("screenshots", {})
        key = "testPrepared" if status == "test_prepared" else ("result" if status in DONE else "error")
        if key not in screenshots:
            try:
                folder = self.settings.runtime_root / "screenshots" / self.job_id
                folder.mkdir(parents=True, exist_ok=True)
                shot = await self.browser.screenshot(folder, label)
                if shot:
                    self.database.update(self.sha256, details={"screenshots": {**screenshots, key: shot}})
            except Exception as exc:
                self.database.update(self.sha256, details={"screenshotError": str(exc)})
        return self.status()

    def status(self):
        record = self.record
        return {"job_id": self.job_id, "platform": self.settings.platform, "account_id": self.settings.account_id, "status": record["status"], "content_id": record["content_id"],
                "episode_id": record["episode_id"], "message": record["message"],
                "test_mode": self.job.manifest.testMode, "submission_allowed": not self.job.manifest.testMode}

    async def scan_directory(self, params):
        return {"directory": str(self.settings.directory), "counts": self.scan_summary.get("counts", {}),
                "current_job": {**self.status(), "series": self.job.manifest.seriesTitle,
                                "episode": self.job.manifest.episodes[0].number, "fields": self.job.fields(),
                                "files": {role: path.name for role, path in self.job.files.items()},
                                "resume": {key: self.state[key] for key in ("draftUrl", "uploadedAssets", "attachmentIntent", "formVerified") if key in self.state},
                                "reconcile_only": self.reconcile_only, "duplicate_checked": self.checked,
                                "remaining_fields": self.remaining_fields()}}

    async def inspect_platform(self, params):
        self.check_job(params.job_id)
        action = params.action
        snap = await self.browser.snapshot()
        if action == "open_detail":
            if not self.record["content_id"]:
                raise ValueError("没有已核对的目标作品 ID")
            await self.browser.open_detail(self.record["content_id"])
        elif action == "return_to_upload":
            url = self.state.get("draftUrl", "")
            if not self.record["content_id"] or content_id(url) != self.record["content_id"] or not url.startswith("https://kdj.kuaishou.com/"):
                raise ValueError("没有有效的原上传页 URL")
            await self.browser.page.goto(url)
            await self.browser.wait_page()
        elif action in {"search", "click", "scroll"}:
            if self.record["status"] in FINISHED:
                raise ValueError("任务已经完成，禁止继续页面操作")
            if action == "search":
                if urlsplit(snap["url"]).path.rstrip("/") != "/home/content/content-management":
                    raise ValueError("只允许在内容管理列表的搜索框填写剧名，不能修改详情页标题")
                command = Decision(action="fill", ref=params.ref, field="剧名")
            elif action == "scroll":
                command = Decision(action="scroll")
            else:
                command = Decision(action="click", ref=params.ref)
            if self.failed and action != "scroll":
                raise ValueError("任务已停止，不能继续页面操作")
            phase = "prepare" if self.checked and not self.reconcile_only else "discover"
            # 下一步只能由 fill_form 执行，确保校验与落盘发生在同一工具中。
            if action == "click" and any(n["ref"] == params.ref and n["text"].strip() == "下一步" for n in snap["controls"]):
                raise ValueError("请使用 fill_form(next_ref=...) 执行下一步")
            await self.browser.act(command, phase, snap)
        snap = await self.browser.snapshot()
        cid = content_id(snap["url"])
        if cid:
            title = self.job.manifest.seriesTitle
            if title not in snap["text"] and not any(n["value"] == title for n in snap["controls"]):
                raise ValueError("详情页剧名与任务资料不同")
            if self.record["content_id"] and cid != self.record["content_id"]:
                raise ValueError("平台作品 ID 与任务不同")
            existing = self.browser.find_episode(snap)
            if existing:
                status = "submitted" if self.reconcile_only else "exists"
                return await self.finish(status, "平台已存在目标集数，跳过上传", contentId=cid, **existing)
        if action == "confirm_target":
            if self.reconcile_only:
                raise ValueError("尚未确认上次提交结果，禁止重新提交")
            if cid:
                if re.search(r"审核中|已完结", snap["text"]):
                    raise ValueError("目标剧审核中或已完结，停止加集")
                ep = self.job.manifest.episodes[0]
                numbers = []
                available_slot = False
                for row in snap["rows"]:
                    number = re.search(r"第0*(\d+)集", row["text"])
                    if number:
                        numbers.append(int(number.group(1)))
                        if int(number.group(1)) == ep.number and re.search(r"未上传|待上传|草稿", row["text"]):
                            available_slot = True
                total = re.search(r"(?:共|已上传|上传集数[：:]?)\s*(\d+)\s*(?:条|集)", snap["text"])
                full_list = bool(total and int(total.group(1)) == len(set(numbers)))
                if not available_slot and not (full_list and ep.number not in numbers) and not re.search(r"暂无数据|暂无集数", snap["text"]):
                    raise ValueError("无法确认目标集数尚未上传，请核对完整集数列表")
                self.database.remember_series(self.record["series_key"], cid)
                self.database.update(self.sha256, content_id=cid)
                self.browser.expected_card_count = len(set(numbers)) + (0 if available_slot else 1)
            else:
                searched = any(n["value"] == self.job.manifest.seriesTitle for n in snap["controls"])
                if not searched or not re.search(r"暂无数据|暂无内容|无搜索结果|没有找到", snap["text"]):
                    raise ValueError("缺少剧名搜索后无匹配结果的明确证据")
                if self.job.manifest.episodes[0].number != 1:
                    raise ValueError("未找到已有剧，不能直接从非第1集新建")
            self.checked = True
            self.database.update(self.sha256, message="平台查重通过")
        if self.bootstrapped and self.checked and not self.reconcile_only:
            await self.auto_prepare()
            snap = await self.browser.snapshot()
        return {**self.status(), "duplicate_checked": self.checked, "page": compact_snapshot(snap)}

    async def fill_form(self, params):
        self.check_job(params.job_id)
        if not params.read_only:
            self.require_write()
        if self.reconcile_only:
            raise ValueError("结果待核对任务不能重新填表或推进向导")
        self.browser.phase_name = "prepare"
        for binding in params.fields:
            snap = await self.browser.snapshot()
            nodes = [n for n in snap["controls"] if n["ref"] == binding.ref]
            if len(nodes) != 1:
                raise ValueError("字段引用不在最新快照中")
            if params.read_only:
                self.browser.record_readback(nodes[0], binding.name)
            else:
                action = {"fill": "fill", "choose": "choose", "open": "click"}[binding.mode]
                await self.browser.act(Decision(action=action, ref=binding.ref, field=binding.name), "prepare", snap)
        if params.verify or params.next_ref:
            await self.browser.verify_fields()
            await self.checkpoint(formVerified=True, cardCount=self.browser.expected_card_count)
        if params.next_ref:
            self.require_write()
            snap = await self.browser.snapshot()
            if not any(n["ref"] == params.next_ref and n["text"].strip() == "下一步" for n in snap["controls"]):
                raise ValueError("next_ref 必须是当前下一步按钮")
            await self.checkpoint(nextClicked=True)
            await self.browser.act(Decision(action="click", ref=params.next_ref), "prepare", snap)
        await self.checkpoint(filledFields=list(self.browser.filled))
        await self.auto_prepare()
        return {**self.status(), "filled_fields": list(self.browser.filled), "form_verified": self.browser.form_verified,
                "remaining_fields": self.remaining_fields(), "page": compact_snapshot(await self.browser.snapshot())}

    async def upload_files(self, params):
        self.check_job(params.job_id)
        self.require_write()
        self.browser.phase_name = "prepare"
        recorded = set(self.state.get("uploadedAssets", []))
        if self.state.get("attachmentIntent"):
            raise ValueError("上次附件选择结果不明确，请先核对平台，禁止重复选择文件")
        for binding in params.files:
            if binding.role in recorded or binding.role in self.browser.uploaded:
                raise ValueError("该附件已经选择过，请核对上传进度，不重复选择")
            # 在选择文件前保存意图；进程此时断开后，不猜测文件是否已经传输。
            self.database.update(self.sha256, status="uploading", details={"attachmentIntent": binding.role})
            await self.browser.act(Decision(action="upload", ref=binding.ref, asset=binding.role), "prepare", await self.browser.snapshot())
            recorded.add(binding.role)
            await self.checkpoint(uploadedAssets=sorted(recorded), attachmentIntent=None)
        return {**self.status(), "uploaded_assets": sorted(recorded), "message": "文件已选择，get_upload_status 检查进度"}

    async def get_upload_status(self, params):
        self.check_job(params.job_id)
        if self.record["status"] in FINISHED:
            return self.status()
        if self.reconcile_only:
            result = await self.browser.reconcile(self.state)
            if result:
                return await self.finish("submitted", "已核对上次提交结果，未重复提交", **result)
            return {**self.status(), "message": "提交结果仍待核对；不要重新上传或提交"}
        if "video" in self.state.get("uploadedAssets", []):
            delay = 30 - (time.monotonic() - self.last_poll)
            if delay > 0:
                await asyncio.sleep(delay)
            self.last_poll = time.monotonic()
        snap = await self.browser.snapshot()
        if re.search(r"上传失败|文件过大|格式不支持", snap["text"]):
            raise ValueError("页面报告附件上传失败")
        complete = self.browser.video_complete(snap, self.job.files["video"].name)
        selected = set(self.state.get("uploadedAssets", [])) | self.browser.uploaded
        if complete and selected == set(self.job.files) and self.state.get("formVerified") and self.record["content_id"]:
            self.database.update(self.sha256, status="ready", details={"uploadEvidence": self.job.files["video"].name}, message="上传完成，等待最终复核")
            if self.job.manifest.testMode:
                await self.complete_test_preparation()
        return {**self.status(), "video_complete": complete, "page": compact_snapshot(snap)}

    async def complete_test_preparation(self):
        await self.browser.verify_prepared(self.state)
        return await self.finish("test_prepared", "模拟准备完成：表单与附件上传已核对，未点击最终提交",
                                 testMode=True, preparedAt=now())

    async def submit_upload(self, params):
        self.check_job(params.job_id)
        if self.job.manifest.testMode and self.record["status"] == "test_prepared":
            return self.status()
        self.require_write()
        if self.record["status"] != "ready":
            raise ValueError("上传尚未完成或附件/元数据尚未核对")
        if self.job.manifest.testMode:
            return await self.complete_test_preparation()
        await self.browser.verify_prepared(self.state)
        # 数据库提交意图先于最终页面点击；异常后只能核对，不能自动重试。
        self.database.update(self.sha256, status="submitting", details={"submissionAttempted": True, "submitStartedAt": now()})
        result = await self.browser.submit(self.state)
        if result:
            return await self.finish("submitted", "平台已确认目标集数审核状态", **result)
        self.reconcile_only = True
        return await self.finish("unknown", "已触发提交但未确认结果，下次先核对")

    async def fail(self, exc):
        self.failed = True
        message = str(exc)
        if self.trace:
            self.trace.error(exc)
            try:
                from ..state import atomic_json
                snap = await self.browser.snapshot()
                atomic_json(self.trace.folder / "page.json", snap)
            except Exception as snapshot_error:
                self.trace.event("snapshot_error", message=str(snapshot_error))
            message = f"[{self.stage}] {type(exc).__name__}: {message}；日志：{self.trace.folder}"
        if re.search(r"登录|验证码|安全验证", message):
            self.requires_user = True
        if getattr(exc, 'code', '') in {'agreement', 'account_mismatch', 'permission_denied'}:
            self.requires_user = True
        if getattr(exc, 'code', None):
            await self.checkpoint(errorCode=exc.code)
        record = self.record
        status = record["status"] if record["status"] in FINISHED else (
            "unknown" if record["status"] in UNCERTAIN or record["details"].get("submissionAttempted") else "blocked")
        return await self.finish(status, message)

