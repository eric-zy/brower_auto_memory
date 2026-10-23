import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("ANONYMIZED_TELEMETRY", "false")
os.environ.setdefault("BROWSER_USE_CLOUD_SYNC", "false")

from browser_use.llm.exceptions import ModelProviderError
from browser_use.llm.messages import UserMessage
from pydantic import BaseModel

from kuaishou_upload.config import Settings
from kuaishou_upload.database import UploadDatabase
from kuaishou_upload.diagnostics import RunTrace
from kuaishou_upload.llm import ReliableModel
from kuaishou_upload.scan import scan_directory
from kuaishou_upload.tools import UploadTask
from support import make_materials
from test_upload import FakeBrowser


def malformed():
    try:
        json.loads('{"key":"test-secret" "action":1}')
    except json.JSONDecodeError as cause:
        error = ModelProviderError(str(cause), model="local-test")
        error.__cause__ = cause
        return error


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.trace = RunTrace(self.base / "logs", secrets=["test-secret"])
        self.directory = self.base / "videos"
        self.source = make_materials(self.directory)
        self.settings = Settings(self.directory, self.base / "state", self.directory / "发布记录.xlsx", stable_seconds=0)
        self.db = UploadDatabase(self.settings.root / "uploads.sqlite3")

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def inner(self, side_effect):
        return SimpleNamespace(model="local-test", name="local-test", provider="deepseek", ainvoke=AsyncMock(side_effect=side_effect))

    def test_bad_json_retries_generation_once_and_redacts_raw_arguments(self):
        expected = SimpleNamespace(completion={"action": 1})
        inner = self.inner([malformed(), expected])
        model = ReliableModel(inner, self.trace)
        result = asyncio.run(model.ainvoke([UserMessage(content="local only")]))
        self.assertIs(result, expected)
        self.assertEqual(inner.ainvoke.await_count, 2)
        self.assertFalse(model.failed)
        raw = next(self.trace.folder.glob("raw_arguments_*.txt")).read_text(encoding="utf-8")
        self.assertNotIn("test-secret", raw)
        self.assertIn("[redacted]", raw)

    def test_second_bad_json_fails_without_unbounded_retries(self):
        inner = self.inner([malformed(), malformed(), AssertionError("不应请求第三次")])
        model = ReliableModel(inner, self.trace)
        with self.assertRaises(ModelProviderError):
            asyncio.run(model.ainvoke([UserMessage(content="local only")]))
        self.assertEqual(inner.ainvoke.await_count, 2)
        self.assertTrue(model.failed)
        self.assertEqual(len(list(self.trace.folder.glob("raw_arguments_*.txt"))), 2)

    def test_request_timeout_is_bounded_and_does_not_retry_network_wait(self):
        async def slow(*args, **kwargs):
            await asyncio.sleep(5)
        inner = self.inner(slow)
        model = ReliableModel(inner, self.trace, timeout=0.01)
        with self.assertRaisesRegex(ModelProviderError, "超过"):
            asyncio.run(model.ainvoke([]))
        self.assertTrue(model.failed)
        self.assertEqual(inner.ainvoke.await_count, 1)

    def context(self):
        sha, job = next(iter(scan_directory(self.settings, self.db).jobs.items()))
        browser = FakeBrowser(job)
        events = []
        async def entry():
            events.append("entry")
        async def lookup():
            events.append("lookup")
            return None
        browser.open_upload_entry = AsyncMock(side_effect=entry)
        browser.lookup_series = AsyncMock(side_effect=lookup)
        context = UploadTask(job, sha, self.settings, self.db, {}, browser=browser)
        return context, browser, events

    def test_verified_absence_precedes_upload_entry_and_needs_no_model(self):
        context, browser, events = self.context()
        context.auto_prepare = AsyncMock()
        asyncio.run(context.bootstrap())
        self.assertEqual(events, ["lookup", "entry"])
        self.assertTrue(context.checked)
        self.assertTrue(context.bootstrapped)
        context.auto_prepare.assert_awaited_once()

    def test_verified_absence_opens_new_form_even_for_episode_two(self):
        data = json.loads(self.source.read_text(encoding="utf-8"))
        data["episodes"][0].update(number=2, title="第2集")
        self.source.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        context, browser, events = self.context()
        context.auto_prepare = AsyncMock()
        asyncio.run(context.bootstrap())
        self.assertEqual(events, ["lookup", "entry"])
        browser.open_upload_entry.assert_awaited_once()
        self.assertTrue(context.checked)

    def test_existing_episode_is_inspected_without_opening_upload_form(self):
        context, browser, events = self.context()
        async def lookup():
            events.append('lookup')
            return 'C1'
        async def detail(cid):
            events.append('detail')
        async def inspect(params):
            self.db.update(context.sha256, status='exists')
        browser.lookup_series = AsyncMock(side_effect=lookup)
        browser.open_detail = AsyncMock(side_effect=detail)
        context.inspect_platform = AsyncMock(side_effect=inspect)
        context.auto_prepare = AsyncMock()
        asyncio.run(context.bootstrap())
        self.assertEqual(events, ['lookup', 'detail'])
        browser.open_upload_entry.assert_not_awaited()
        context.auto_prepare.assert_not_awaited()

    def test_uncertain_lookup_never_opens_upload_form(self):
        context, browser, events = self.context()
        browser.lookup_series = AsyncMock(side_effect=TimeoutError('没有明确查重证据'))
        with self.assertRaises(TimeoutError):
            asyncio.run(context.bootstrap())
        browser.open_upload_entry.assert_not_awaited()
        self.assertFalse(context.checked)

    def test_provider_failure_stops_batch_and_preserves_remaining_queue(self):
        import main
        make_materials(self.directory, name="episode2", episode=2)
        make_materials(self.directory, name="episode3", episode=3)
        report = scan_directory(self.settings, self.db)
        self.settings.max_jobs = 0
        contexts = []
        def create(*args):
            browser = FakeBrowser(args[0])
            browser.browser = object()
            browser.open = browser.close = AsyncMock()
            context = UploadTask(*args, browser=browser)
            context.bootstrap = AsyncMock()
            contexts.append(context)
            return context
        inner = self.inner([malformed(), malformed()])
        class AgentDouble:
            def __init__(self, **kwargs):
                self.llm = kwargs["llm"]
            async def run(self, **kwargs):
                try:
                    await self.llm.ainvoke([UserMessage(content="local only")])
                except ModelProviderError as exc:
                    return SimpleNamespace(errors=lambda error=str(exc): [error])
        with patch.dict(os.environ, {"OPENAI_MODEL": "deepseek-flash-free", "OPENAI_API_KEY": "test-secret", "OPENAI_BASE_URL": "https://unused.invalid"}), \
             patch("browser_use.Agent", AgentDouble), patch("browser_use.ChatDeepSeek", return_value=inner), \
             patch("kuaishou_upload.tools.UploadTask", side_effect=create):
            result = asyncio.run(main.process_queue(self.settings, self.db, report))
        self.assertEqual(len(result), 1)
        self.assertEqual(len(contexts), 1)
        self.assertEqual(inner.ainvoke.await_count, 2)
        self.assertEqual(sum(record["status"] == "pending" for record in self.db.records()), 2)
        self.assertIn("Expecting", result[0]["message"])
        self.assertTrue((self.settings.root / "last_run.json").exists())

    def test_wrapped_model_is_accepted_by_real_agent_without_api_calls(self):
        with patch.dict(os.environ, {"ANONYMIZED_TELEMETRY": "false", "BROWSER_USE_CLOUD_SYNC": "false", "BROWSER_USE_CONFIG_DIR": str(self.base / "config")}):
            from browser_use import Agent, Browser, ChatDeepSeek
            model = ReliableModel(ChatDeepSeek(model="deepseek-flash-free", api_key="local-test"), self.trace)
            agent = Agent(task="本地构造测试，不运行", llm=model, browser=Browser(enable_default_extensions=False),
                          flash_mode=True, use_vision=False, enable_planning=False, use_judge=False)
            asyncio.run(agent.close())


if __name__ == "__main__":
    unittest.main()
