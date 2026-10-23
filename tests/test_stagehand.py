import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from kuaishou_upload.config import Settings
from kuaishou_upload.platforms import get_adapter
from kuaishou_upload.stagehand_transport import StagehandTransport, StagehandPage, StagehandElement
from kuaishou_upload.platforms.stagehand_browser import StagehandPlatformBrowser


class StagehandTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    async def test_backend_defaults_to_stagehand_and_rollback_is_explicit(self):
        settings=Settings(self.root,self.root,self.root/'ledger.xlsx')
        self.assertIsInstance(get_adapter('kuaishou').create_browser(None,settings),StagehandPlatformBrowser)
        settings.browser_backend='browser_use'
        self.assertNotIsInstance(get_adapter('kuaishou').create_browser(None,settings),StagehandPlatformBrowser)

    async def test_element_only_sends_asset_role_and_one_final_capability(self):
        rpc=SimpleNamespace(call=AsyncMock(return_value=True))
        page=StagehandPage(rpc,'TAB',{'video':self.root/'video.mp4'})
        element=StagehandElement(page,'k123')
        with self.assertRaises(ValueError):
            await element.set_input_files(['C:/unrelated.mp4'])
        rpc.call.assert_not_awaited()
        await element.set_input_files([str(self.root/'video.mp4')])
        self.assertEqual(rpc.call.await_args.kwargs['asset'],'video')
        self.assertNotIn('path',rpc.call.await_args.kwargs)
        page.final_submit_allowed=True
        await element.click()
        self.assertTrue(rpc.call.await_args.kwargs['final'])
        await element.click()
        self.assertFalse(rpc.call.await_args.kwargs['final'])

    async def test_rpc_timeout_terminates_owned_worker_and_never_retries(self):
        transport=StagehandTransport(self.root,timeout=.01)
        async def hang(): await asyncio.sleep(2)
        proc=SimpleNamespace(returncode=None,stdin=SimpleNamespace(write=Mock(),drain=AsyncMock()),
            stdout=SimpleNamespace(readline=AsyncMock(side_effect=hang)),kill=Mock(),wait=AsyncMock())
        transport.process=proc
        with self.assertRaises(TimeoutError):await transport.call('click',ref='k1')
        proc.kill.assert_called_once()
        self.assertTrue(transport.failed)
        with self.assertRaisesRegex(RuntimeError,'禁止自动重试'):await transport.call('click',ref='k1')
        self.assertEqual(proc.stdin.write.call_count,1)

    async def test_rpc_identity_mismatch_fails_closed(self):
        transport=StagehandTransport(self.root)
        transport.process=SimpleNamespace(returncode=None,stdin=SimpleNamespace(write=Mock(),drain=AsyncMock()),
            stdout=SimpleNamespace(readline=AsyncMock(return_value=b'{"id":99,"result":true}\n')),kill=Mock(),wait=AsyncMock())
        with self.assertRaisesRegex(RuntimeError,'身份不匹配'):await transport.call('snapshot')
        self.assertTrue(transport.failed)

    async def test_stagehand_requires_verified_form_not_agent_claim(self):
        browser=StagehandPlatformBrowser(None,self.root)
        browser.page=SimpleNamespace(_target_id='T')
        browser.rpc=SimpleNamespace(call=AsyncMock(return_value={'verified':False}))
        with self.assertRaisesRegex(RuntimeError,'表单证据'):await browser.open_upload_entry()

    async def test_source_snapshot_uses_frame_aware_worker(self):
        from kuaishou_upload.observation import SNAPSHOT
        rpc=SimpleNamespace(call=AsyncMock(return_value={'controls':[],'frames':[{'frameId':'CHILD'}]}))
        page=StagehandPage(rpc,'T',{})
        result=json.loads(await page.evaluate(SNAPSHOT))
        self.assertEqual(result['frames'][0]['frameId'],'CHILD')
        self.assertEqual(rpc.call.await_args.args[0],'snapshot')

    async def test_stagehand_locator_does_not_call_old_python_recovery(self):
        browser=StagehandPlatformBrowser(None,self.root)
        browser.page=SimpleNamespace(_target_id='T')
        browser.recover=AsyncMock(side_effect=AssertionError('旧的单次ref恢复不得执行'))
        browser.rpc=SimpleNamespace(call=AsyncMock(return_value={'ref':'k42'}))
        node={'ref':'k42','tag':'button','type':'button','text':'继续','accessibleName':'继续','label':'单集信息 制作协议','disabled':False}
        state={'url':'https://kdj.kuaishou.com/upload','controls':[node]}
        browser.snapshot=AsyncMock(return_value=state)
        selected=await browser.resolve_intent({'url':state['url'],'controls':[]},'next')
        self.assertEqual(selected['ref'],'k42')
        browser.recover.assert_not_awaited()

    async def test_account_and_protocol_helpers_keep_the_same_target(self):
        from kuaishou_upload.stagehand_transport import StagehandSession
        rpc=SimpleNamespace(call=AsyncMock(return_value=True))
        await StagehandSession(rpc,{}).get_or_create_cdp_session('ORIGINAL',focus=True)
        rpc.call.assert_awaited_once_with('focus',pageId='ORIGINAL')
