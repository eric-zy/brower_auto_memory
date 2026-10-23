import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from kuaishou_upload.config import Settings
from kuaishou_upload.database import UploadDatabase
from kuaishou_upload.folder_batch import FolderBatchTask, folder_groups
from kuaishou_upload.scan import scan_directory
from kuaishou_upload.platforms.kuaishou_browser import PlatformBrowser
from kuaishou_upload.platforms.midscene_browser import MidscenePlatformBrowser
from support import make_materials
from test_upload import FakeBrowser


class FolderBatchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.directory = self.root/'videos'
        for number in (3,1,2):
            make_materials(self.directory,name=f'episode{number}',episode=number,test_mode=True)
        self.settings = Settings(self.directory,self.root/'state',self.root/'ledger.xlsx',stable_seconds=0,browser_backend='midscene')
        self.db = UploadDatabase(self.settings.root/'uploads.sqlite3')
        self.addCleanup(self.db.close)
        self.report = scan_directory(self.settings,self.db)

    def task(self, members=None):
        members = members or self.report.ordered_jobs()
        browser = FakeBrowser(members[0][1])
        browser.backend = 'midscene'
        browser.lookup_series = AsyncMock(return_value=None)
        browser.open_upload_entry = AsyncMock()
        browser.rpc = SimpleNamespace(call=AsyncMock(return_value={'count':len(members)}))
        browser.page._target_id = 'TASK'
        return FolderBatchTask(members,self.settings,self.db,{},browser=browser), browser

    def test_grouping_keeps_folders_separate(self):
        make_materials(self.directory/'other',name='other',episode=4,test_mode=True)
        report=scan_directory(self.settings,self.db)
        groups=folder_groups(report.ordered_jobs(),self.db)
        self.assertEqual(sorted(len(g) for g in groups),[1,3])
        self.assertEqual([j.manifest.episodes[0].number for _,j in max(groups,key=len)],[1,2,3])

    def test_numeric_filenames_cannot_borrow_other_episode_upload_receipts(self):
        snap={'uploadRows':[{'text':'第11集 11.mp4 100%','previews':1}]}
        self.assertFalse(PlatformBrowser.video_complete(snap,'1.mp4'))
        self.assertTrue(PlatformBrowser.video_complete(snap,'11.mp4'))

    async def test_batch_receipts_require_actual_platform_numbers_outside_filenames(self):
        browser=MidscenePlatformBrowser(self.report.ordered_jobs()[0][1],self.settings.root)
        browser.batch_hashes=['test']
        browser.batch_specs=[{'sha256':'test','number':3,'filename':'film_第3集.mp4'}]
        snap={'uploadRows':[{'text':'film_第3集.mp4 100%','previews':1}]}
        with patch.object(PlatformBrowser,'verify_prepared',new=AsyncMock(return_value=snap)):
            with self.assertRaisesRegex(ValueError,'平台第3集'):await browser.verify_prepared({})
        snap['uploadRows'][0]['text']='第1集 film_第3集.mp4 100%'
        with patch.object(PlatformBrowser,'verify_prepared',new=AsyncMock(return_value=snap)):
            with self.assertRaisesRegex(ValueError,'平台第3集'):await browser.verify_prepared({})
        snap['uploadRows'][0]['text']='第3集 film_第3集.mp4 100%'
        with patch.object(PlatformBrowser,'verify_prepared',new=AsyncMock(return_value=snap)):
            self.assertEqual(await browser.verify_prepared({}),snap)

    async def test_absent_series_from_nonfirst_episode_enters_new_form_once(self):
        members=[p for p in self.report.ordered_jobs() if p[1].manifest.episodes[0].number>1]
        task,browser=self.task(members)
        task.auto_prepare=AsyncMock()
        await task.bootstrap()
        browser.lookup_series.assert_awaited_once()
        browser.open_upload_entry.assert_awaited_once()
        self.assertEqual([s['number'] for s in browser.batch_specs],[2,3])
        self.assertEqual(len(browser.batch_hashes),2)
        self.assertTrue(task.checked)

    async def test_ambiguous_lookup_does_not_open_new_form(self):
        task,browser=self.task()
        browser.lookup_series.side_effect=TimeoutError('无明确无结果证据')
        with self.assertRaises(TimeoutError): await task.bootstrap()
        browser.open_upload_entry.assert_not_awaited()

    async def test_mixed_shared_facts_stop_before_lookup(self):
        task,browser=self.task()
        task.members[1][1].manifest.synopsis='另一份公共资料'
        with self.assertRaisesRegex(ValueError,'公共资料'): await task.bootstrap()
        browser.lookup_series.assert_not_awaited()

    async def test_closed_series_does_not_request_pagination_or_guess_unseen_episode(self):
        members=self.report.ordered_jobs()
        task,browser=self.task(members)
        browser.lookup_series.return_value='C1'
        browser.snap.update(url='https://kdj.kuaishou.com/detail?miniSeriesId=C1',text=task.job.manifest.seriesTitle+' 是否完结是 上传集数/总集数51/51')
        browser.episode_inventory=AsyncMock(side_effect=AssertionError('不应翻页'))
        browser.find_episode=lambda snap: {'episodeId':'E1','reviewStatus':'审核通过'} if browser.job.manifest.episodes[0].number==1 else None
        with self.assertRaisesRegex(ValueError,'已完结') as caught: await task.bootstrap()
        await task.fail(caught.exception)
        self.assertEqual([self.db.get(s)['status'] for s,_ in members],['exists','blocked','blocked'])
        browser.episode_inventory.assert_not_awaited()

    async def test_one_bulk_selection_is_journaled_for_every_hash_before_rpc(self):
        task,browser=self.task()
        task.bootstrapped=task.checked=task.batch_prepared=True
        browser.batch_hashes=[s for s,_ in task.active]
        browser.expected_card_count=3
        browser.fields={}
        browser.snap['url']='https://kdj.kuaishou.com/edit?miniSeriesId=C1'
        browser.snap['text']='视频上传'
        await task.checkpoint(contentId='C1',draftUrl=browser.snap['url'],nextClicked=True,formVerified=True,cardCount=3,uploadedAssets=['cover','copyright'])
        async def call(method,**params):
            self.assertEqual(method,'batch_files')
            for sha,_ in task.active:
                r=self.db.get(sha)
                self.assertTrue(r['details']['batchSelectionAttempted'])
                self.assertEqual(r['details']['attachmentIntent'],'video')
            return {'selected':3}
        browser.rpc.call.side_effect=call
        await task.auto_prepare()
        await task.auto_prepare()
        browser.rpc.call.assert_awaited_once()
        self.assertTrue(task.awaiting_upload)
        self.assertTrue(all('video' in self.db.get(s)['details']['uploadedAssets'] for s,_ in task.active))

    async def test_interruption_preserves_unknown_for_all_and_prevents_reimport(self):
        task,browser=self.task()
        for sha,_ in task.members:
            self.db.update(sha,details={'batchSelectionAttempted':True,'attachmentIntent':'video'})
        await task.fail(RuntimeError('接收后断开'))
        self.assertTrue(all(self.db.get(s)['status']=='unknown' for s,_ in task.members))
        with self.assertRaisesRegex(ValueError,'禁止重新批量导入'): task.validate_members()

    async def test_partial_upload_does_not_mark_whole_batch_complete(self):
        task,browser=self.task()
        self.settings.upload_timeout=0.01
        browser.snap['uploadRows']=[{'text':task.members[0][1].files['video'].name+' 100%','previews':1}]
        for sha,_ in task.active: self.db.update(sha,details={'batchSelectionAttempted':True})
        with patch('kuaishou_upload.folder_batch.asyncio.sleep',new=AsyncMock()):
            with self.assertRaises(TimeoutError): await task.wait_for_upload()
        self.assertEqual(sum(self.db.get(s)['status']=='ready' for s,_ in task.active),1)
        self.assertEqual(sum(self.db.get(s)['status']=='test_prepared' for s,_ in task.active),0)
        browser.submit.assert_not_awaited()

    async def test_test_mode_records_every_file_and_never_submits(self):
        task,browser=self.task()
        browser.snap['uploadRows']=[{'text':j.files['video'].name+' 100%','previews':1} for _,j in task.active]
        await task.wait_for_upload()
        self.assertTrue(all(self.db.get(s)['status']=='test_prepared' for s,_ in task.active))
        self.assertEqual(len(task.results()),3)
        browser.verify_prepared.assert_awaited_once()
        browser.submit.assert_not_awaited()

    async def test_queue_uses_one_context_per_folder_and_cap_counts_videos(self):
        import main
        contexts=[]
        real=FolderBatchTask
        self.settings.max_jobs=2
        # Exercise with a real class subclass so isinstance remains meaningful.
        class FixtureBatch(real):
            def __init__(inner,members,*args):
                browser=FakeBrowser(members[0][1]);browser.open=browser.close=AsyncMock()
                super().__init__(members,*args,browser=browser);contexts.append(inner)
            async def bootstrap(inner):
                for sha,_ in inner.members:self.db.update(sha,status='test_prepared')
        with patch('kuaishou_upload.folder_batch.FolderBatchTask',FixtureBatch):
            result=await main.process_queue(self.settings,self.db,self.report)
        self.assertEqual(len(contexts),1)
        self.assertEqual(len(result),2)
        self.assertEqual(sum(r['status']=='pending' for r in self.db.records()),1)
