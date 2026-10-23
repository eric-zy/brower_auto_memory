"""Explicit platform registry. Unknown sites never inherit Kuaishou rules."""
from dataclasses import dataclass
from importlib import import_module


@dataclass(frozen=True)
class AdapterSpec:
    name: str
    browser_class: str
    task_class: str
    skill_path: str
    sidecar_suffix: str
    manifest_loader: str
    builder_class: str
    series_identity: str

    @staticmethod
    def load(target):
        module, name = target.split(':')
        return getattr(import_module(module), name)

    def create_browser(self, job, settings):
        return self.load(self.browser_class)(job, settings.runtime_root, headless=settings.headless,
                                             expected_account=settings.expected_account,
                                             recovery_steps=settings.recovery_steps)

    def create_task(self, *args, **kwargs):
        return self.load(self.task_class)(*args, **kwargs)

    def load_job(self, *args, **kwargs):
        return self.load(self.manifest_loader)(*args, **kwargs)

    def metadata_builder(self, settings, videos):
        return self.load(self.builder_class)(settings, videos)

    def series_key(self, job):
        return self.load(self.series_identity)(job)


_ADAPTERS = {
    'kuaishou': AdapterSpec('kuaishou',
        'kuaishou_upload.platforms.kuaishou_browser:PlatformBrowser',
        'kuaishou_upload.platforms.kuaishou_workflow:KuaishouUploadTask',
        'skills/kuaishou-minidrama-upload/SKILL.md', '.kuaishou.json',
        'kuaishou_upload.schema:load_job', 'kuaishou_upload.prepare:ManifestBuilder',
        'kuaishou_upload.platforms.kuaishou_metadata:series_key')
}


def get_adapter(platform):
    try:
        return _ADAPTERS[platform]
    except KeyError:
        raise ValueError(f'尚未实现平台适配器：{platform}；可用平台：{", ".join(_ADAPTERS)}') from None


def register_adapter(adapter):
    if adapter.name in _ADAPTERS:
        raise ValueError('平台适配器已注册')
    _ADAPTERS[adapter.name] = adapter
