from datetime import datetime, timezone

from sqlmodel import Field, SQLModel


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class DocumentModel(SQLModel, table=True):
    __tablename__ = "document"

    id: str = Field(primary_key=True)
    title: str
    original_name: str
    sha256: str
    size_bytes: int
    page_count: int
    cursor: int = 0
    version: int = 0
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class OpModel(SQLModel, table=True):
    __tablename__ = "op"

    id: int | None = Field(default=None, primary_key=True)
    document_id: str = Field(index=True)
    seq: int = Field(index=True)
    kind: str
    payload: str
    created_at: datetime = Field(default_factory=utcnow)


class PageRefModel(SQLModel, table=True):
    __tablename__ = "page_ref"

    document_id: str = Field(primary_key=True)
    page_uuid: str = Field(primary_key=True)
    origin: str
    origin_index: int | None = None


class PresetModel(SQLModel, table=True):
    __tablename__ = "preset"

    id: str = Field(primary_key=True)
    name: str = Field(unique=True)
    params: str
    builtin: bool = False
    created_at: datetime = Field(default_factory=utcnow)


class JobModel(SQLModel, table=True):
    __tablename__ = "job"

    id: str = Field(primary_key=True)
    document_id: str = Field(index=True)
    kind: str
    status: str
    progress: float = 0.0
    message: str | None = None
    result_path: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
