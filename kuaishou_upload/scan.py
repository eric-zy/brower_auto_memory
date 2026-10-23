from __future__ import annotations

import hashlib
import json
import os
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from .database import DONE
from .ledger import Ledger, PUBLISHED
from .schema import file_hash, load_job
from .platforms import get_adapter

EXCLUDED = {"testdata", "screenshots", ".kuaishou-upload", ".venv", ".git", "__pycache__"}


def sidecar_for(video):
    return video.with_suffix(".kuaishou.json")


def discover_videos(directory: Path):
    directory = directory.resolve(strict=True)
    if not directory.is_dir():
        raise ValueError(f"不是目录: {directory}")
    result = []
    for base, dirs, files in os.walk(directory, followlinks=False):
        dirs[:] = [name for name in dirs if name.casefold() not in EXCLUDED
                   and not (Path(base) / name).is_symlink()
                   and not (Path(base) / name).is_junction()]
        for name in files:
            path = Path(base) / name
            if path.suffix.lower() == ".mp4" and not path.is_symlink():
                result.append(path.resolve(strict=True))
    return sorted(result, key=lambda p: [int(part) if part.isdigit() else part.casefold() for part in re.split(r"(\d+)", str(p))])


@dataclass
class ScanResult:
    items: list[dict] = field(default_factory=list)
    jobs: dict = field(default_factory=dict)
    config_path: str | None = None

    def summary(self):
        return {"counts": dict(Counter(item["status"] for item in self.items)), "files": self.items,
                "config": self.config_path, "generated": sum(bool(item.get("generated")) for item in self.items),
                "test_jobs": sum(job.manifest.testMode for job in self.jobs.values())}

    def checklist(self, directory):
        summary = self.summary()
        lines = ["# 上传预检清单", "", f"视频目录：`{directory}`", "",
                 f"待上传 {summary['counts'].get('pending', 0)}；已上传跳过 {summary['counts'].get('skipped', 0)}；"
                 f"阻塞 {summary['counts'].get('blocked', 0)}；已自动生成/更新 JSON {summary['generated']} 份。", ""]
        if self.config_path:
            lines += [f"公共固定资料只需填写一次：`{self.config_path}`。填写后重跑，所有单集 JSON 自动更新。", ""]
        for item in self.items:
            if item["status"] != "blocked":
                continue
            video = Path(item["video"])
            lines += [f"## {video.relative_to(directory)}", ""]
            if item.get("manifest"):
                lines += [f"自动生成资料：`{item['manifest']}`", ""]
            for reason in item.get("issues", [item.get("reason", "未知原因")]):
                lines.append(f"- {reason}")
            lines.append("")
        return "\n".join(lines)

    def ordered_jobs(self):
        return sorted(self.jobs.items(), key=lambda pair: (pair[1].manifest.seriesTitle.casefold(), pair[1].manifest.episodes[0].number))


def series_key(job):
    # Compatibility export; the scanner uses the configured adapter below.
    return get_adapter('kuaishou').series_key(job)


