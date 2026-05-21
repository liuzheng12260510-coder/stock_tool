"""
FastAPI 依赖注入
"""
from __future__ import annotations

from collections.abc import Generator

from sqlalchemy.orm import Session

from app.db.session import get_db


def get_db_session() -> Generator[Session, None, None]:
    """DB session 注入"""
    yield from get_db()
