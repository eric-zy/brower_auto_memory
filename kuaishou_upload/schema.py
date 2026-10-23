from __future__ import annotations

import copy
import hashlib
import json
import re
from datetime import date
from pathlib import Path
from typing import Annotated, Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, StrictBool, StrictInt, StringConstraints, ValidationError, model_validator

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class MaterialError(ValueError):
    def __init__(self, issues):
        self.issues = list(dict.fromkeys(issues))
        super().__init__("待补资料或确认：" + "、".join(self.issues))


def empty(value):
    if value is None or value == "":
        return True
    if isinstance(value, dict):
        return all(empty(item) for item in value.values())
    if isinstance(value, list):
        return all(empty(item) for item in value)
    return False


def normalize_manifest(raw, *, video_override=None):
    """兼容已知旧字段，不改原文件；未明确提供的业务资料不推测。"""
    data = copy.deepcopy(raw)
    if not isinstance(data, dict):
        return data

    def cover(value):
        if empty(value):
            return
        current = data.get("coverFile")
        if not empty(current) and current != value:
            raise MaterialError(["coverFile 与旧版 coverPath 不一致，请确认封面"])
        data["coverFile"] = value

    cover(data.pop("coverPath", None))
    # 来源说明和媒体属性不参与表单，也不改变上传身份。
    for key in ("metadataSources", "preparation"):
        data.pop(key, None)
    media = data.get("mediaInfo")
    if isinstance(media, dict):
        for key in ("sizeBytes", "width", "height", "frameRate"):
            media.pop(key, None)
    # 只忽略旧版中的空占位；有值的未知业务字段仍需明确映射，不能静默丢弃。
    for key in ("content", "finished", "fullScene", "productionMethod", "authorDeclaration"):
        if key in data and empty(data[key]):
            data.pop(key)
    filing = data.get("filing")
    if isinstance(filing, dict) and empty(filing.get("numberType")):
        filing.pop("numberType", None)
    sale = data.get("sale")
    if isinstance(sale, dict):
        mode = sale.get("mode")
        modes = {"free": "free", "paid": "paid", "免费": "free", "整剧付费": "paid"}
        if isinstance(mode, str) and mode in modes:
            if not empty(data.get("sellingMode")) and data["sellingMode"] != modes[mode]:
                raise MaterialError(["sellingMode 与旧版 sale.mode 不一致，请确认售卖方式"])
            data["sellingMode"] = modes[mode]
            sale.pop("mode")
        if empty(sale):
            data.pop("sale")
    episodes = data.get("episodes")
    if isinstance(episodes, list):
        for ep in episodes:
            if not isinstance(ep, dict):
                continue
            cover(ep.pop("coverPath", None))
            if "no" in ep:
                if "number" in ep and (type(ep["number"]) is not type(ep["no"]) or ep["number"] != ep["no"]):
                    raise MaterialError(["number 与 no 集号冲突"])
                ep.setdefault("number", ep["no"])
                ep.pop("no")
            numbers = set()
            title = ep.get("title")
            match = re.match(r"^第0*(\d+)集(?:[：:\s]|$)", title) if isinstance(title, str) else None
            if match:
                numbers.add(int(match.group(1)))
            paths = [ep.get("sourceFile")]
            if video_override and len(episodes) == 1:
                paths.append(str(video_override))
            for value in paths:
                match = re.search(r"第0*(\d+)集$", Path(value).stem) if isinstance(value, str) else None
                if match:
                    numbers.add(int(match.group(1)))
            if len(numbers) > 1 or (numbers and type(ep.get("number")) is int and ep["number"] not in numbers):
                raise MaterialError(["标题、文件名与集号不一致，请确认集号"])
            if "number" not in ep and numbers:
                ep["number"] = numbers.pop()
            # 只有明确选择免费时，空价格才可解释为 0；未选择售卖方式不能自动按免费上传。
            if ep.get("price") is None and data.get("sellingMode") == "free":
                ep["price"] = 0
    return data


FIELD_NAMES = {
    "seriesTitle": "剧名", "synopsis": "简介", "episodes": "单集资料",
    "episodes.0.number": "集号", "episodes.0.no": "集号", "episodes.0.title": "单集标题",
    "episodes.0.sourceFile": "视频路径", "episodes.0.price": "单集价格（需先确认售卖方式）",
    "episodes.0.paid": "单集付费选择", "mediaInfo.sha256": "视频 SHA256", "mediaInfo.durationSeconds": "视频时长",
    "copyright": "版权资料", "copyright.proofType": "版权证明类型", "copyright.proofFile": "版权证明文件",
    "copyright.authorizedForUpload": "上传授权（需明确授权）", "copyright.validFrom": "版权开始日期",
    "copyright.validUntil": "版权结束日期", "copyright.allowSublicense": "转授权选择",
    "filing": "备案资料", "filing.hasNumber": "备案确认", "filing.number": "备案号",
    "coverFile": "封面文件", "sellingMode": "售卖方式",
}


