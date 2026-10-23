import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from kuaishou_upload.config import Settings
from kuaishou_upload.database import UploadDatabase
from kuaishou_upload.diagnostics import RunTrace
from kuaishou_upload.folder_batch import FolderBatchTask
from kuaishou_upload.page_state import PageBlocked, is_login_challenge, needs_user_action
from kuaishou_upload.scan import scan_directory
from kuaishou_upload.browser_process import start_owned_browser, close_owned_processes
from kuaishou_upload.browser_runtime import BrowserSessionPool
from support import make_materials
from test_upload import FakeBrowser


class ConflictTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.videos=self.root/'videos'
        for number in (3,4,5,6):make_materials(self.videos,name=f'episode{number}',episode=number,test_mode=True)
        self.settings=Settings(self.videos,self.root/'state',self.root/'ledger.xlsx',stable_seconds=0,browser_backend='midscene')
        self.db=UploadDatabase(self.settings.root/'uploads.sqlite3');self.addCleanup(self.db.close)
        self.report=scan_directory(self.settings,self.db)

    def task(self):
        members=self.report.ordered_jobs();browser=FakeBrowser(members[0][1])
        return FolderBatchTask(members,self.settings,self.db,{},browser=browser)

    async def test_exact_generated_debug_synopses_share_one_value_without_mutating_fingerprints(self):
        task=self.task()
        for _,job in task.members:
            job.manifest.synopsis=f'《{job.manifest.seriesTitle}》第{job.manifest.episodes[0].number}集。'
        original=[(job.manifest.synopsis,job.fingerprint) for _,job in task.members]
        task.validate_members();task.activate_member()
        self.assertEqual(task.browser.fields['作品简介'],f'《{task.job.manifest.seriesTitle}》。')
        self.assertEqual(original,[(job.manifest.synopsis,job.fingerprint) for _,job in task.members])

    def test_real_synopsis_difference_is_named_and_not_silently_replaced(self):
        task=self.task();task.members[1][1].manifest.synopsis='真实但不同的剧情简介'
        with self.assertRaisesRegex(ValueError,'作品简介.*第3集与第4集'):task.validate_members()

    def test_mixed_real_and_generated_synopses_still_conflict(self):
        task=self.task()
        job=task.members[1][1]
        job.manifest.synopsis=f'《{job.manifest.seriesTitle}》第{job.manifest.episodes[0].number}集。'
        with self.assertRaisesRegex(ValueError,'作品简介'):task.validate_members()

    def test_formal_mode_cannot_submit_debug_synopsis(self):
        task=self.task()
        for _,job in task.members:
            job.manifest.testMode=False
            job.manifest.synopsis=f'《{job.manifest.seriesTitle}》第{job.manifest.episodes[0].number}集。'
        with self.assertRaisesRegex(ValueError,'正式上传需要有来源'):task.validate_members()

    def test_factual_conflicts_and_test_mode_conflicts_name_exact_fields(self):
        task=self.task();task.members[1][1].manifest.filing.number='V11111111111111'
        with self.assertRaisesRegex(ValueError,'备案号'):task.validate_members()
        task=self.task();task.members[1][1].manifest.testMode=False
        with self.assertRaisesRegex(ValueError,'模拟模式'):task.validate_members()

    async def test_conflict_with_login_stage_does_not_require_human_login(self):
        task=self.task();task.stage='检查登录';task.trace=RunTrace(self.root/'trace')
        await task.fail(ValueError('同剧公共资料冲突：作品简介'))
        self.assertIn('检查登录',task.record['message'])
        self.assertFalse(task.requires_user)
        self.assertEqual(task.record['status'],'blocked')

    def test_only_actual_login_challenges_or_structured_user_actions_stop_queue(self):
        self.assertFalse(needs_user_action(ValueError('作品简介为登录失败，出现验证码')))
        self.assertFalse(is_login_challenge(PageBlocked('midscene_model_error','402 model failure')))
        self.assertTrue(needs_user_action(PageBlocked('login_required','等待扫码登录')))
        self.assertTrue(needs_user_action(PageBlocked('challenge','需要验证码')))
        self.assertTrue(needs_user_action(PageBlocked('agreement','确认法律协议')))
        self.assertTrue(is_login_challenge(RuntimeError('等待扫码登录')))

    async def test_conflict_preflight_never_opens_browser_and_next_folder_runs(self):
        import main
        make_materials(self.videos/'other',name='other',episode=7,test_mode=True)
        report=scan_directory(self.settings,self.db)
        contexts=[];real=FolderBatchTask
        class FixtureBatch(real):
            def __init__(inner,members,*args):
                browser=FakeBrowser(members[0][1]);browser.open=AsyncMock();browser.close=AsyncMock()
                super().__init__(members,*args,browser=browser);contexts.append(inner)
                if len(members)>1:members[1][1].manifest.synopsis='不同剧情'
            async def bootstrap(inner):
                for sha,_ in inner.members:self.db.update(sha,status='test_prepared')
        with patch('kuaishou_upload.folder_batch.FolderBatchTask',FixtureBatch):
            results=await main.process_queue(self.settings,self.db,report)
        conflict=next(c for c in contexts if len(c.members)>1)
        conflict.browser.open.assert_not_awaited()
        self.assertFalse(conflict.requires_user)
        self.assertEqual(len(results),5)
        self.assertEqual(sum(r['status']=='test_prepared' for r in results),1)

    async def test_historical_model_error_does_not_stop_a_successful_current_queue(self):
        import main
        make_materials(self.videos/'other',name='other',episode=7,test_mode=True)
        report=scan_directory(self.settings,self.db)
        for sha in report.jobs:self.db.update(sha,details={'errorCode':'midscene_model_error'})
        contexts=[];real=FolderBatchTask
        class FixtureBatch(real):
            def __init__(inner,members,*args):
                browser=FakeBrowser(members[0][1]);browser.open=AsyncMock();browser.close=AsyncMock()
                super().__init__(members,*args,browser=browser);contexts.append(inner)
            async def bootstrap(inner):
                for sha,_ in inner.members:self.db.update(sha,status='test_prepared')
        with patch('kuaishou_upload.folder_batch.FolderBatchTask',FixtureBatch):
            results=await main.process_queue(self.settings,self.db,report)
        self.assertEqual(len(contexts),2)
        self.assertEqual(len(results),5)
        self.assertTrue(all(r['status']=='test_prepared' for r in results))


class ProcessOwnershipTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_owned_chrome_cdp_launch_loses_unused_pipes(self):
        chrome=SimpleNamespace(wait=AsyncMock(return_value=0))
        node=SimpleNamespace()
        spawn=AsyncMock(side_effect=[chrome,node])
        class Browser:
            async def start(self):
                await asyncio.create_subprocess_exec('chrome.exe','--remote-debugging-port=12345',stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
                await asyncio.create_subprocess_exec('node.exe','worker.mjs',stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
        processes=[]
        with patch('asyncio.create_subprocess_exec',spawn):
            await start_owned_browser(Browser(),processes)
            self.assertIs(asyncio.create_subprocess_exec,spawn)
        self.assertEqual(processes,[chrome])
        self.assertEqual(spawn.await_args_list[0].kwargs['stdout'],asyncio.subprocess.DEVNULL)
        self.assertEqual(spawn.await_args_list[1].kwargs['stdout'],asyncio.subprocess.PIPE)
        await close_owned_processes(processes)
        self.assertEqual(processes,[]);chrome.wait.assert_awaited_once()

    async def test_failed_start_restores_spawn_and_keeps_process_for_cleanup(self):
        process=SimpleNamespace(wait=AsyncMock(return_value=0))
        spawn=AsyncMock(return_value=process)
        class Browser:
            async def start(self):
                await asyncio.create_subprocess_exec('chrome.exe','--remote-debugging-port=1')
                raise RuntimeError('CDP unavailable')
        processes=[]
        with patch('asyncio.create_subprocess_exec',spawn):
            with self.assertRaises(RuntimeError):await start_owned_browser(Browser(),processes)
            self.assertIs(asyncio.create_subprocess_exec,spawn)
        self.assertEqual(processes,[process]);await close_owned_processes(processes)

    async def test_pool_cleanup_reaps_owned_process_even_if_browser_kill_raises(self):
        process=SimpleNamespace(wait=AsyncMock(return_value=0))
        pool=BrowserSessionPool(Path(tempfile.gettempdir())/'owned-test')
        pool.browser=SimpleNamespace(kill=AsyncMock(side_effect=RuntimeError('stop error')))
        pool.owned_processes=[process]
        with self.assertRaises(RuntimeError):await pool.close()
        process.wait.assert_awaited_once();self.assertEqual(pool.owned_processes,[])
