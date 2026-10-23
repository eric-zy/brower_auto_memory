import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from kuaishou_upload.platforms.kuaishou_lookup import lookup_evidence
from kuaishou_upload.platforms.kuaishou_browser import PlatformBrowser


TITLE = '示例短剧'


def page(titles=(TITLE,), *, loading=False, text='搜索结果'):
    rows = [{'text': f'预览\n{title}\n短剧ID：{index + 100}',
             'cells': [f'预览\n{title}\n短剧ID：{index + 100}', '已上架'], 'links': []}
            for index, title in enumerate(titles)]
    return {'url': 'https://kdj.kuaishou.com/home/content/content-management',
            'text': text, 'rows': rows, 'controls': [{'ref': 'k1', 'tag': 'input', 'value': TITLE}],
            'dialogs': [], 'loading': loading}


def read(snap):
    return lookup_evidence(snap, TITLE, search_ref='k1')


class LookupEvidenceTests(unittest.TestCase):
    def test_preview_label_does_not_hide_exact_match_among_similar_titles(self):
        self.assertEqual(read(page((TITLE + '3', TITLE + '2', TITLE + '1', TITLE)))['ids'], ['103'])

    def test_similar_title_is_never_an_exact_match_or_empty(self):
        result = read(page((TITLE + '1',)))
        self.assertEqual(result['ids'], [])
        self.assertFalse(result['empty'])

    def test_preview_word_inside_title_is_preserved(self):
        self.assertEqual(read(page(('预览' + TITLE,)))['ids'], [])

    def test_matching_status_cell_is_not_a_matching_title(self):
        snap = page(('其他短剧',))
        snap['rows'][0]['cells'].append(TITLE)
        self.assertEqual(read(snap)['ids'], [])

    def test_conflicting_row_ids_stop(self):
        snap = page()
        snap['rows'][0]['links'] = ['https://kdj.kuaishou.com/detail?miniSeriesId=999']
        with self.assertRaisesRegex(ValueError, '冲突'):
            read(snap)

    def test_foreign_link_never_supplies_an_identity(self):
        snap = page()
        snap['rows'][0]['links'] = ['https://example.com/?miniSeriesId=999']
        self.assertEqual(read(snap)['ids'], ['100'])

    def test_loading_and_truncated_results_are_not_decisive(self):
        for key in ('loading', 'truncated'):
            with self.subTest(key=key):
                snap = page()
                snap[key] = True
                self.assertEqual(read(snap)['ids'], [])
                snap = page((), text='暂无数据')
                snap[key] = True
                self.assertFalse(read(snap)['empty'])

    def test_empty_requires_query_proof_and_no_identified_rows(self):
        snap = page((), text='暂无数据')
        self.assertTrue(read(snap)['empty'])
        snap['controls'] = []
        self.assertFalse(read(snap)['empty'])
        snap['url'] += '?miniSeriesTitle=' + TITLE
        self.assertTrue(read(snap)['empty'])
        self.assertFalse(read(page(text='暂无数据'))['empty'])


class LookupWorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.browser = PlatformBrowser(SimpleNamespace(fields=lambda: {'剧名': TITLE}), Path(self.temp.name))
        self.main_page = SimpleNamespace(_target_id='MAIN')
        self.browser.page = self.main_page
        self.browser.browser = SimpleNamespace(new_page=AsyncMock(side_effect=AssertionError('查重不能新建标签页')),
            close_page=AsyncMock(), get_or_create_cdp_session=AsyncMock())
        self.browser.wait_page = self.browser.dismiss_onboarding = AsyncMock()
        self.browser.write_field = AsyncMock()
        self.browser.click_intent = AsyncMock()
        self.browser.confirm_intents = AsyncMock()
        self.browser.resolve_intent = AsyncMock(side_effect=lambda snap, intent, **kw: {'ref': 'k1' if intent == 'search_field' else 'k2'})

    async def lookup(self, snapshots):
        self.browser.snapshot = AsyncMock(side_effect=snapshots)
        with patch('kuaishou_upload.platforms.kuaishou_browser.asyncio.sleep', new_callable=AsyncMock):
            return await self.browser.lookup_series()

    async def test_waits_for_stable_result_and_keeps_current_list_tab(self):
        result = await self.lookup([page(), page(), page(loading=True), page(), page()])
        self.assertEqual(result, '100')
        self.assertIs(self.browser.page, self.main_page)
        self.browser.browser.new_page.assert_not_awaited()
        self.browser.browser.close_page.assert_not_awaited()
        self.browser.browser.get_or_create_cdp_session.assert_not_awaited()

    async def test_duplicate_exact_titles_stop_instead_of_choosing_first(self):
        with self.assertRaisesRegex(ValueError, '多个同名'):
            await self.lookup([page(), page(), page((TITLE, TITLE)), page((TITLE, TITLE))])
        self.assertIs(self.browser.page, self.main_page)
        self.assertIsNotNone(self.browser.failure_snapshot)

    async def test_timeout_preserves_failing_list_snapshot(self):
        snap = page((TITLE + '1',))
        with self.assertRaisesRegex(TimeoutError, '作品行数=1'):
            await self.lookup([page(), page()] + [snap] * 30)
        self.assertIs(self.browser.failure_snapshot, snap)
        self.assertIs(self.browser.page, self.main_page)

    async def test_verified_empty_returns_none(self):
        empty = page((), text='暂无数据')
        self.assertIsNone(await self.lookup([page(), page(), empty, empty]))

    async def test_upload_form_is_never_used_as_a_search_page(self):
        snap = page()
        snap['url'] += '/edit?step=0'
        with self.assertRaisesRegex(ValueError, '当前内容管理列表'):
            await self.lookup([snap])
        self.browser.write_field.assert_not_awaited()

    async def test_search_field_binding_does_not_leak_into_metadata(self):
        async def write(node, key):
            self.browser.filled[key] = {'ref': 'SEARCH', 'value': TITLE}
        self.browser.write_field.side_effect = write
        await self.lookup([page()] * 4)
        self.assertEqual(self.browser.filled, {})


if __name__ == '__main__':
    unittest.main()
