"""
初始化数据库 + 拉取股票基础信息
运行方式：python scripts/init_db.py
"""
from __future__ import annotations

import sys
from pathlib import Path

# 确保项目根目录在 sys.path
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.core.config import settings
from app.core.logging import configure_logging, get_logger
from app.db.models import Base
from app.db.session import engine

configure_logging(settings.logs_dir)
logger = get_logger("init_db")


def init() -> None:
    logger.info("创建数据库表...")
    settings.ensure_dirs()
    Base.metadata.create_all(bind=engine)
    logger.info("数据库表创建完成", database=settings.database_url)

    logger.info("拉取股票基础信息（首次可能需要 2-5 分钟）...")
    try:
        from app.jobs.pipeline import Pipeline
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
