from __future__ import annotations
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field

class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["fill", "choose", "click", "upload", "scroll", "block"]
    ref: str = ""
    field: str = ""
    asset: Literal["", "video", "copyright", "cover"] = ""
    reason: str = Field(default="", max_length=500)
    intent: Literal['', 'open_upload', 'open_list', 'search', 'search_field', 'next', 'add_episode'] = ''


class Params(BaseModel):
    model_config = ConfigDict(extra="forbid")


class JobParams(Params):
    job_id: str


class InspectParams(JobParams):
    action: Literal["read", "search", "click", "confirm_target", "open_detail", "return_to_upload", "scroll"] = "read"
    ref: str = ""


class FieldBinding(Params):
    name: str
    ref: str
    mode: Literal["fill", "choose", "open"] = "fill"


class FormParams(JobParams):
    fields: list[FieldBinding] = Field(default_factory=list, max_length=30)
    read_only: bool = False
    verify: bool = False
    next_ref: str = ""


class AssetBinding(Params):
    role: Literal["video", "copyright", "cover"]
    ref: str


class UploadParams(JobParams):
    files: list[AssetBinding] = Field(min_length=1, max_length=3)


class ScanParams(Params):
    pass


