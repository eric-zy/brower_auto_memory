import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from kuaishou_upload.browser import PlatformBrowser, safe_control


def snapshot(text='', controls=None):
    return {'url': 'https://kdj.kuaishou.com/home/content/content-management',
            'text': text, 'controls': controls or [], 'dialogs': []}


def node(text, tag='button', role='button', ref='k1'):
    return {'ref': ref, 'text': text, 'tag': tag, 'type': role, 'disabled': False}


class UploadEntryTests(unittest.IsolatedAsyncioTestCase):
    def adapter(self, snapshots):
        adapter = PlatformBrowser(None, Path('.'))
        adapter.dismiss_onboarding = AsyncMock()
        adapter.snapshot = AsyncMock(side_effect=snapshots)
        adapter.act = AsyncMock()
        return adapter

    async def test_shell_navigates_once_then_uses_fresh_role_button(self):
        menu = snapshot('麻雀短剧 付费短剧', [node('付费短剧', 'li', 'menuitem', 'k4')])
        entry = snapshot('内容上传', [node('内容上传', 'div', 'button', 'k8')])
        form = snapshot('版权', [node('', 'input', 'text')])
        adapter = self.adapter([menu, menu, entry, form])
        with patch('kuaishou_upload.browser.asyncio.sleep', new_callable=AsyncMock):
            self.assertEqual(await adapter.open_upload_entry(), form)
        self.assertEqual(adapter.act.await_count, 2)
        first, second = adapter.act.await_args_list
        self.assertEqual(first.args[0].ref, 'k4')
        self.assertEqual(second.args[0].ref, 'k8')
        self.assertIs(second.args[2], entry)

    async def test_existing_form_does_not_click(self):
        adapter = self.adapter([snapshot('备案', [node('', 'input', 'text')])])
        await adapter.open_upload_entry()
        adapter.act.assert_not_awaited()

    async def test_duplicate_buttons_stop_without_clicking(self):
        adapter = self.adapter([snapshot('内容上传', [node('内容上传'), node('内容上传', ref='k2')])])
        with self.assertRaisesRegex(ValueError, '2 个'):
            await adapter.open_upload_entry()
        adapter.act.assert_not_awaited()

    async def test_empty_shell_reports_timeout_and_navigates_only_once(self):
        shell = snapshot('付费短剧', [node('付费短剧', 'li', 'menuitem')])
        adapter = self.adapter([shell] * 60)
        with patch('kuaishou_upload.browser.asyncio.sleep', new_callable=AsyncMock):
            with self.assertRaisesRegex(TimeoutError, '内容区可能未加载'):
                await adapter.open_upload_entry()
        self.assertEqual(adapter.act.await_count, 1)

    def test_entry_allowlist_preserves_submission_and_agreement_blocks(self):
        self.assertTrue(safe_control(node('付费短剧', 'li', 'menuitem'), 'entry', {}, []))
        for text in ('提交', '发布', '删除', '同意用户协议', '未知按钮'):
            self.assertFalse(safe_control(node(text), 'entry', {}, []))
