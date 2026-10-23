"""Same fail-closed local protocol; Midscene is an independent optional Node worker."""
from .stagehand_transport import StagehandTransport
import os
import re
import asyncio
import time


class MidsceneTransport(StagehandTransport):
    engine_name = 'Midscene'
    worker_folder = 'midscene'
    dependency_package = '@midscene/web'

    def __init__(self, root, *, timeout=None):
        wait = os.getenv('MIDSCENE_WAIT_FOR_MODEL', 'true').lower()
        if wait not in {'true','false','1','0'}:
            raise ValueError('MIDSCENE_WAIT_FOR_MODEL必须为true或false')
        self.wait_for_model = wait in {'true','1'} and timeout is None
        self.model_wait_notice_seconds = 30
        if wait in {'true','1'}:
            super().__init__(root, timeout=timeout if timeout is not None else 120)
            return
        request_ms = self._milliseconds('MIDSCENE_MODEL_TIMEOUT', 90000)
        navigation_ms = self._milliseconds('MIDSCENE_NAVIGATION_TIMEOUT', max(180000, request_ms + 30000))
        if navigation_ms <= request_ms:
            raise ValueError('MIDSCENE_NAVIGATION_TIMEOUT必须大于MIDSCENE_MODEL_TIMEOUT')
        # Reserve the bounded DOM discovery (at most 20 seconds), retry delay,
        # snapshot/report serialization and IPC after the navigation deadline.
        super().__init__(root, timeout=timeout if timeout is not None else (navigation_ms / 1000 + 45))

    async def read_response(self, method):
        if not self.wait_for_model or method not in {'ensure_upload_form','resolve_intent','field_binding','read_inventory','prepare_batch'}:
            return await super().read_response(method)
        reader = asyncio.create_task(self.process.stdout.readline())
        started = time.monotonic()
        try:
            while True:
                done, _ = await asyncio.wait({reader}, timeout=self.model_wait_notice_seconds)
                if done:
                    return await reader
                print(f'[模型] 仍在等待执行引擎返回，已等待{int(time.monotonic()-started)}秒；Ctrl+C可停止。', flush=True)
        finally:
            if not reader.done():
                reader.cancel()
                await asyncio.gather(reader, return_exceptions=True)

    @staticmethod
    def _milliseconds(name, fallback):
        try:
            raw = os.getenv(name)
            if raw is not None and not re.fullmatch(r'[0-9]+', raw.strip()):
                raise ValueError(name)
            value = int(raw) if raw is not None else fallback
        except ValueError:
            raise ValueError(f'{name}必须是1000至600000的整数毫秒数') from None
        if not 1000 <= value <= 600000:
            raise ValueError(f'{name}必须是1000至600000的整数毫秒数')
        return value