def missing_materials(data, *, live, allow_test_prepare=False):
    if not isinstance(data, dict):
        return {}
    checks = ["copyright.authorizedForUpload", "filing.hasNumber"]
    if live:
        checks += ["coverFile", "sellingMode", "copyright.validFrom", "copyright.validUntil", "copyright.allowSublicense"]
    issues = {}
    for key in checks:
        value = data
        for part in key.split("."):
            value = value.get(part) if isinstance(value, dict) else None
        if empty(value) or (key in {"copyright.authorizedForUpload", "filing.hasNumber"} and value is not True):
            issues[key] = FIELD_NAMES[key]
    if live and data.get("testMode") is True and not allow_test_prepare:
        issues["testMode"] = "testMode=true 仅允许浏览器准备调试，禁止最终提交"
    return issues


class InputModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Episode(InputModel):
    number: Annotated[StrictInt, Field(ge=1, validation_alias=AliasChoices("number", "no"))]
    title: Text
    sourceFile: Text
    paid: StrictBool = False
    price: Annotated[float, Field(ge=0)] = 0


class MediaInfo(InputModel):
    durationSeconds: Annotated[float, Field(gt=0)]
    sha256: Annotated[str, StringConstraints(pattern=r"^[0-9a-fA-F]{64}$")]


class Copyright(InputModel):
    proofType: Text
    proofFile: Text
    authorizedForUpload: StrictBool
    validFrom: date | None = None
    validUntil: date | None = None
    allowSublicense: StrictBool | None = None

    @model_validator(mode="after")
    def valid_dates(self):
        if self.validUntil and self.validFrom and self.validUntil < self.validFrom:
            raise ValueError("版权有效期结束日期早于开始日期")
        return self


class Filing(InputModel):
    hasNumber: StrictBool
    # 统一采用导入 skill 实操段的规则；正式平台规则变化时在此调整，不自动截取/补号。
    number: Annotated[str, StringConstraints(pattern=r"^V[0-9]{14}$")]


class PlatformHistory(InputModel):
    contentId: str | None = None
    uploadedEpisodes: list[StrictInt] = Field(default_factory=list)


class Manifest(InputModel):
    seriesTitle: Text
    synopsis: Text
    episodes: Annotated[list[Episode], Field(min_length=1, max_length=1)]
    mediaInfo: MediaInfo
    copyright: Copyright
    filing: Filing
    coverFile: Text | None = None
    sellingMode: Literal["free", "paid"] | None = None
    # 平台额外必填项，键为页面字段名称，值必须由用户提供，不由模型补全。
    platformFields: dict[str, str | bool] = Field(default_factory=dict)
    platformHistory: PlatformHistory = Field(default_factory=PlatformHistory)
    testMode: StrictBool = False

    @model_validator(mode="after")
    def validate_business(self):
        if not self.copyright.authorizedForUpload:
            raise ValueError("待补资料: 上传授权")
        if not self.filing.hasNumber:
            raise ValueError("待补资料: 备案信息")
        episode = self.episodes[0]
        if self.sellingMode == "paid" and (not episode.paid or episode.price <= 0):
            raise ValueError("单集整剧付费模式要求该集选择付费且价格大于 0")
        if self.sellingMode == "free" and (episode.paid or episode.price != 0):
            raise ValueError("免费模式要求单集免费且价格为 0")
        for key, value in self.platformFields.items():
            if not key.strip() or (isinstance(value, str) and not value.strip()):
                raise ValueError("platformFields 名称和值不得为空")
        return self


