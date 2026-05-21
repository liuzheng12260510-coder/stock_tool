"""
命令行手动跑批（不依赖 Web 服务）
运行方式：python scripts/run_once.py [日期 YYYYMMDD] [--force] [--resume JOB_ID]

示例：
  python scripts/run_once.py                      # 最近交易日
  python scripts/run_once.py 20250519              # 指定日期
  python scripts/run_once.py 20250519 --force      # 强制重跑（忽略 checkpoint，创建新 job）
  python scripts/run_once.py --resume 42           # 恢复指定 job_run（断点续传）
  python scripts/run_once.py 20250519 --resume 42  # 恢复指定 job，明确交易日
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
    resume_job_id: int | None = None

    # 解析 --resume <job_id>
    if "--resume" in args:
        idx = args.index("--resume")
        if idx + 1 >= len(args):
            print("错误：--resume 需要提供 job_id 参数，例如：--resume 42")
            sys.exit(1)
        try:
            resume_job_id = int(args[idx + 1])
        except ValueError:
            print(f"错误：--resume 参数 '{args[idx + 1]}' 不是有效的整数 job_id")
            sys.exit(1)

    # 解析日期（非 -- 开头的位置参数，且不是 --resume 的值）
    skip_next = False
    for a in args:
        if skip_next:
            skip_next = False
            continue
        if a == "--resume":
            skip_next = True
            continue
        if not a.startswith("--"):
            try:
                trade_date = datetime.strptime(a, "%Y%m%d").date()
            except ValueError:
                print(f"日期格式错误: {a}，请使用 YYYYMMDD 格式")
                sys.exit(1)
            break

    logger.info(
        "手动跑批开始",
        trade_date=str(trade_date) if trade_date else "最近交易日",
        force=force,
        resume_job_id=resume_job_id,
    )

    from app.db.models import Base
    from app.db.session import engine
    Base.metadata.create_all(bind=engine)

    from app.jobs.pipeline import Pipeline
    pipeline = Pipeline()
    job_id = pipeline.run(
        trade_date=trade_date,
        force=force,
        resume_job_id=resume_job_id,
    )
    logger.info("跑批完成", job_id=job_id)


if __name__ == "__main__":
    main()
