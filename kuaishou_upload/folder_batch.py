"""One folder/series, one form, one multi-file selection; receipts remain per hash."""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from pathlib import Path

from .contracts import AssetBinding, Decision, InspectParams, JobParams, UploadParams
from .database import DONE, FINISHED, UNCERTAIN
from .platforms import get_adapter
from .platforms.kuaishou_browser import content_id, field_candidates
from .platforms.kuaishou_workflow import KuaishouUploadTask
from .state import now
from .prepare import generated_synopsis

EPISODE_FIELDS = {'集数标题', '单集付费', '单集价格', '添加集数'}


def folder_groups(ordered, database):
    groups = {}
    for sha, job in ordered:
        if database.get(sha)['status'] in FINISHED:
            continue
        key = (str(job.files['video'].parent.resolve()).casefold(), get_adapter(database.platform).series_key(job))
        groups.setdefault(key, []).append((sha, job))
    return list(groups.values())


class FolderBatchTask(KuaishouUploadTask):
    def __init__(self, members, settings, database, summary, *, browser=None):
        self.members = sorted(members, key=lambda p: p[1].manifest.episodes[0].number)
        self.active = list(self.members)
        self.batch_prepared = False
        self.batch_checked = False
        self.batch_card_count = 0
        self.existing_slots = set()
        self.batch_id = hashlib.sha256(('\0'.join(sha for sha, _ in self.members)).encode()).hexdigest()[:24]
        self.batch_public_fields = {}
        first_sha, first_job = self.members[0]
        super().__init__(first_job, first_sha, settings, database, summary, browser=browser)
        self.browser.batch_specs = [self.spec(sha, job) for sha, job in self.members]
        self.browser.batch_hashes = []

    @staticmethod
    def spec(sha, job):
        ep = job.manifest.episodes[0]
        return dict(sha256=sha, number=ep.number, title=ep.title, paid=ep.paid, price=ep.price,
                    path=str(job.files['video']), filename=job.files['video'].name)

    def validate_members(self):
        baseline = None
        numbers, filenames = set(), set()
        root = self.job.files['video'].parent.resolve()
        for sha, job in self.members:
            record = self.database.get(sha)
            if record['status'] in UNCERTAIN or record['details'].get('submissionAttempted') or record['details'].get('attachmentIntent') or record['details'].get('batchSelectionAttempted'):
                raise ValueError('该文件夹存在上传或提交结果待核对的批次，本轮仅核对，禁止重新批量导入')
            if record['details'].get('uploadedAssets'):
                raise ValueError('批次包含已有附件或草稿，请先核对原草稿，禁止重新批量导入')
            if job.files['video'].parent.resolve() != root:
                raise ValueError('不同文件夹不能合并为一个上传批次')
            ep = job.manifest.episodes[0]
            if ep.number in numbers or job.files['video'].name.casefold() in filenames:
                raise ValueError('批次集号或文件名冲突，无法建立唯一文件回执')
            numbers.add(ep.number); filenames.add(job.files['video'].name.casefold())
            shared = {k: v for k, v in job.fields().items() if k not in EPISODE_FIELDS}
            if generated_synopsis(shared.get('作品简介'), job.manifest.seriesTitle, ep.number):
                if not job.manifest.testMode:
                    raise ValueError('待补资料：作品简介仅为自动占位文本，正式上传需要有来源的整剧简介')
                shared['作品简介'] = f'《{job.manifest.seriesTitle}》。'
            # Per-episode generated thumbnails may differ. The first pending
            # episode supplies the series cover; rights proof must agree.
            signature = {**shared, '模拟模式':job.manifest.testMode, '版权证明SHA-256':job.asset_hashes.get('copyright')}
            if baseline is not None and signature != baseline:
                conflicts = sorted(k for k in set(signature) | set(baseline) if signature.get(k) != baseline.get(k))
                raise ValueError(f"同一文件夹公共资料冲突：{'、'.join(conflicts)}（第{self.members[0][1].manifest.episodes[0].number}集与第{ep.number}集）；停止合批")
            baseline = signature
        self.batch_public_fields = {k:v for k,v in baseline.items() if k not in {'模拟模式','版权证明SHA-256'}}

    def activate_member(self):
        self.sha256, self.job = self.active[0]
        self.job_id = self.database.task_id(self.sha256)
        self.browser.job = self.job
        self.browser.fields = {**self.job.fields(), **self.batch_public_fields}

    async def checkpoint(self, **updates):
        await super().checkpoint(**updates)
        for sha, _ in self.active:
            if sha == self.sha256:
                continue
            changes = {'details': updates}
            if updates.get('contentId'):
                old = self.database.get(sha)['content_id']
                if old and old != str(updates['contentId']):
                    raise ValueError('批次作品ID冲突')
                changes['content_id'] = str(updates['contentId'])
            self.database.update(sha, **changes)

    def found(self, job, snap):
        previous = self.browser.job
        try:
            self.browser.job = job
            return self.browser.find_episode(snap)
        finally:
            self.browser.job = previous

    async def bootstrap(self):
        self.validate_members()
        targets = {self.database.get(sha)['content_id'] for sha, _ in self.members if self.database.get(sha)['content_id']}
        if len(targets) > 1:
            raise ValueError('文件夹批次对应多个作品ID，禁止自动合并')
        cid = next(iter(targets), None)
        if not cid:
            await self.set_stage('文件夹批次查重')
            cid = await self.browser.lookup_series()
        if cid:
            await self.set_stage('一次核对整剧集数')
            await self.browser.open_detail(cid)
            snap = await self.browser.snapshot()
            if content_id(snap['url']) != str(cid) or self.job.manifest.seriesTitle not in snap['text']:
                raise ValueError('作品ID或剧名与文件夹批次不一致')
            # A closed series must not trigger a visual next-page search merely to try appending.
            closed = bool(re.search(r'审核中|已完结|是否完结\s*[：:]?\s*是', snap['text']))
            if not closed:
                snap = await self.browser.episode_inventory(snap)
                if not snap.get('episodeInventoryComplete'):
                    raise ValueError('未取得完整平台集数清单，禁止批量追加')
            for sha, job in self.members:
                evidence = self.found(job, snap)
                if evidence:
                    self.database.update(sha, status='exists', content_id=str(cid),
                        episode_id=evidence.get('episodeId'), ledger_dirty=1,
                        message='平台已存在目标集数，跳过导入', details=evidence)
            self.active = [(sha, job) for sha, job in self.members if self.database.get(sha)['status'] not in DONE]
            if not self.active:
                return
            self.activate_member()
            if closed:
                raise ValueError('目标剧审核中或已完结，停止批量加集；未读取页的集数不猜测为已上传')
            self.existing_series = True
            numbers = {int(m.group(1)) for row in snap['rows'] if (m := re.search(r'第0*(\d+)集', row['text']))}
            if not numbers:
                numbers = {int(row['cells'][0]) for row in snap['rows'] if row.get('cells') and row['cells'][0].isdigit()}
            self.batch_card_count = len(numbers)
            self.existing_slots = {job.manifest.episodes[0].number for _, job in self.active if job.manifest.episodes[0].number in numbers}
            for number in self.existing_slots:
                rows = [r for r in snap['rows'] if re.search(rf'第0*{number}集(?:\D|$)',r['text']) or (r.get('cells') and r['cells'][0]==str(number))]
                if len(rows)!=1 or not re.search(r'未上传|待上传|草稿',rows[0]['text']):
                    raise ValueError(f'第{number}集已有卡片状态不明确，禁止覆盖')
            if not self.batch_card_count and snap['rows'] and not re.search(r'暂无数据|暂无集数', snap['text']):
                raise ValueError('平台已有卡片集号无法核实')
            for sha, _ in self.active:
                self.database.update(sha, content_id=str(cid))
            self.database.remember_series(self.record['series_key'], cid)
        # Anchor the shared form to the first actually missing episode.
        self.activate_member()
        self.browser.batch_hashes = [sha for sha, _ in self.active]
        if cid:
            snap = await self.browser.snapshot()
            if not self.browser.is_upload_form(snap) or '/detail' in snap['url']:
                node = await self.resolve_control(snap, 'add_episode', {'新增集数', '添加单集信息', '+ 添加单集信息'})
                if node is None:
                    raise ValueError('已有作品查重通过，但无法定位新增集数入口')
                await self.click_control(snap, node, 'add_episode')
        else:
            await self.set_stage('打开新建作品上传')
            await self.browser.open_upload_entry()
        self.checked = self.bootstrapped = True
        self.browser.phase_name = 'prepare'
        for sha, _ in self.active:
            self.database.update(sha, details={'folderBatchId': self.batch_id, 'folderBatchMembers': self.browser.batch_hashes},
                                 message='文件夹批次查重通过，进入同一作品上传表单')
        await self.auto_prepare()

    def remaining_fields(self):
        required = set(self.browser.fields) - EPISODE_FIELDS
        return sorted(required - self.browser.filled.keys())

    async def auto_prepare(self):
        if not self.bootstrapped or self.record['status'] in FINISHED:
            return
        self.require_write()
        if not self.batch_prepared:
            await self.set_stage('批量建立单集卡片')
            result = await self.browser.rpc.call('prepare_batch', pageId=self.browser.page._target_id,
                hashes=self.browser.batch_hashes, totalCards=self.batch_card_count + len(self.active) - len(self.existing_slots),
                newSeries=not self.existing_series)
            self.browser.expected_card_count = result['count']
            self.batch_prepared = True
        await self.set_stage('整剧公共资料与附件')
        for name in self.remaining_fields():
            snap = await self.browser.snapshot()
            nodes = field_candidates(snap, name, self.browser.fields[name], read_only=self.existing_series)
            if len(nodes) == 1:
                if self.existing_series:
                    self.browser.record_readback(nodes[0], name)
                else:
                    await self.browser.act(Decision(action='fill',ref=nodes[0]['ref'],field=name),'prepare',snap)
        for role, pattern in ({} if self.existing_series else {'copyright':r'版权|制作协议|权属|证明','cover':r'封面|cover'}).items():
            if role in self.state.get('uploadedAssets',[]):
                continue
            snap = await self.browser.snapshot()
            nodes = [n for n in snap['controls'] if n['type']=='file' and re.search(pattern,n['label'],re.I)]
            if len(nodes)==1:
                await super().upload_files(UploadParams(job_id=self.job_id,files=[AssetBinding(role=role,ref=nodes[0]['ref'])]))
        if self.remaining_fields() or (not self.existing_series and not {'copyright','cover'}.issubset(self.state.get('uploadedAssets',[]))):
            return
        if not self.state.get('nextClicked'):
            snap = await self.browser.snapshot()
            node = await self.resolve_control(snap,'next',{'下一步'})
            if node is None:
                return
            await self.browser.verify_fields()
            await self.checkpoint(formVerified=True, cardCount=self.browser.expected_card_count, nextClicked=True)
            await self.click_control(snap,node,'next')
        snap = await self.browser.snapshot()
        if any('审核后仅支持修改' in d for d in snap.get('dialogs',[])):
            nodes = [n for n in snap['controls'] if n['tag']=='button' and n['text'].strip() in {'确定','确认'}]
            if len(nodes)!=1:
                return
            await self.browser.act(Decision(action='click',ref=nodes[0]['ref']),'prepare',snap)
            snap = await self.browser.snapshot()
        cid = self.record['content_id'] or content_id(snap['url'])
        if not cid:
            return
        if not self.state.get('contentId'):
            await self.checkpoint(contentId=cid, draftUrl=snap['url'])
        if not self.state.get('batchSelectionAttempted'):
            await self.set_stage(f'一次导入{len(self.active)}个视频')
            for sha, _ in self.active:
                self.database.update(sha,status='uploading',details={'attachmentIntent':'video','batchSelectionAttempted':True})
            result = await self.browser.rpc.call('batch_files',pageId=self.browser.page._target_id,hashes=self.browser.batch_hashes)
            if result.get('selected') != len(self.active):
                raise ValueError('批量选择文件数量不一致，结果待核对')
            for sha, _ in self.active:
                assets = set(self.database.get(sha)['details'].get('uploadedAssets', [])) | {'video'}
                self.database.update(sha,details={'attachmentIntent':None,'uploadedAssets':sorted(assets)})
        self.awaiting_upload = True

    async def wait_for_upload(self):
        await self.set_stage('核对整批上传结果')
        deadline = time.monotonic()+self.settings.upload_timeout
        while True:
            snap = await self.browser.snapshot()
            if re.search(r'上传失败|文件过大|格式不支持',snap['text']):
                raise ValueError('页面报告批次附件上传失败，保留现场，禁止整批重传')
            complete = [(sha,job) for sha,job in self.active if self.browser.video_complete(snap,job.files['video'].name)]
            print(f'[批量进度] 已核实{len(complete)}/{len(self.active)}个文件上传完成',flush=True)
            for sha,job in complete:
                self.database.update(sha,status='ready',details={'uploadEvidence':job.files['video'].name},message='该文件上传完成，等待整批核对')
            if len(complete)==len(self.active):
                if self.job.manifest.testMode:
                    await self.browser.verify_prepared(self.state)
                    for sha, _ in self.active:
                        self.database.update(sha,status='test_prepared',ledger_dirty=1,message='整批模拟准备完成，附件逐个核实，未最终提交',details={'preparedAt':now()})
                    return
                # Exactly one shared submit. Mark every member before the click.
                for sha, _ in self.active:
                    self.database.update(sha,status='submitting',details={'submissionAttempted':True,'submitStartedAt':now()})
                await self.browser.submit(self.state)
                await self.browser.open_detail(self.record['content_id'])
                self.browser._episode_inventory = None
                after = await self.browser.episode_inventory(await self.browser.snapshot())
                for sha,job in self.active:
                    evidence = self.found(job,after)
                    self.database.update(sha,status='submitted' if evidence else 'unknown',ledger_dirty=1,
                        message='平台已确认该集审核状态' if evidence else '整批已提交，该集结果待核对，禁止重新上传',
                        details={**(evidence or {}),'submittedAt':now()} if evidence else {})
                return
            if time.monotonic()>=deadline:
                raise TimeoutError('整批上传等待超时，已保留每个文件状态，禁止整批重传')
            await asyncio.sleep(2)

    async def fail(self, exc):
        if self.record['status'] not in FINISHED and self.state.get('batchSelectionAttempted'):
            self.database.update(self.sha256, status='unknown')
        result = await super().fail(exc)
        for sha, _ in self.members:
            if sha == self.sha256 or self.database.get(sha)['status'] in FINISHED:
                continue
            old = self.database.get(sha)
            uncertain = old['details'].get('batchSelectionAttempted') or old['details'].get('submissionAttempted')
            self.database.update(sha,status='unknown' if uncertain else ('blocked' if result['status'] in FINISHED else result['status']),ledger_dirty=1,
                message=result['message'],details={'errorCode':self.state.get('errorCode'),'folderBatchId':self.batch_id})
        return result

    def results(self):
        return [{k:record.get(k) for k in ('status','content_id','episode_id','message')} | {
            'job_id':self.database.task_id(sha),'platform':self.settings.platform,'account_id':self.settings.account_id,
            'test_mode':job.manifest.testMode,'submission_allowed':not job.manifest.testMode}
            for sha,job in self.members if (record:=self.database.get(sha))]
