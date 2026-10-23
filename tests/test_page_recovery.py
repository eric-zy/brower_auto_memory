import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from kuaishou_upload.browser import PlatformBrowser, Decision
from kuaishou_upload.locator import signature
from kuaishou_upload.page_state import classify_page, PageBlocked
from kuaishou_upload.recovery import ModelLocatorRecovery, LocatorChoice


def node(text='', ref='k1', **kwargs):
    return {'ref': ref, 'tag': 'button', 'type': 'button', 'text': text,
            'accessibleName': text, 'label': text, 'disabled': False, 'href': '', **kwargs}


def snap(*controls, **kwargs):
    return {'url': 'https://kdj.kuaishou.com/home/content/content-management',
            'text': '内容管理', 'controls': list(controls), 'rows': [], 'dialogs': [], **kwargs}


class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.browser = PlatformBrowser(None, Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    async def test_recorded_shell_is_not_misclassified_as_ready(self):
        state = json.loads((Path(__file__).parent / 'fixtures/platform-shell.json').read_text(encoding='utf-8'))
        self.assertEqual(classify_page(state).name, 'shell')
        state['loading'] = True
        self.assertEqual(classify_page(state).name, 'loading')

    async def test_permission_login_challenge_and_agreement_never_recover(self):
        cases = [(snap(text='暂无权限'), 'permission_denied'),
                 (snap(text='扫码登录'), 'login_required'),
                 (snap(text='请完成安全验证'), 'challenge'),
                 (snap(dialogs=['请阅读服务协议']), 'agreement')]
        self.browser.recover = AsyncMock()
        for state, code in cases:
            with self.subTest(code=code), self.assertRaises(PageBlocked) as raised:
                await self.browser.observe_state(state)
            self.assertEqual(raised.exception.code, code)
        self.browser.recover.assert_not_awaited()

    async def test_account_must_match_account_area_exactly(self):
        self.browser.expected_account = '麻雀短剧'
        state = snap(text='麻雀短剧', accountText='另一个账号')
        with self.assertRaises(PageBlocked):
            await self.browser.observe_state(state)
        with self.assertRaises(PageBlocked):
            await self.browser.observe_state(snap(accountText='麻雀短剧2'))
        await self.browser.observe_state(snap(accountText='某公司\n麻雀短剧'))

    async def test_model_selects_current_ref_and_cache_waits_for_postcondition(self):
        state = snap(node('', accessibleName='加号'), pageState='content_list')
        self.browser.recover = AsyncMock(return_value={'ref': 'k1'})
        found = await self.browser.resolve_intent(state, 'open_upload')
        self.assertEqual(found['ref'], 'k1')
        self.assertFalse(self.browser.locator.memory.entries)
        self.browser.act = AsyncMock()
        await self.browser.click_intent(found, 'open_upload', 'entry', state)
        self.assertFalse(self.browser.locator.memory.entries)
        await self.browser.confirm_intents(snap(pageState='metadata_form'))
        cached = json.loads(self.browser.locator.memory.path.read_text(encoding='utf-8'))
        self.assertNotIn('ref', next(iter(cached.values())))
        state['controls'][0]['ref'] = 'k99'
        self.assertEqual((await self.browser.resolve_intent(state, 'open_upload'))['ref'], 'k99')
        self.assertEqual(self.browser.recover.await_count, 1)

    async def test_stale_cache_does_not_click_new_target(self):
        old = snap(node('上传作品'))
        self.browser.locator.memory.remember('open_upload', old, old['controls'][0])
        current = snap(node('删除作品'))
        self.assertIsNone(await self.browser.resolve_intent(current, 'open_upload'))

    async def test_hallucinated_and_hazardous_refs_are_rejected(self):
        state = snap(node('加号'), node('提交审核', ref='k2'), pageState='content_list')
        for ref in ('k999', 'k2'):
            self.browser.recovery_attempts.clear()
            self.browser.recovery_used = 0
            self.browser.recover = AsyncMock(return_value={'ref': ref})
            with self.assertRaisesRegex(ValueError, '当前允许候选'):
                await self.browser.resolve_intent(state, 'open_upload')

    async def test_recovery_budget_and_loading_guard(self):
        self.browser.recovery_steps = 1
        self.browser.recover = AsyncMock(return_value={'ref': None})
        state = snap(node('加号'), pageState='loading')
        await self.browser.resolve_intent(state, 'open_upload')
        self.browser.recover.assert_not_awaited()
        state['pageState'] = 'content_list'
        await self.browser.resolve_intent(state, 'open_upload')
        await self.browser.resolve_intent(state, 'open_upload')
        await self.browser.resolve_intent(state, 'search')
        self.assertEqual(self.browser.recover.await_count, 1)

    async def test_wrong_domain_and_changed_grant_fail_closed(self):
        with self.assertRaises(PageBlocked):
            await self.browser.observe_state(snap(url='https://evil.example/'))
        state = snap(node('内容上传'))
        found = await self.browser.resolve_intent(state, 'open_upload')
        found['text'] = '删除'
        self.assertFalse(self.browser.consume_intent_grant(Decision(action='click', ref='k1', intent='open_upload'), found, state))

    async def test_click_target_covered_by_modal_is_not_executed(self):
        target = node('内容上传')
        state = snap(target)
        self.browser.page = SimpleNamespace(get_url=AsyncMock(return_value=state['url']),
            evaluate=AsyncMock(return_value=json.dumps({**target, 'visible': True, 'receivesEvents': False})))
        self.browser.element = AsyncMock()
        with self.assertRaisesRegex(RuntimeError, '遮挡'):
            await self.browser.act(Decision(action='click', ref='k1'), 'entry', state)
        self.browser.element.assert_not_awaited()

    async def test_expected_state_is_required_after_click(self):
        self.browser.snapshot = AsyncMock(return_value=snap(pageState='shell'))
        with patch('kuaishou_upload.browser_runtime.asyncio.sleep', new_callable=AsyncMock):
            with self.assertRaises(PageBlocked) as raised:
                await self.browser.wait_for_states({'metadata_form'}, polls=2)
        self.assertEqual(raised.exception.code, 'transition_timeout')

    async def test_structured_recovery_requests_no_values_or_arbitrary_actions(self):
        model = SimpleNamespace(ainvoke=AsyncMock(return_value=SimpleNamespace(completion=LocatorChoice(ref='k1'))))
        recovery = ModelLocatorRecovery(lambda: model)
        result = await recovery({'intent': 'open_upload', 'candidates': [{'ref': 'k1'}], 'screenshot': None})
        self.assertEqual(result['ref'], 'k1')
        self.assertIs(model.ainvoke.await_args.kwargs['output_format'], LocatorChoice)

    async def test_vision_is_added_only_when_requested(self):
        model = SimpleNamespace(ainvoke=AsyncMock(return_value=SimpleNamespace(completion=LocatorChoice())))
        await ModelLocatorRecovery(lambda: model)({'intent': 'open_upload', 'candidates': [], 'screenshot': b'fake-png'})
        content = model.ainvoke.await_args.args[0][0].content
        self.assertEqual(content[1].type, 'image_url')

    async def test_duplicate_candidates_use_model_to_disambiguate(self):
        state = snap(node('内容上传'), node('内容上传', ref='k2'), pageState='content_list')
        self.browser.recover = AsyncMock(return_value={'ref': 'k2'})
        self.assertIsNone(await self.browser.resolve_intent(state, 'open_upload', recover=False))
        self.assertEqual((await self.browser.resolve_intent(state, 'open_upload'))['ref'], 'k2')

    async def test_stale_accessible_name_never_clicks(self):
        target = node('内容上传')
        state = snap(target)
        self.browser.page = SimpleNamespace(get_url=AsyncMock(return_value=state['url']),
            evaluate=AsyncMock(return_value=json.dumps({**target, 'accessibleName': '删除'})))
        self.browser.element = AsyncMock()
        with self.assertRaisesRegex(RuntimeError, '可访问名称'):
            await self.browser.act(Decision(action='click', ref='k1'), 'entry', state)
        self.browser.element.assert_not_awaited()
