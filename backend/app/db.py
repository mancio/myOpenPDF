from collections.abc import Generator
from functools import lru_cache

from sqlmodel import Session, SQLModel, create_engine

from app.config import get_settings


@lru_cache
def get_engine():
    settings = get_settings()
    return create_engine(f"sqlite:///{settings.db_path}", connect_args={"check_same_thread": False})


def create_db_and_tables() -> None:
    SQLModel.metadata.create_all(get_engine())


def get_session() -> Generator[Session, None, None]:
    with Session(get_engine()) as session:
        yield session
