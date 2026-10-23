"""Visual execution with the existing metadata, file and submission contracts."""
from .stagehand_browser import StagehandPlatformBrowser
from ..midscene_transport import MidsceneTransport


class MidscenePlatformBrowser(StagehandPlatformBrowser):
    backend = 'midscene'

    async def verify_fields(self):
        if not getattr(self, 'batch_hashes', None):
            return await super().verify_fields()
        await self.rpc.call('verify_batch', pageId=self.page._target_id, hashes=self.batch_hashes)
        previous = self.fields
        try:
            from ..folder_batch import EPISODE_FIELDS
            self.fields = {k:v for k,v in previous.items() if k not in EPISODE_FIELDS}
            await super().verify_fields()
        finally:
            self.fields = previous

    async def verify_prepared(self, state):
        snap = await super().verify_prepared(state)
        if getattr(self, 'batch_hashes', None):
            import re
            jobs = [s for s in self.batch_specs if s['sha256'] in self.batch_hashes]
            for job in jobs:
                rows = self.upload_rows_for(snap, job['filename'])
                if len(rows) != 1 or not self.video_complete({'uploadRows':rows}, job['filename']):
                    raise ValueError('整批视频必须各自有唯一上传完成回执')
                observed = re.search(r'第\s*0*(\d+)\s*集', rows[0]['text'].replace(job['filename'], ''))
                if not observed or int(observed.group(1)) != job['number']:
                    raise ValueError(f"无法核实{job['filename']}对应平台第{job['number']}集，停止最终提交")
        return snap

    async def episode_inventory(self, snap):
        from .kuaishou_browser import content_id
        cid = content_id(snap['url'])
        cached = getattr(self, '_episode_inventory', None)
        if cached and cached['contentId'] == cid:
            return {**snap, 'rows': cached['rows'], 'episodeInventoryComplete': True}
        result = await self.rpc.call('read_inventory', pageId=self.page._target_id, contentId=cid)
        if not result.get('complete'):
            raise ValueError(f"无法确认完整集数列表：已读取{result.get('pages', 0)}页、{result.get('count', 0)}集，预计{result.get('expected')}集")
        current = await self.observe_state(result['page'])
        self._episode_inventory = {'contentId': cid, 'rows': current['rows']}
        self.last_snapshot = current
        await self.emit('midscene_episode_inventory', pages=result['pages'], count=result['count'])
        return current

    async def act(self, decision, phase, snap):
        if decision.action == 'upload':
            self._episode_inventory = None
        return await super().act(decision, phase, snap)

    async def reconcile(self, state):
        if not state.get('contentId'):
            return None
        self._episode_inventory = None
        await self.open_detail(state['contentId'])
        snap = await self.episode_inventory(await self.snapshot())
        found = self.find_episode(snap)
        return {**found, 'contentId': state['contentId']} if found else None

    def create_transport(self):
        return MidsceneTransport(self.root)

    def worker_cache_file(self):
        return self.root / 'midscene' / 'cache'

    async def commit_cache(self):
        if self.rpc:
            metrics = await self.rpc.call('commit_cache', pageId=self.page._target_id)
            await self.emit('midscene_verified_cache', **metrics)

    async def confirm_intents(self, snap):
        await super().confirm_intents(snap)
        await self.commit_cache()

    async def write_field(self, node, key):
        await super().write_field(node, key)
        await self.commit_cache()

    async def open_upload_entry(self):
        result = await self.rpc.call('ensure_upload_form', pageId=self.page._target_id, maxSteps=5)
        if not result.get('verified'):
            raise RuntimeError('Midscene未取得上传表单证据')
        await self.emit('midscene_form_verified', strategy=result.get('strategy'), metrics=result.get('metrics'))
        return await self.snapshot()
