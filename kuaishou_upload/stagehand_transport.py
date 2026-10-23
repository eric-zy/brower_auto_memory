"""Local JSON-lines bridge. No credentials in argv and no retries after unknown outcomes."""
from __future__ import annotations
import asyncio
import json
import os
import shutil
import subprocess
from pathlib import Path
from .page_state import PageBlocked


class StagehandTransport:
    def __init__(self, root: Path, *, timeout=120):
        self.root, self.timeout = root, timeout
        self.process = None
        self.counter = 0
        self.lock = asyncio.Lock()
        self.stderr_task = None
        self.stderr_tail = []
        self.failed = False

    async def start(self):
        node = shutil.which('node')
        folder = Path(__file__).resolve().parents[1] / 'stagehand'
        if not node or not (folder / 'node_modules/@browserbasehq/stagehand/package.json').exists():
            raise RuntimeError('Stagehand依赖未安装：请在stagehand目录执行 npm ci --ignore-scripts')
        self.process = await asyncio.create_subprocess_exec(node, str(folder / 'worker.mjs'), cwd=folder,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            limit=16*1024*1024, creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        self.stderr_task = asyncio.create_task(self._read_stderr())

    async def _read_stderr(self):
        async for raw in self.process.stderr:
            text = raw.decode('utf-8', errors='replace').strip()
            for secret in (os.getenv('OPENAI_API_KEY'), os.getenv('STAGEHAND_API_KEY')):
                if secret:
                    text = text.replace(secret, '[redacted]')
            self.stderr_tail = (self.stderr_tail + [text])[-30:]

    async def call(self, method, **params):
        if self.failed:
            raise RuntimeError('Stagehand进程结果不明，禁止自动重试页面动作')
        async with self.lock:
            self.counter += 1
            request = {'id': self.counter, 'method': method, 'params': params}
            try:
                self.process.stdin.write((json.dumps(request, ensure_ascii=False)+'\n').encode('utf-8'))
                await self.process.stdin.drain()
                line = await asyncio.wait_for(self.process.stdout.readline(), self.timeout)
                if not line:
                    raise RuntimeError('Stagehand进程提前退出：'+'；'.join(self.stderr_tail[-3:]))
                response = json.loads(line)
                if response.get('id') != self.counter:
                    raise RuntimeError('Stagehand返回请求身份不匹配')
            except BaseException:
                self.failed = True
                await self.terminate()
                raise
            if response.get('error'):
                error = response['error']
                raise PageBlocked(error['code'], error['message'])
            return response.get('result')

    async def terminate(self):
        if self.process and self.process.returncode is None:
            self.process.kill()
            await self.process.wait()
        if self.stderr_task:
            self.stderr_task.cancel()

    async def close(self):
        try:
            if self.process and not self.failed and self.process.returncode is None:
                await asyncio.wait_for(self.call('close'), 5)
                await asyncio.wait_for(self.process.wait(), 3)
        finally:
            await self.terminate()


class StagehandElement:
    def __init__(self, page, ref):
        self.page, self.ref = page, ref

    async def click(self):
        final = self.page.final_submit_allowed
        self.page.final_submit_allowed = False
        return await self.page.rpc.call('click', pageId=self.page._target_id, ref=self.ref, final=final)

    async def set_input_files(self, files):
        roles = [role for role, candidate in self.page.files.items() if files == [str(candidate)]]
        if len(roles)!=1:
            raise ValueError('文件不在本任务白名单')
        return await self.page.rpc.call('files', pageId=self.page._target_id, ref=self.ref, asset=roles[0])


class StagehandPage:
    def __init__(self, rpc, page_id, files):
        self.rpc, self._target_id, self.files = rpc, page_id, files
        self.final_submit_allowed = False

    async def evaluate(self, source, *args):
        from .observation import SNAPSHOT
        if source == SNAPSHOT:
            value = await self.rpc.call('snapshot', pageId=self._target_id)
        else:
            value = await self.rpc.call('evaluate', pageId=self._target_id, source=source, args=list(args))
        if value is None:
            return ''
        return json.dumps(value, ensure_ascii=False) if isinstance(value, (dict,list)) else str(value)

    async def get_url(self):
        return await self.rpc.call('get_url', pageId=self._target_id)

    async def goto(self, url):
        return await self.rpc.call('goto', pageId=self._target_id, url=url)

    async def screenshot(self):
        return await self.rpc.call('screenshot', pageId=self._target_id)

    async def get_elements_by_css_selector(self, selector):
        refs = await self.rpc.call('selectors', pageId=self._target_id, selector=selector)
        return [StagehandElement(self, ref) for ref in refs]


class StagehandSession:
    def __init__(self, rpc, files):
        self.rpc, self.files = rpc, files

    async def new_page(self, url):
        result = await self.rpc.call('new_page', url=url)
        return StagehandPage(self.rpc, result['pageId'], self.files)

    async def close_page(self, page):
        await self.rpc.call('close_page', pageId=page._target_id)

    async def get_or_create_cdp_session(self, target_id, **kwargs):
        await self.rpc.call('focus', pageId=target_id)
        return None
