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
