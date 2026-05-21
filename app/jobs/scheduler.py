"""
APScheduler 配置 — 每交易日 15:30 自动触发跑批
"""
from __future__ import annotations

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger("scheduler")

_scheduler: BackgroundScheduler | None = None


def get_scheduler() -> BackgroundScheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = BackgroundScheduler(timezone="Asia/Shanghai")
    return _scheduler


def start_scheduler() -> None:
    """启动后台调度器"""
    if not settings.scheduler_enabled:
        logger.info("调度器已禁用（SCHEDULER_ENABLED=false）")
        return

    scheduler = get_scheduler()
    if scheduler.running:
        logger.info("调度器已运行中")
        return

    # 每个工作日（周一到周五）的设定时间触发
    scheduler.add_job(
        _run_pipeline_job,
        trigger=CronTrigger(
            day_of_week="mon-fri",
            hour=settings.scheduler_cron_hour,
            minute=settings.scheduler_cron_minute,
            timezone="Asia/Shanghai",
        ),
        id="daily_pipeline",
        name="每日行情跑批",
        replace_existing=True,
        misfire_grace_time=3600,  # 允许最多延误 1 小时
    )

    scheduler.start()
    logger.info(
        "调度器已启动",
        cron=f"Mon-Fri {settings.scheduler_cron_hour:02d}:{settings.scheduler_cron_minute:02d}",
    )


def stop_scheduler() -> None:
    """停止调度器"""
    scheduler = get_scheduler()
    if scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("调度器已停止")


def _run_pipeline_job() -> None:
    """调度器触发的跑批任务（捕获所有异常，防止调度器崩溃）"""
    from app.jobs.pipeline import Pipeline  # 延迟导入避免循环

    logger.info("调度器触发跑批")
    try:
        pipeline = Pipeline()
        job_id = pipeline.run()
        logger.info("调度器跑批完成", job_id=job_id)
    except Exception as e:
        logger.exception("调度器跑批失败", error=str(e))


def trigger_now() -> int:
    """
    手动触发一次跑批（用于 API /api/jobs/run）

    Returns:
        job_run id
    """
    from app.jobs.pipeline import Pipeline  # 延迟导入

    pipeline = Pipeline()
    return pipeline.run()
