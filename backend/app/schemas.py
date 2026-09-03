from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class ErrorPayload(BaseModel):
    code: str
    message: str
    detail: dict = Field(default_factory=dict)


class ErrorResponse(BaseModel):
    error: ErrorPayload


class Document(BaseModel):
    id: str
    title: str
    original_name: str
    size_bytes: int
    page_count: int
    cursor: int
    version: int
    created_at: datetime
    updated_at: datetime


class PageDocuments(BaseModel):
    items: list[Document]
    total: int
    limit: int
    offset: int


class UpdateDocumentRequest(BaseModel):
    title: str = Field(min_length=1, max_length=200)


class PageInfo(BaseModel):
    uuid: str
    index: int
    width: float
    height: float
    rotation: Literal[0, 90, 180, 270]
    has_text: bool
    label: str | None = None


class TextBlock(BaseModel):
    page: str
    block_index: int
    rect: tuple[float, float, float, float]
    text: str


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=200)
    caseSensitive: bool = False


class SearchHit(BaseModel):
    page: str
    rects: list[tuple[float, float, float, float]]
    preview: str


class OpRequest(BaseModel):
    kind: str
    payload: dict[str, Any] = Field(default_factory=dict)


class StoredOp(BaseModel):
    seq: int
    kind: str
    payload: dict[str, Any]
    created_at: datetime


class OpLogResponse(BaseModel):
    cursor: int
    ops: list[StoredOp]


class OpResult(BaseModel):
    cursor: int
    canUndo: bool
    canRedo: bool
    pageCount: int
    version: int
    warnings: list[str] = Field(default_factory=list)


class ScanParams(BaseModel):
    seed: int | None = None
    dpi: int = Field(200, ge=72, le=600)
    color_mode: Literal["gray", "color", "bw"] = "gray"
    paper_tint: str = "#FFFFFF"
    gamma: float = Field(1.0, ge=0.4, le=2.5)
    brightness: float = Field(1.0, ge=0.5, le=1.5)
    contrast: float = Field(1.0, ge=0.5, le=2.0)
    jitter: float = Field(0.03, ge=0.0, le=0.2)
    blur_sigma: float = Field(0.0, ge=0.0, le=3.0)
    noise_sigma: float = Field(0.0, ge=0.0, le=40.0)
    noise_mono: bool = True
    bw_threshold: int = Field(128, ge=0, le=255)
    bw_dither: bool = True
    jpeg_quality: int = Field(75, ge=20, le=98)
    downsample: float = Field(1.0, ge=0.25, le=1.0)


class Preset(BaseModel):
    id: str
    name: str
    params: dict[str, Any]
    builtin: bool
    created_at: datetime


class CreatePresetRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    params: ScanParams


class ScanPreviewRequest(BaseModel):
    pageUuid: str
    params: ScanParams
    previewDpi: int = Field(110, ge=72, le=150)


class ScanRunRequest(BaseModel):
    params: ScanParams
    mode: Literal["in_place", "export"] = "export"


class CompressRequest(BaseModel):
    profile: Literal["light", "balanced", "strong"] = "balanced"
    stripMetadata: bool = True
    imageDpi: int = Field(200, ge=72, le=600)


class CompressEstimateResponse(BaseModel):
    sourceBytes: int
    estimatedBytes: int
    estimatedReductionPercent: float
    profile: Literal["light", "balanced", "strong"]
    note: str | None = None


class Job(BaseModel):
    id: str
    document_id: str
    kind: Literal["scan", "export", "ocr", "compress"]
    status: Literal["queued", "running", "done", "error", "cancelled"]
    progress: float
    message: str | None = None
    result_path: str | None = None
    created_at: datetime
    updated_at: datetime
