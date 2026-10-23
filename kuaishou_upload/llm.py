from __future__ import annotations

import asyncio
import time

from .diagnostics import is_json_error


class ReliableModel:
    """只重试模型生成，不执行页面动作；耗尽后交给队列停止机制。"""

    def __init__(self, inner, trace, timeout=30):
        self.inner, self.trace, self.timeout = inner, trace, timeout
        self.model = inner.model
        self._verified_api_keys = False
        self.failed = False
        self.last_error = None

    @property
    def provider(self):
        return self.inner.provider

    @property
    def name(self):
        return self.inner.name

    @property
    def model_name(self):
        return self.model

    async def ainvoke(self, messages, output_format=None, **kwargs):
        from browser_use.llm.exceptions import ModelProviderError
        from browser_use.llm.messages import UserMessage

        request = list(messages)
        for attempt in (1, 2):
            started = time.monotonic()
            self.trace.event("llm_request", attempt=attempt, model=self.model)
            print(f"[模型] 正在生成动作，第 {attempt}/2 次，单次上限 {self.timeout:g} 秒", flush=True)
            try:
                result = await asyncio.wait_for(self.inner.ainvoke(request, output_format=output_format, **kwargs), self.timeout)
                self.trace.event("llm_result", attempt=attempt, duration=round(time.monotonic() - started, 3))
                return result
            except asyncio.CancelledError:
                self.failed = True
                self.last_error = "模型调用被步骤超时或用户中断取消"
                raise
            except Exception as exc:
                self.trace.error(exc, attempt=attempt, duration=round(time.monotonic() - started, 3))
                if attempt == 1 and is_json_error(exc):
                    print("[模型] 参数 JSON 无法解析，仅重新生成一次；未重执行页面动作。", flush=True)
                    request.append(UserMessage(content="上次响应的工具参数不符合 JSON/schema。只返回符合指定函数结构的简短有效参数，不输出解释。"))
                    continue
                self.failed = True
                self.last_error = f"{type(exc).__name__}: {exc}" if str(exc) else f"模型请求超过 {self.timeout:g} 秒"
                raise ModelProviderError(f"模型生成失败：{self.last_error}；日志：{self.trace.folder}", model=self.model) from exc