def scan_directory(settings, database, *, clock=time.time):
    adapter = get_adapter(settings.platform)
    if database.scope != (settings.platform, settings.account_id):
        raise ValueError('扫描设置与数据库平台/账号不一致')
    videos = discover_videos(settings.directory)
    names = Counter(video.name.casefold() for video in videos)
    legacy = Ledger(settings.ledger).read()
    report, seen = ScanResult(), set()
    builder = None
    if settings.config:
        builder = adapter.metadata_builder(settings, videos)
        report.config_path = str(settings.config)
    for video in videos:
        item = {"video": str(video)}
        report.items.append(item)
        try:
            before = video.stat()
            if before.st_size == 0 or clock() - before.st_mtime < settings.stable_seconds or any(
                p.exists() for p in (video.with_suffix(".mp4.downloading"), video.with_suffix(".downloading"))
            ):
                item.update(status="deferred", reason="文件为空、仍在下载或修改时间尚未稳定")
                continue
            sha256 = file_hash(video)
            after = video.stat()
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                item.update(status="deferred", reason="计算哈希时文件发生变化")
                continue
            item["sha256"] = sha256
            old = database.get(sha256)
            if old and old["status"] in DONE:
                item.update(status="skipped", reason="视频内容已上传（哈希匹配）")
                continue
            row = Ledger.match(legacy, video, sha256, unique_name=names[video.name.casefold()] == 1,
                               platform=settings.platform, account_id=settings.account_id)
            if row and str(row.get("发布状态") or "").strip().lower() in PUBLISHED:
                database.update(sha256, path=str(video), status="legacy_uploaded", ledger_dirty=1,
                    content_id=str(row.get("作品ID") or "") or None,
                    message="历史台账已发布，跳过；原台账无哈希时按唯一文件名或路径匹配",
                    details={"ledgerRow": row["_row"], "reviewStatus": row.get("审核状态")})
                item.update(status="skipped", reason="现有发布记录.xlsx 已标记发布")
                continue
            if sha256 in seen:
                item.update(status="duplicate", reason="本轮存在相同内容的视频，只处理一份")
                continue
            details = {"jobId": database.task_id(sha256), **({"ledgerRow": row["_row"]} if row else {})}
            sidecar = video.with_suffix(adapter.sidecar_suffix)
            if builder:
                sidecar = builder.build(video, sha256, sidecar)
                item.update(generated=True, manifest=str(sidecar))
            elif not sidecar.is_file():
                raise ValueError(f"待补资料: 缺少 {sidecar.name}")
            database.update(sha256, path=str(video), sidecar=str(sidecar), details=details)
            job = adapter.load_job(sidecar, live=True, video_override=video, known_video_hash=sha256, allow_test_prepare=True)
            final = video.stat()
            if (after.st_size, after.st_mtime_ns) != (final.st_size, final.st_mtime_ns):
                raise ValueError("自动生成资料时视频发生变化，下次文件稳定后重新扫描")
            old = database.get(sha256)
            if old["fingerprint"] and old["fingerprint"] != job.fingerprint and (
                old["content_id"] or old["details"].get("uploadedAssets") or old["status"] in {"unknown", "submitting"}
            ):
                raise ValueError("已有平台操作后输入资料发生变化，先核对平台，禁止自动恢复")
            # 导入前一版同一集的状态，保留提交不明/已提交记录，不能因换入口重复上传。
            previous_path = settings.runtime_root / "live" / job.job_id / "run_state.json"
            previous = json.loads(previous_path.read_text(encoding="utf-8")) if previous_path.exists() and not old["fingerprint"] else {}
            if previous and previous.get("fingerprint") != job.fingerprint:
                raise ValueError("旧任务资料指纹不一致，请先核对平台")
            status = old["status"]
            if status in {"blocked", "failed", "pending"}:
                status = "pending"
            status = {"submitted": "submitted", "exists": "exists", "submitting": "unknown", "outcome_unknown": "unknown",
                      "ready_to_submit": "ready"}.get(previous.get("status"), status)
            key = adapter.series_key(job)
            # Legacy sidecars had unscoped platformHistory: never carry their IDs
            # into a new account. New scopes discover their target on the platform.
            history_id = job.manifest.platformHistory.contentId if database.is_legacy_scope else None
            ids = {str(value) for value in (old["content_id"], previous.get("contentId"), history_id, database.series_id(key)) if value}
            if len(ids) > 1:
                raise ValueError("同一剧存在冲突的平台作品 ID，禁止自动选择")
            target = next(iter(ids), None)
            database.update(sha256, fingerprint=job.fingerprint, series_key=key, series_title=job.manifest.seriesTitle,
                            episode=job.manifest.episodes[0].number, content_id=target, status=status,
                            message=("模拟准备完成，未提交" if status == "test_prepared" else
                                     "等待平台核对" if status != "pending" else "待上传"), details=previous)
            if status in DONE:
                item.update(status="skipped", reason="旧版任务已经提交")
                continue
            if status == "test_prepared" and job.manifest.testMode:
                item.update(status="test_prepared", reason="已完成模拟准备，未提交；保留原草稿，不重复上传附件")
                continue
            report.jobs[sha256] = job
            seen.add(sha256)
            item.update(status="pending", series=job.manifest.seriesTitle, episode=job.manifest.episodes[0].number,
                        testMode=job.manifest.testMode)
        except Exception as exc:
            item.update(status="blocked", reason=str(exc))
            if getattr(exc, "issues", None):
                item["issues"] = exc.issues
            if "sha256" in item:
                old = database.get(item["sha256"])
                if not old or old["status"] not in DONE:
                    uncertain = old and (old["status"] in {"unknown", "submitting"} or old["details"].get("submissionAttempted"))
                    database.update(item["sha256"], path=str(video), status="unknown" if uncertain else "blocked", message=str(exc), ledger_dirty=1)
    identities = {}
    for sha256, job in report.jobs.items():
        identity = (adapter.series_key(job), job.manifest.episodes[0].number)
        identities.setdefault(identity, []).append(sha256)
    conflicts = {sha for group in identities.values() if len(group) > 1 for sha in group}
    for sha256 in conflicts:
        reason = "同剧同一集号对应多个不同视频，请调整文件名或资料中的集号后重跑"
        old = database.get(sha256)
        uncertain = old["status"] in {"unknown", "submitting"} or old["details"].get("submissionAttempted")
        database.update(sha256, status="unknown" if uncertain else "blocked", message=reason, ledger_dirty=1)
        for item in report.items:
            if item.get("sha256") == sha256 and item["status"] == "pending":
                item.update(status="blocked", reason=reason)
        del report.jobs[sha256]
    for sha256 in list(report.jobs):
        if database.get(sha256)["status"] in DONE:
            del report.jobs[sha256]
    for item in report.items:
        if item["status"] == "pending" and database.get(item["sha256"])["status"] in DONE:
            item.update(status="skipped", reason="同内容的其他文件已由历史记录确认发布")
    return report
