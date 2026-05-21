"""
初始化数据库 + schema 迁移 + 拉取股票基础信息

运行方式：python scripts/init_db.py

设计原则：
- 幂等：反复运行安全，不重复建表/加列
- 版本化：通过 schema_migrations 表记录已执行的 DDL 版本
- 最小化：仅使用裸 DDL（不引入 Alembic），适合单 SQLite 场景

迁移版本列表（MIGRATIONS）：
  v1 — 初始建表（由 SQLAlchemy metadata.create_all 处理）
  v2 — P0: stocks 表加 is_blacklisted / is_st / is_new_listing / delist_date 列；
           新建 blacklist / schema_migrations 表
  v3 — P1 (P1_01_financial_deep_columns):
           financial_quarters 表增 9 列（杜邦/盈余质量字段）；
           新建 dividend_history 表；
           factor_scores 表增 5 列（dupont/earnings_quality/dividend_continuity）
  v4 — P2 (P2_01_job_checkpoints):
           job_runs 表增 4 列（current_step / step_progress / checkpoint_data / last_heartbeat）；
           新建 job_checkpoints 断点明细表
"""
from __future__ import annotations

import sys
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

# 确保项目根目录在 sys.path
sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import text

from app.core.config import settings
from app.core.logging import configure_logging, get_logger
from app.db.models import Base
from app.db.session import engine

configure_logging(settings.logs_dir)
logger = get_logger("init_db")


# ──────────────────────────────────────────────────────────────────────────────
# Schema 迁移注册表
# 格式：(version: int, description: str, ddl_callable)
# ddl_callable 接受一个 sqlalchemy.Connection 参数，执行需要的 DDL
# ──────────────────────────────────────────────────────────────────────────────

def _migrate_v2(conn) -> None:
    """
    v2 迁移 — P0 变更：
    1. stocks 表增加 4 个新列（is_blacklisted, is_st, is_new_listing, delist_date）
    2. blacklist 表和 schema_migrations 表已由 metadata.create_all 创建
       此处只处理存量 stocks 表的 ALTER TABLE
    """
    # SQLite 的 ALTER TABLE 只支持 ADD COLUMN，逐列执行，忽略已存在的列
    new_columns = [
        ("is_blacklisted", "BOOLEAN NOT NULL DEFAULT 0"),
        ("is_st",          "BOOLEAN NOT NULL DEFAULT 0"),
        ("is_new_listing", "BOOLEAN NOT NULL DEFAULT 0"),
        ("delist_date",    "DATE"),
    ]
    for col_name, col_def in new_columns:
        try:
            conn.execute(text(f"ALTER TABLE stocks ADD COLUMN {col_name} {col_def}"))
            logger.info("migration.add_column", table="stocks", column=col_name)
        except Exception as e:
            # SQLite 对已存在列会抛 OperationalError: duplicate column name
            if "duplicate column name" in str(e).lower():
                logger.debug("migration.column_exists", table="stocks", column=col_name)
            else:
                raise

    # 为新列创建索引（忽略已存在的索引）
    index_ddls = [
        "CREATE INDEX IF NOT EXISTS ix_stocks_is_blacklisted ON stocks (is_blacklisted)",
    ]
    for ddl in index_ddls:
        try:
            conn.execute(text(ddl))
        except Exception as e:
            logger.debug("migration.index_skip", reason=str(e)[:100])


