from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
PLATFORM_URL = "https://kdj.kuaishou.com/home/content/content-management"


def flag(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    if value.lower() not in {"true", "false", "1", "0"}:
        raise ValueError(f"{name} 必须为 true 或 false")
    return value.lower() in {"true", "1"}


@dataclass
class Settings:
    directory: Path
    root: Path
    ledger: Path
    headless: bool = False
    stable_seconds: float = 30
    max_steps: int = 80
    # 直接构造 Settings 时可关闭资料生成，便于独立测试；命令入口默认启用。
    config: Path | None = None
    max_jobs: int = 1
    request_timeout: float = 30
    upload_timeout: float = 600
    platform: str = 'kuaishou'
    account_id: str = 'default'
    expected_account: str = ''
    use_vision: bool = False
    recovery_steps: int = 2
    browser_backend: str = 'stagehand'

    @property
    def runtime_root(self):
        if (self.platform, self.account_id) == ('kuaishou', 'default'):
            return self.root
        scope = hashlib.sha256(f'{self.platform}\0{self.account_id}'.encode()).hexdigest()[:24]
        return self.root / 'scopes' / scope

    @classmethod
    def from_env(cls):
        directory = Path(os.getenv("UPLOAD_DIR", "E:/upload_test")).expanduser().resolve()
        settings = cls(directory=directory,
            root=Path(os.getenv("UPLOAD_STATE_DIR", str(PROJECT / ".kuaishou-upload"))).expanduser().resolve(),
            ledger=Path(os.getenv("UPLOAD_LEDGER", str(directory / "发布记录.xlsx"))).expanduser().resolve(),
            headless=flag("UPLOAD_HEADLESS"), stable_seconds=float(os.getenv("UPLOAD_STABLE_SECONDS", "30")),
            max_steps=int(os.getenv("UPLOAD_MAX_STEPS", "80")),
            config=Path(os.getenv("UPLOAD_CONFIG", str(PROJECT / "upload_config.json"))).expanduser().resolve(),
            max_jobs=int(os.getenv("UPLOAD_MAX_JOBS", "1")),
            request_timeout=float(os.getenv("UPLOAD_REQUEST_TIMEOUT", "30")),
            upload_timeout=float(os.getenv("UPLOAD_TIMEOUT", "600")),
            platform=os.getenv('UPLOAD_PLATFORM', 'kuaishou').strip(),
            account_id=os.getenv('UPLOAD_ACCOUNT_ID', 'default').strip(),
            expected_account=os.getenv('UPLOAD_EXPECTED_ACCOUNT', '').strip(),
            use_vision=flag('UPLOAD_USE_VISION'),
            recovery_steps=int(os.getenv('UPLOAD_RECOVERY_STEPS', '2')),
            browser_backend=os.getenv('UPLOAD_BROWSER_BACKEND', 'stagehand').strip())
        if settings.browser_backend not in {'stagehand', 'browser_use'}:
            raise ValueError('UPLOAD_BROWSER_BACKEND必须为stagehand或browser_use')
        from .platforms import get_adapter
        get_adapter(settings.platform)  # unsupported platforms stop before scanning/writing
        if not settings.account_id or not 0 <= settings.recovery_steps <= 3:
            raise ValueError('账号身份不得为空，恢复步数须为0至3')
        if settings.account_id != 'default' and not settings.expected_account:
            raise ValueError('新账号必须配置 UPLOAD_EXPECTED_ACCOUNT，核对页面账号后才允许继续')
        if settings.stable_seconds < 0 or settings.max_steps < 1:
            raise ValueError("文件稳定秒数不能为负，最大步数必须大于零")
        if settings.max_jobs < 0 or settings.request_timeout <= 0 or settings.upload_timeout <= 0:
            raise ValueError("UPLOAD_MAX_JOBS 不得为负，模型及上传超时必须大于零")
        if settings.root == directory:
            raise ValueError("UPLOAD_STATE_DIR 不能等于视频目录")
        return settings
