"""
SQLAlchemy engine + session 工厂
"""
from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings

# SQLite WAL 模式提升并发读写性能
engine = create_engine(
    settings.database_url,
    connect_args={"check_same_thread": False},
    echo=settings.web_debug,
    pool_pre_ping=True,
)


@event.listens_for(engine, "connect")
def _set_sqlite_pragma(dbapi_conn, _connection_record):
    """
    SQLite 优化：WAL 模式 + 外键约束 + 写锁等待

    busy_timeout=5000ms：当另一个写事务持有锁时，最多等待 5 秒后再抛
    OperationalError，避免多线程（APScheduler + FastAPI）并发写时立即报
    'database is locked'。结合 max_instances=1 调度器配置，实际冲突极少。
    """
    cursor = dbapi_conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.execute("PRAGMA cache_size=-64000")  # 64 MB
    cursor.execute("PRAGMA busy_timeout=5000")  # 等待最多 5 秒（毫秒单位）
    cursor.close()


SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine,
    expire_on_commit=False,   # commit 后属性不失效，避免跨 session 的 DetachedInstanceError
)


def get_db() -> Generator[Session, None, None]:
    """FastAPI 依赖注入用的 DB session 生成器"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def db_session() -> Generator[Session, None, None]:
    """非 FastAPI 上下文（scripts / jobs）用的 session 上下文管理器"""
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
