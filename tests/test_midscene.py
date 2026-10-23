import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from kuaishou_upload.config import Settings
from kuaishou_upload.platforms import get_adapter
from kuaishou_upload.platforms.midscene_browser import MidscenePlatformBrowser
from kuaishou_upload.midscene_transport import MidsceneTransport
from kuaishou_upload.browser_runtime import BrowserSessionPool


class MidsceneTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_backend_switch_keeps_both_existing_options(self):
        settings = Settings(self.root, self.root / 'state', self.root / 'ledger.xlsx')
        from kuaishou_upload.platforms.stagehand_browser import StagehandPlatformBrowser
        self.assertIsInstance(get_adapter('kuaishou').create_browser(None, settings), StagehandPlatformBrowser)
        settings.browser_backend = 'midscene'
        self.assertIsInstance(get_adapter('kuaishou').create_browser(None, settings), MidscenePlatformBrowser)
        with patch.dict(os.environ, {'UPLOAD_BROWSER_BACKEND': 'midscene'}, clear=True):
            self.assertEqual(Settings.from_env().browser_backend, 'midscene')
        settings.browser_backend = 'browser_use'
        self.assertNotIsInstance(get_adapter('kuaishou').create_browser(None, settings), StagehandPlatformBrowser)

    async def test_no_cache_write_when_form_is_not_verified(self):
        browser = MidscenePlatformBrowser(None, self.root)
        browser.page = SimpleNamespace(_target_id='T')
        browser.rpc = SimpleNamespace(call=AsyncMock(return_value={'verified': False}))
        with self.assertRaisesRegex(RuntimeError, '表单证据'):
            await browser.open_upload_entry()
        self.assertEqual(browser.rpc.call.await_count, 1)
        self.assertEqual(browser.worker_cache_file(), self.root / 'midscene/cache')
        self.assertEqual(browser.create_transport().worker_folder, 'midscene')

    async def test_two_episodes_reuse_owner_tab_with_separate_file_grants(self):
        page = SimpleNamespace(_target_id='TASK', goto=AsyncMock(), evaluate=AsyncMock(return_value='complete'))
        owner = SimpleNamespace(cdp_url='http://127.0.0.1:12345', start=AsyncMock(), kill=AsyncMock(),
                                get_current_page=AsyncMock(return_value=page))
        transports = [SimpleNamespace(start=AsyncMock(), close=AsyncMock(), terminate=AsyncMock(),
                        call=AsyncMock(return_value={'pageId': 'TASK'})) for _ in range(2)]
        with patch('browser_use.Browser', return_value=owner) as launch, \
             patch('kuaishou_upload.platforms.midscene_browser.MidsceneTransport', side_effect=transports):
            async with BrowserSessionPool(self.root) as pool:
                for name in ('1.mp4', '2.mp4'):
                    browser = MidscenePlatformBrowser(SimpleNamespace(files={'video': self.root / name}, fields=lambda: {}), self.root)
                    browser.session_pool = pool
                    await browser.open()
                    await browser.close()
                    owner.kill.assert_not_awaited()
                    self.assertIs(pool.page, page)
            self.assertEqual(launch.call_count, 1)
        owner.kill.assert_awaited_once()
        for transport, name in zip(transports, ('1.mp4', '2.mp4')):
            self.assertEqual(transport.call.await_args.kwargs['files'], {'video': str(self.root / name)})

    async def test_transport_uses_same_unknown_outcome_no_retry_rule(self):
        transport = MidsceneTransport(self.root)
        transport.failed = True
        with self.assertRaisesRegex(RuntimeError, 'Midscene.*禁止自动重试'):
            await transport.call('files')

    async def test_incomplete_episode_pages_stop_before_any_write(self):
        browser = MidscenePlatformBrowser(None, self.root)
        browser.page = SimpleNamespace(_target_id='T')
        browser.rpc = SimpleNamespace(call=AsyncMock(return_value={'complete': False, 'pages': 1, 'count': 10, 'expected': 51}))
        with self.assertRaisesRegex(ValueError, '完整集数列表'):
            await browser.episode_inventory({'url': 'https://kdj.kuaishou.com/detail?miniSeriesId=C1'})

    async def test_explicit_completion_label_blocks_adding_even_without_literal_completed_word(self):
        from support import make_materials
        from test_upload import FakeBrowser
        from kuaishou_upload.database import UploadDatabase
        from kuaishou_upload.scan import scan_directory
        from kuaishou_upload.tools import UploadTask, InspectParams
        directory = self.root / 'videos'
        make_materials(directory)
        settings = Settings(directory, self.root / 'state', self.root / 'ledger.xlsx', stable_seconds=0)
        db = UploadDatabase(settings.root / 'uploads.sqlite3')
        try:
            scan = scan_directory(settings, db)
            sha, job = next(iter(scan.jobs.items()))
            db.update(sha, content_id='C1')
            browser = FakeBrowser(job)
            browser.snap.update(url='https://kdj.kuaishou.com/detail?miniSeriesId=C1',
                text=job.manifest.seriesTitle + '\n是否完结\n是\n上传集数/总集数：51/51')
            context = UploadTask(job, sha, settings, db, {}, browser=browser)
            with self.assertRaisesRegex(ValueError, '已完结'):
                await context.inspect_platform(InspectParams(job_id=context.job_id, action='confirm_target'))
            browser.act.assert_not_awaited()
        finally:
            db.close()