def _migrate_v3(conn) -> None:
    """
    v3 迁移 — P1_01_financial_deep_columns：
    1. financial_quarters 增加 9 个新列（杜邦分解 + 盈余质量）
    2. 新建 dividend_history 表（如已存在则跳过）
    3. factor_scores 增加 5 个新列（P1 评分字段）
    """
    # ── financial_quarters 增列 ──────────────────────────────────────────
    fq_columns = [
        ("roe",            "REAL"),
        ("npta",           "REAL"),
        ("debt_to_assets", "REAL"),
        ("assets_turn",    "REAL"),
        ("eqt_multiplier", "REAL"),
        ("netprofit_yoy",  "REAL"),
        ("op_yoy",         "REAL"),
        ("n_cashflow_act", "REAL"),
        ("cfo_to_np",      "REAL"),
    ]
    for col_name, col_def in fq_columns:
        try:
            conn.execute(text(
                f"ALTER TABLE financial_quarters ADD COLUMN {col_name} {col_def}"
            ))
            logger.info("migration.add_column", table="financial_quarters", column=col_name)
        except Exception as e:
            if "duplicate column name" in str(e).lower():
                logger.debug("migration.column_exists",
                             table="financial_quarters", column=col_name)
            else:
                raise

    # ── 新建 dividend_history 表 ─────────────────────────────────────────
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS dividend_history (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            ts_code       VARCHAR(10) NOT NULL,
            ann_date      DATE,
            end_date      DATE NOT NULL,
            div_proc      VARCHAR(20) NOT NULL DEFAULT '',
            stk_div       REAL,
            cash_div      REAL,
            cash_div_tax  REAL,
            base_date     DATE,
            pay_date      DATE,
            record_date   DATE,
            ex_date       DATE,
            UNIQUE(ts_code, end_date)
        )
    """))
    logger.info("migration.create_table_if_not_exists", table="dividend_history")

    # 为 dividend_history 创建索引（IF NOT EXISTS 自动幂等）
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_dividend_ts_code ON dividend_history (ts_code)"
    ))
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_dividend_end_date ON dividend_history (end_date)"
    ))

    # ── factor_scores 增列 ───────────────────────────────────────────────
    fs_columns = [
        ("dupont_score",             "REAL"),
        ("high_leverage_flag",       "BOOLEAN DEFAULT 0"),
        ("earnings_quality_score",   "REAL"),
        ("dividend_continuity",      "INTEGER"),
        ("dividend_continuity_score","REAL"),
    ]
    for col_name, col_def in fs_columns:
        try:
            conn.execute(text(
                f"ALTER TABLE factor_scores ADD COLUMN {col_name} {col_def}"
            ))
            logger.info("migration.add_column", table="factor_scores", column=col_name)
        except Exception as e:
            if "duplicate column name" in str(e).lower():
                logger.debug("migration.column_exists",
                             table="factor_scores", column=col_name)
            else:
                raise


def _migrate_v4(conn) -> None:
    """
    v4 迁移 — P2_01_job_checkpoints：
    1. job_runs 增加 4 列（current_step, step_progress, checkpoint_data, last_heartbeat）
    2. 新建 job_checkpoints 断点明细表（如已存在则跳过）
    """
    # ── job_runs 增列 ─────────────────────────────────────────────────────
    jr_columns = [
        ("current_step",    "VARCHAR(50) NOT NULL DEFAULT ''"),
        ("step_progress",   "INTEGER NOT NULL DEFAULT 0"),
        ("checkpoint_data", "TEXT NOT NULL DEFAULT ''"),
        ("last_heartbeat",  "DATETIME"),
    ]
    for col_name, col_def in jr_columns:
        try:
            conn.execute(text(f"ALTER TABLE job_runs ADD COLUMN {col_name} {col_def}"))
            logger.info("migration.add_column", table="job_runs", column=col_name)
        except Exception as e:
            if "duplicate column name" in str(e).lower():
                logger.debug("migration.column_exists", table="job_runs", column=col_name)
            else:
                raise

    # ── 新建 job_checkpoints 表 ───────────────────────────────────────────
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS job_checkpoints (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            job_run_id  INTEGER NOT NULL,
            step_name   VARCHAR(50) NOT NULL,
            status      VARCHAR(20) NOT NULL DEFAULT 'pending',
            started_at  DATETIME,
            finished_at DATETIME,
            payload     TEXT NOT NULL DEFAULT '',
            UNIQUE(job_run_id, step_name)
        )
    """))
    logger.info("migration.create_table_if_not_exists", table="job_checkpoints")

    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_checkpoint_job_run_id"
        " ON job_checkpoints (job_run_id)"
    ))


