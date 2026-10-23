from __future__ import annotations
import json
from .kuaishou_browser import PlatformBrowser
from ..observation import SNAPSHOT
from ..stagehand_transport import StagehandTransport, StagehandPage, StagehandSession
from ..locator import signature, safe_candidate


class StagehandPlatformBrowser(PlatformBrowser):
    backend = 'stagehand'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.rpc = None
        self.owner_browser = None

    async def open(self):
        # Browser Use only owns Chrome lifecycle/profile; Stagehand owns page/frame execution.
        await super().open()
        self.owner_browser = self.browser
        self.rpc = StagehandTransport(self.root)
        try:
            await self.rpc.start()
            result = await self.rpc.call('init', cdpUrl=self.owner_browser.cdp_url, targetId=self.page._target_id,
                allowedHosts=sorted(self.allowed_hosts), snapshotScript=SNAPSHOT,
                expectedAccount=self.expected_account,
                files={k:str(v) for k,v in self.job.files.items()}, cacheFile=str(self.root/'stagehand-locators.json'))
            self.browser = StagehandSession(self.rpc, self.job.files)
            self.page = StagehandPage(self.rpc, result['pageId'], self.job.files)
        except BaseException:
            await self.rpc.terminate()
            await self.owner_browser.kill()
            raise

    async def open_upload_entry(self):
        result = await self.rpc.call('ensure_upload_form', pageId=self.page._target_id, maxSteps=8)
        if not result.get('verified'):
            raise RuntimeError('Stagehand未取得上传表单证据')
        await self.emit('stagehand_form_verified', strategy=result.get('strategy'), frames=result['page'].get('frames'))
        return await self.snapshot()

    async def handle_special_controls(self, context):
        from ..contracts import FormParams, FieldBinding
        for _ in range(12):
            if context.awaiting_upload or context.record['status'] in {'test_prepared','submitted','exists'}:
                return
            changed = False
            for name in context.remaining_fields():
                binding = await self.rpc.call('field_binding', pageId=self.page._target_id, name=name,
                    value=self.fields[name], selected=name in self.pending_choices)
                if binding:
                    await context.fill_form(FormParams(job_id=context.job_id, fields=[FieldBinding(**binding)]))
                    changed = True
                    break
            if not changed:
                await context.auto_prepare()
                return
        raise RuntimeError('特殊控件恢复达到上限，保留现场后停止')

    async def resolve_intent(self, snap, intent, *, recover=True):
        aliases = self.intents[intent]
        try:
            node = self.locator.resolve(snap, intent, aliases)
        except ValueError:
            node = None
        if node is None and recover:
            result = await self.rpc.call('resolve_intent', pageId=self.page._target_id, intent=intent, aliases=list(aliases))
            if result:
                # Refresh after model observation; only current controls may be used.
                current = await self.snapshot()
                matches = [n for n in current['controls'] if n['ref'] == result['ref'] and safe_candidate(n,intent)]
                if len(matches)==1:
                    node = matches[0]
                    snap.update(current)
        if node and intent != 'search_field':
            self.intent_grants[(node['ref'],intent)] = (snap['url'],signature(node))
        return node

    async def submit(self, state):
        # Python has already durably saved submissionAttempted before calling this method.
        if self.job.manifest.testMode:
            raise ValueError('模拟模式禁止最终提交')
        self.page.final_submit_allowed = True
        try:
            return await super().submit(state)
        finally:
            self.page.final_submit_allowed = False

    async def close(self):
        try:
            if self.rpc:
                await self.rpc.close()
        finally:
            if self.owner_browser:
                await self.owner_browser.kill()
            elif self.browser:
                await super().close()
