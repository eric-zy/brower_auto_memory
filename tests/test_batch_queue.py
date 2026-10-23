import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from kuaishou_upload.browser_runtime import BrowserSessionPool
from kuaishou_upload.config import Settings
from kuaishou_upload.database import UploadDatabase
from kuaishou_upload.platforms.stagehand_browser import StagehandPlatformBrowser
from kuaishou_upload.scan import scan_directory
from support import make_materials
from test_upload import FakeBrowser


class SessionReuseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.page = SimpleNamespace(_target_id='TASK', goto=AsyncMock(), evaluate=AsyncMock(return_value='complete'))
        self.owner = SimpleNamespace(cdp_url='http://127.0.0.1:12345', start=AsyncMock(), kill=AsyncMock(),
            get_current_page=AsyncMock(return_value=self.page))

    def task(self, pool, filename):
        job = SimpleNamespace(files={'video': self.root / filename}, fields=lambda: {})
        browser = StagehandPlatformBrowser(job, self.root)
        browser.session_pool = pool
        return browser

    async def test_one_chrome_and_tab_with_fresh_file_whitelist_for_each_episode(self):
        transports = [SimpleNamespace(start=AsyncMock(), close=AsyncMock(), terminate=AsyncMock(),
            call=AsyncMock(return_value={'pageId': 'TASK'})) for _ in range(2)]
        with patch('browser_use.Browser', return_value=self.owner) as launch, \
             patch('kuaishou_upload.platforms.stagehand_browser.StagehandTransport', side_effect=transports):
            async with BrowserSessionPool(self.root) as pool:
                for filename in ('episode1.mp4', 'episode2.mp4'):
                    browser = self.task(pool, filename)
                    await browser.open()
                    await browser.close()
                    self.owner.kill.assert_not_awaited()
                self.assertIs(pool.page, self.page)
            self.assertEqual(launch.call_count, 1)
        self.owner.start.assert_awaited_once()
        self.owner.kill.assert_awaited_once()
        self.assertEqual(self.page.goto.await_count, 2)
        for transport, filename in zip(transports, ('episode1.mp4', 'episode2.mp4')):
            transport.close.assert_awaited_once()
            self.assertEqual(transport.call.await_args.kwargs['files'], {'video': str(self.root / filename)})

    async def test_worker_initialization_failure_stops_pool_and_closes_owner_once(self):
        transport = SimpleNamespace(start=AsyncMock(), close=AsyncMock(), terminate=AsyncMock(),
            call=AsyncMock(side_effect=RuntimeError('worker init failed')))
        with patch('browser_use.Browser', return_value=self.owner), \
             patch('kuaishou_upload.platforms.stagehand_browser.StagehandTransport', return_value=transport):
            async with BrowserSessionPool(self.root) as pool:
                browser = self.task(pool, 'episode1.mp4')
                with self.assertRaisesRegex(RuntimeError, 'worker init'):
                    await browser.open()
                self.assertTrue(pool.failed)
                self.owner.kill.assert_not_awaited()
                await browser.close()
        self.owner.kill.assert_awaited_once()

    async def test_different_account_cannot_reuse_session(self):
        async with BrowserSessionPool(self.root, expected_account='ACCOUNT-A') as pool:
            browser = self.task(pool, 'episode1.mp4')
            browser.expected_account = 'ACCOUNT-B'
            with self.assertRaisesRegex(RuntimeError, '身份不一致'):
                await browser.open()


class BatchQueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.videos = self.root / 'videos'
        for number in (3, 1, 2):
            make_materials(self.videos, name=f'episode{number}', episode=number, test_mode=True)
        self.settings = Settings(self.videos, self.root / 'state', self.videos / 'ledger.xlsx', stable_seconds=0)
        self.db = UploadDatabase(self.settings.root / 'uploads.sqlite3')
        self.addCleanup(self.db.close)

    def test_batch_is_default_and_grouped_in_episode_order(self):
        report = scan_directory(self.settings, self.db)
        self.assertEqual(self.settings.max_jobs, 0)
        self.assertEqual(len(report.summary()['series']), 1)
        self.assertEqual(report.summary()['series'][0]['episodes'], [1, 2, 3])
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(Settings.from_env().max_jobs, 0)

    def test_existing_first_episode_does_not_stop_remaining_episodes(self):
        import main
        report = scan_directory(self.settings, self.db)
        contexts = []
        def create(*args):
            browser = FakeBrowser(args[0])
            browser.open = browser.close = AsyncMock()
            context = main_context(*args, browser=browser)
            async def bootstrap():
                number = context.job.manifest.episodes[0].number
                self.db.update(context.sha256, status='exists' if number == 1 else 'test_prepared')
            context.bootstrap = AsyncMock(side_effect=bootstrap)
            contexts.append(context)
            return context
        from kuaishou_upload.tools import UploadTask as main_context
        with patch.dict(os.environ, {'OPENAI_API_KEY': 'local-test-only', 'OPENAI_BASE_URL': 'https://unused.invalid'}), \
             patch('kuaishou_upload.tools.UploadTask', side_effect=create):
            result = asyncio.run(main.process_queue(self.settings, self.db, report))
        self.assertEqual([r['status'] for r in result], ['exists', 'test_prepared', 'test_prepared'])
        self.assertEqual([c.job.manifest.episodes[0].number for c in contexts], [1, 2, 3])
        self.assertEqual(len({id(c.browser.session_pool) for c in contexts}), 1)
        for context in contexts:
            context.browser.submit.assert_not_awaited()

    def test_explicit_debug_cap_is_respected(self):
        import main
        report = scan_directory(self.settings, self.db)
        self.settings.max_jobs = 1
        from kuaishou_upload.tools import UploadTask
        def create(*args):
            browser = FakeBrowser(args[0])
            browser.open = browser.close = AsyncMock()
            context = UploadTask(*args, browser=browser)
            async def bootstrap():
                self.db.update(context.sha256, status='exists')
            context.bootstrap = AsyncMock(side_effect=bootstrap)
            return context
        with patch.dict(os.environ, {'OPENAI_API_KEY': 'local-test-only', 'OPENAI_BASE_URL': 'https://unused.invalid'}), \
             patch('kuaishou_upload.tools.UploadTask', side_effect=create):
            result = asyncio.run(main.process_queue(self.settings, self.db, report))
        self.assertEqual(len(result), 1)
        self.assertEqual(sum(r['status'] == 'pending' for r in self.db.records()), 2)

    def test_selected_directory_excludes_other_series_and_preserves_disk_database(self):
        import main
        selected = self.videos / 'selected'
        make_materials(selected, name='selected', test_mode=True)
        before = self.db.path.read_bytes()
        with patch('main.Settings.from_env', return_value=self.settings):
            self.assertEqual(main.main(['--directory', str(selected), '--max-jobs', '0', '--dry-run']), 0)
        report = json.loads((self.settings.runtime_root / 'last_scan.json').read_text(encoding='utf-8'))
        self.assertEqual(report['counts'], {'pending': 1})
        self.assertTrue(all(Path(item['video']).is_relative_to(selected) for item in report['files']))
        self.assertEqual(self.db.path.read_bytes(), before)