def _migrate_v5(conn) -> None:
    """
    v5 迁移 — P3_01_ai_red_flags：
    1. ai_insights 表增加 financial_red_flags 列（JSON 数组存 AI 识别的财务红旗）
    2. init_db.py 文档头更新版本说明（见模块 docstring）
    """
    ai_columns = [
        ("financial_red_flags", "TEXT NOT NULL DEFAULT '[]'"),
    ]
    for col_name, col_def in ai_columns:
        try:
            conn.execute(text(
                f"ALTER TABLE ai_insights ADD COLUMN {col_name} {col_def}"
            ))
            logger.info("migration.add_column", table="ai_insights", column=col_name)
        except Exception as e:
            if "duplicate column name" in str(e).lower():
                logger.debug("migration.column_exists", table="ai_insights", column=col_name)
            else:
                raise


MIGRATIONS: list[tuple[int, str, Callable]] = [
    # v1 由 Base.metadata.create_all 处理，此处不重复
    (2, "P0: stocks 新增黑名单辅助列；blacklist / schema_migrations 表", _migrate_v2),
    (3, "P1_01_financial_deep_columns: 杜邦/盈余质量/分红历史", _migrate_v3),
    (4, "P2_01_job_checkpoints: job_runs 增断点续传字段；新建 job_checkpoints 表", _migrate_v4),
    (5, "P3_01_ai_red_flags: ai_insights 增 financial_red_flags 列", _migrate_v5),
]


# ──────────────────────────────────────────────────────────────────────────────
# 迁移执行器
# ──────────────────────────────────────────────────────────────────────────────

def _run_migrations() -> None:
    """执行所有未应用的迁移，幂等"""
    with engine.begin() as conn:
        # 先确保 schema_migrations 表存在（可能是首次运行）
        conn.execute(text(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version     INTEGER PRIMARY KEY,
                description TEXT    NOT NULL DEFAULT '',
                applied_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        ))

        # 获取已应用的最大版本
        row = conn.execute(text("SELECT COALESCE(MAX(version), 0) FROM schema_migrations")).fetchone()
        max_applied: int = row[0] if row else 0
        logger.info("migration.current_version", max_applied=max_applied)

        for version, description, ddl_fn in MIGRATIONS:
            if version <= max_applied:
                logger.debug("migration.skip", version=version)
                continue

            logger.info("migration.apply", version=version, description=description)
            try:
                ddl_fn(conn)
                conn.execute(
                    text(
                        "INSERT OR REPLACE INTO schema_migrations (version, description, applied_at)"
                        " VALUES (:v, :d, :t)"
                    ),
                    {"v": version, "d": description, "t": datetime.now().isoformat()},
                )
                logger.info("migration.done", version=version)
            except Exception as e:
                logger.error("migration.failed", version=version, error=str(e))
                raise


# ──────────────────────────────────────────────────────────────────────────────
# 主入口
# ──────────────────────────────────────────────────────────────────────────────

def init() -> None:
    logger.info("初始化数据库...")
    settings.ensure_dirs()

    # Step 1: 通过 ORM metadata 创建所有新表（幂等）
    logger.info("创建/确认所有表结构...")
    Base.metadata.create_all(bind=engine)
    logger.info("表结构确认完成", database=settings.database_url)

    # Step 2: 执行 schema 迁移（为存量 DB 补列）
    logger.info("执行 schema 迁移...")
    _run_migrations()
    logger.info("schema 迁移完成")

    # Step 3: 拉取股票基础信息（首次需要联网）
    logger.info("拉取股票基础信息（首次可能需要 2-5 分钟）...")
    try:
        from app.jobs.pipeline import Pipeline  # noqa: PLC0415
        pipeline = Pipeline()
        pipeline._update_stock_basic()
        logger.info("股票基础信息初始化完成")
    except Exception as e:
        logger.error("股票基础信息拉取失败", error=str(e))
        logger.info("提示：检查 TUSHARE_TOKEN 是否正确，以及网络连接")
        sys.exit(1)

    logger.info("初始化完成！现在可以运行 run.bat 启动服务")


if __name__ == "__main__":
    init()
