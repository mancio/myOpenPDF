from datetime import datetime

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
