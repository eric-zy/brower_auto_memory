from __future__ import annotations

import copy
import json
import math
import os
import re
import subprocess
import tempfile
from pathlib import Path

from .config import PROJECT
from .schema import empty, normalize_manifest
from .state import atomic_json


def ffmpeg():
    try:
        from imageio_ffmpeg import get_ffmpeg_exe
    except ImportError:
        raise RuntimeError("自动提取视频信息需要安装依赖：uv pip install -r requirements.txt") from None
    return get_ffmpeg_exe()


def run_media(arguments):
    return subprocess.run([ffmpeg(), "-nostdin", *arguments], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=60,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def probe_duration(video):
    result = run_media(["-hide_banner", "-i", str(video)])
    match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", result.stderr)
    if not match or not re.search(r"Stream #.*Video:", result.stderr):
        raise ValueError(f"无法读取有效视频时长：{video.name}；请确认视频已完整下载且可播放")
    hours, minutes, seconds = map(float, match.groups())
    duration = hours * 3600 + minutes * 60 + seconds
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError(f"视频时长无效：{video.name}")
    return duration


def extract_cover(video, destination, duration):
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, filename = tempfile.mkstemp(dir=destination.parent, suffix=".jpg")
    os.close(fd)
    try:
        result = run_media(["-hide_banner", "-loglevel", "error", "-ss", str(min(duration / 2, 30)),
                            "-i", str(video), "-frames:v", "1", "-vf", "scale=720:-2", "-y", filename])
        if result.returncode or Path(filename).stat().st_size == 0:
            raise ValueError(f"无法截取封面：{video.name}；{result.stderr.strip()[:160]}")
        os.replace(filename, destination)
    finally:
        Path(filename).unlink(missing_ok=True)
    return destination


def series_title(video):
    stem = video.stem
    match = re.match(r"^(.*?)[_\s-]*第\d+集(?:.*)?$", stem)
    if match and match.group(1).strip(" _-"):
        return match.group(1).strip(" _-")
    parent = video.parent.name
    match = re.search(r"《([^》]+)》", parent)
    return match.group(1).strip() if match else parent


def file_number(video):
    match = re.search(r"第0*(\d+)集", video.stem) or re.search(r"(?:^|[_\s-])0*(\d+)$", video.stem)
    return int(match.group(1)) if match else None


class ManifestBuilder:
    """视频自动提取 + 可选旧资料 + 一份公共固定配置，产物全部在项目状态目录。"""

    def __init__(self, settings, videos):
        self.settings = settings
        self.path = settings.config
        if not self.path.exists():
            atomic_json(self.path, json.loads((PROJECT / "upload_config.example.json").read_text(encoding="utf-8")))
        self.defaults = json.loads(self.path.read_text(encoding="utf-8-sig"))
        allowed = {"seriesTitle", "synopsis", "sellingMode", "episodePrice", "coverFile", "copyright", "filing", "platformFields", "testMode"}
        if not isinstance(self.defaults, dict) or set(self.defaults) - allowed:
            raise ValueError("公共配置必须是 JSON 对象，且只包含 upload_config.example.json 中的字段")
        if "testMode" in self.defaults and type(self.defaults["testMode"]) is not bool:
            raise ValueError("公共配置 testMode 必须为 true 或 false")
        for key in ("copyright", "filing", "platformFields"):
            if key in self.defaults and not isinstance(self.defaults[key], dict):
                raise ValueError(f"公共配置 {key} 必须是 JSON 对象")
        counts = {}
        self.positions = {}
        for video in videos:
            title = self.defaults.get("seriesTitle") or series_title(video)
            counts[title] = counts.get(title, 0) + 1
            self.positions[video] = counts[title]

    def build(self, video, sha256, source):
        raw = json.loads(source.read_text(encoding="utf-8-sig")) if source.is_file() else {}
        data = normalize_manifest(raw, video_override=video)
        if not isinstance(data, dict):
            raise ValueError(f"旧资料必须是 JSON 对象：{source}")
        episodes = data.get("episodes")
        if episodes is not None and (not isinstance(episodes, list) or len(episodes) != 1 or not isinstance(episodes[0], dict)):
            raise ValueError("每个视频的旧资料必须恰好包含一集")
        ep = copy.deepcopy(episodes[0]) if episodes else {}
        defaults = self.defaults
        if defaults.get("testMode") is True:
            data["testMode"] = True
        for key in ("seriesTitle", "synopsis", "sellingMode", "coverFile"):
            if not empty(defaults.get(key)):
                data[key] = defaults[key]
        for key in ("copyright", "filing", "platformFields"):
            existing = data.get(key, {})
            if not isinstance(existing, dict):
                raise ValueError(f"{key} 必须是 JSON 对象")
            data[key] = {**existing, **{k: v for k, v in defaults.get(key, {}).items() if not empty(v)}}
        if empty(data.get("seriesTitle")):
            data["seriesTitle"] = series_title(video)
        number = ep.get("number", file_number(video) or self.positions[video])
        ep.update(number=number, sourceFile=str(video))
        if empty(ep.get("title")):
            ep["title"] = f"第{number}集"
        if empty(data.get("synopsis")):
            data["synopsis"] = f"《{data['seriesTitle']}》第{number}集。"
        if defaults.get("sellingMode") in {"free", "paid"}:
            ep["paid"] = defaults["sellingMode"] == "paid"
            ep["price"] = 0 if not ep["paid"] else defaults.get("episodePrice")
        data["episodes"] = [ep]
        media = data.get("mediaInfo", {})
        if not isinstance(media, dict):
            raise ValueError("mediaInfo 必须是 JSON 对象")
        if media.get("sha256") and str(media["sha256"]).lower() != sha256:
            raise ValueError("旧资料的视频 SHA256 不匹配，禁止给其他视频套用该资料")
        cache = self.settings.root / "media-cache" / f"{sha256}.json"
        duration = None
        if cache.is_file():
            duration = json.loads(cache.read_text(encoding="utf-8")).get("durationSeconds")
        if not isinstance(duration, (int, float)) or isinstance(duration, bool) or not math.isfinite(duration) or duration <= 0:
            duration = probe_duration(video)
            atomic_json(cache, {"durationSeconds": duration})
        data["mediaInfo"] = {"durationSeconds": duration, "sha256": sha256}
        if empty(data.get("coverFile")):
            cover = self.settings.root / "covers" / f"{sha256}.jpg"
            if not cover.is_file() or cover.stat().st_size == 0:
                extract_cover(video, cover, duration)
            data["coverFile"] = str(cover)
        data = normalize_manifest(data, video_override=video)
        destination = self.settings.runtime_root / "manifests" / f"{sha256}.kuaishou.json"
        atomic_json(destination, data)
        return destination