class ValidatedJob:
    def __init__(self, manifest: Manifest, files: dict[str, Path], source: Path, *, asset_hashes=None):
        self.manifest, self.files, self.source = manifest, files, source
        self.asset_hashes = asset_hashes or {role: file_hash(file) for role, file in files.items()}
        episode = manifest.episodes[0]
        identity = f"{manifest.seriesTitle}\0{episode.number}\0{manifest.mediaInfo.sha256.lower()}"
        self.job_id = hashlib.sha256(identity.encode()).hexdigest()[:24]
        canonical = manifest.model_dump(mode="json")
        # 集数历史是提示信息；明确指定的目标作品 ID 变更则必须重新核对任务。
        canonical.pop("platformHistory")
        if manifest.platformHistory.contentId:
            canonical["targetContentId"] = manifest.platformHistory.contentId
        canonical["assetHashes"] = self.asset_hashes
        self.fingerprint = hashlib.sha256(json.dumps(canonical, ensure_ascii=False, sort_keys=True).encode()).hexdigest()

    def fields(self) -> dict[str, str | bool]:
        m, ep = self.manifest, self.manifest.episodes[0]
        fields = {
            "剧名": m.seriesTitle, "作品简介": m.synopsis, "集数标题": ep.title,
            "备案号": m.filing.number, "版权类型": m.copyright.proofType,
            "版权开始日期": str(m.copyright.validFrom), "版权结束日期": str(m.copyright.validUntil),
            "允许转授权": m.copyright.allowSublicense,
            "售卖方式": ("整剧付费" if m.sellingMode == "paid" else "免费") if m.sellingMode else None,
            "单集付费": ep.paid, "单集价格": str(ep.price), "添加集数": "1",
        }
        for key, value in m.platformFields.items():
            if key in fields:
                raise ValueError(f"platformFields 不得覆盖内置字段: {key}")
            fields[key] = value
        return {key: value for key, value in fields.items() if value is not None and value != "None"}


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_job(path: Path, *, live: bool = True, video_override: Path | None = None,
             known_video_hash: str | None = None, allow_test_prepare: bool = False) -> ValidatedJob:
    path = path.expanduser().resolve(strict=True)
    if not path.name.endswith(".kuaishou.json"):
        raise ValueError("预检文件必须以 .kuaishou.json 结尾")
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"JSON 格式错误：第 {exc.lineno} 行，第 {exc.colno} 列") from None
    data = normalize_manifest(raw, video_override=video_override)
    missing = missing_materials(data, live=live, allow_test_prepare=allow_test_prepare)
    try:
        m = Manifest.model_validate(data)
    except ValidationError as exc:
        messages = {"missing": "缺少字段", "extra_forbidden": "不支持字段", "string_too_short": "不能为空",
                    "bool_type": "必须为布尔值", "float_type": "必须为数字", "int_type": "必须为整数"}
        errors = sorted(exc.errors(include_input=False, include_url=False), key=lambda error: error["type"] == "extra_forbidden")
        reasons = []
        for error in errors:
            key = ".".join(map(str, error["loc"]))
            if key in missing:
                continue
            # 授权和备案的缺失已汇总；不重复显示模型级的同一条错误。
            if not key and error["type"] == "value_error" and any(word in error["msg"] for word in ("上传授权", "备案信息")):
                continue
            label = FIELD_NAMES.get(key, key or "资料")
            if error["type"] in {"missing", "string_too_short"}:
                reasons.append(label)
            elif key == "filing.number" and error["type"] == "string_pattern_mismatch":
                reasons.append("备案号" if empty(data.get("filing", {}).get("number")) else "备案号（应为 V 加 14 位数字）")
            else:
                reasons.append(f"{label}：{messages.get(error['type'], error['msg'].removeprefix('Value error, '))}")
        raise MaterialError([*missing.values(), *reasons]) from None
    if missing:
        raise MaterialError(missing.values())
    errors = []
    if live:
        if m.copyright.validFrom and m.copyright.validUntil and not (
            m.copyright.validFrom <= date.today() <= m.copyright.validUntil
        ):
            errors.append("版权授权不在有效期内")
        if "\ufffd" in json.dumps(m.model_dump(mode="json"), ensure_ascii=False):
            errors.append("资料包含 Unicode 替换字符，存在编码损坏，请核对原文件")
    source_files = {"video": str(video_override) if video_override else m.episodes[0].sourceFile,
                    "copyright": m.copyright.proofFile, "cover": m.coverFile}
    files = {}
    suffixes = {"video": {".mp4"}, "copyright": {".pdf", ".png", ".jpg", ".jpeg", ".webp"},
                "cover": {".png", ".jpg", ".jpeg", ".webp"}}
    for role, value in source_files.items():
        if value is None:
            continue
        file = Path(value).expanduser()
        if not file.is_absolute():
            errors.append(f"{role} 必须使用绝对路径")
            continue
        if not file.exists():
            errors.append(f"文件不存在: {role} {file}")
            continue
        file = file.resolve(strict=True)
        if not file.is_file() or file.stat().st_size == 0:
            errors.append(f"文件为空或不是普通文件: {role}")
            continue
        if file.suffix.lower() not in suffixes[role]:
            errors.append(f"不支持的文件类型: {role} {file.suffix}")
            continue
        files[role] = file
    hashes = {role: (known_video_hash if role == "video" and known_video_hash else file_hash(file)) for role, file in files.items()}
    if "video" in files and hashes["video"] != m.mediaInfo.sha256.lower():
        errors.append("视频 SHA256 不匹配，禁止上传")
    if errors:
        raise ValueError("\n".join(errors))
    job = ValidatedJob(m, files, path, asset_hashes=hashes)
    job.fields()
    return job
