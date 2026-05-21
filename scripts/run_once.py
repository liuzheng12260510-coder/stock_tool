"""
命令行手动跑批（不依赖 Web 服务）
运行方式：python scripts/run_once.py [日期 YYYYMMDD] [--force]

示例：
  python scripts/run_once.py                   # 最近交易日
  python scripts/run_once.py 20250519          # 指定日期
  python scripts/run_once.py 20250519 --force  # 强制重跑
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.core.config import settings
from app.core.logging import configure_logging, get_logger

configure_logging(settings.logs_dir)
logger = get_logger("run_once")


def main() -> None:
    args = sys.argv[1:]
    trade_date = None
    force = "--force" in args

    date_args = [a for a in args if not a.startswith("--")]
    if date_args:
        try:
            trade_date = datetime.strptime(date_args[0], "%Y%m%d").date()
        except ValueError:
            print(f"日期格式错误: {date_args[0]}，请使用 YYYYMMDD 格式")
            sys.exit(1)

    logger.info("手动跑批开始", trade_date=str(trade_date) if trade_date else "最近交易日", force=force)

    from app.db.models import Base
    from app.db.session import engine
    Base.metadata.create_all(bind=engine)

    from app.jobs.pipeline import Pipeline
    pipeline = Pipeline()
    job_id = pipeline.run(trade_date=trade_date, force=force)
    logger.info("跑批完成", job_id=job_id)


if __name__ == "__main__":
    main()
