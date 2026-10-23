import asyncio
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from kuaishou_upload.midscene_transport import MidsceneTransport


class ModelWaitTests(unittest.IsolatedAsyncioTestCase):
    def transport(self):
        with patch.dict(os.environ, {}, clear=True):
            transport=MidsceneTransport(Path(tempfile.gettempdir()))
        transport.timeout=0.005
        transport.model_wait_notice_seconds=0.01
        transport.process=SimpleNamespace(stdout=asyncio.StreamReader(),stdin=SimpleNamespace(write=Mock(),drain=AsyncMock()))
        transport.terminate=AsyncMock()
        return transport

    async def test_slow_model_response_survives_progress_polls_and_is_not_resent(self):
        transport=self.transport()
        async def feed():
            await asyncio.sleep(0.045)
            transport.process.stdout.feed_data(json.dumps({'id':1,'result':{'ok':True}}).encode()+b'\n')
        feeding=asyncio.create_task(feed())
        output=io.StringIO()
        with redirect_stdout(output):result=await transport.call('field_binding')
        await feeding
        self.assertEqual(result,{'ok':True})
        self.assertIn('仍在等待',output.getvalue())
        transport.process.stdin.write.assert_called_once()
        self.assertFalse(transport.failed)
        transport.terminate.assert_not_awaited()

    async def test_cancellation_stops_wait_and_preserves_unknown_outcome_rule(self):
        transport=self.transport()
        waiting=asyncio.create_task(transport.call('ensure_upload_form'))
        await asyncio.sleep(0.002)
        waiting.cancel()
        with self.assertRaises(asyncio.CancelledError):await waiting
        self.assertTrue(transport.failed)
        transport.terminate.assert_awaited_once()
        # The pending reader must also be cancelled, rather than consuming a
        # later response or leaving another coroutine stuck in readline().
        transport.process.stdout.feed_data(b'late response\n')
        self.assertEqual(await transport.process.stdout.readline(),b'late response\n')
        with self.assertRaisesRegex(RuntimeError,'禁止自动重试'):await transport.call('ensure_upload_form')

    async def test_non_model_calls_keep_their_finite_protocol_timeout(self):
        transport=self.transport()
        with self.assertRaises(TimeoutError):await transport.call('snapshot')
        self.assertTrue(transport.failed)
        transport.terminate.assert_awaited_once()
