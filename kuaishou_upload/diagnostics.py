from __future__ import annotations

import json
import time
from uuid import uuid4
from pathlib import Path

from .state import atomic_json, now


class RunTrace:
    def __init__(self, folder: Path, *, secrets=()):
        self.folder = folder
        self.folder.mkdir(parents=True, exist_ok=True)
        self.secrets = [value for value in secrets if value]
        self.stage = "starting"
        self.started = time.monotonic()

    def redact(self, value):
        text = str(value)
        for secret in self.secrets:
            text = text.replace(secret, "[redacted]")
        return text

    def event(self, kind, **details):
        data = {"time": now(), "elapsed": round(time.monotonic() - self.started, 3),
                "stage": self.stage, "event": kind, **details}
        with (self.folder / "events.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(self.redact(json.dumps(data, ensure_ascii=False, default=str)) + "\n")

    def error(self, exc, **details):
        causes, current, raw = [], exc, None
        seen = set()
        while current and id(current) not in seen:
            seen.add(id(current))
            causes.append({"type": type(current).__name__, "message": self.redact(current)})
            if isinstance(current, json.JSONDecodeError):
                raw = current.doc
                details["jsonPosition"] = current.pos
            current = current.__cause__ or current.__context__
        if raw is not None:
            filename = f"raw_arguments_{time.time_ns()}_{uuid4().hex}.txt"
            (self.folder / filename).write_text(self.redact(raw), encoding="utf-8")
            details["rawArguments"] = filename
            details["rawLength"] = len(raw)
        data = {"time": now(), "stage": self.stage, "causes": causes, **details}
        # 仅记录响应/异常，不记录请求头或环境变量。
        atomic_json(self.folder / "last_error.json", json.loads(self.redact(json.dumps(data, ensure_ascii=False))))
        self.event("error", **data)


def is_json_error(exc):
    seen = set()
    while exc and id(exc) not in seen:
        seen.add(id(exc))
        if isinstance(exc, json.JSONDecodeError) or type(exc).__name__ == "ValidationError":
            return True
        if any(word in str(exc) for word in ("Expecting ',' delimiter", "Invalid JSON", "Unterminated string", "Expected tool_calls")):
            return True
        exc = exc.__cause__ or exc.__context__
    return False
