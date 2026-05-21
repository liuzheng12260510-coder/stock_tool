"""
跑批任务 API 路由
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException

from app.core.logging import get_logger
from app.db.models import JobRun
from app.db.session import db_session

logger = get_logger("jobs_router")
router = APIRouter(prefix="/api/jobs", tags=["jobs"])

# 防止并发触发跑批
_pipeline_running = False


@router.get("/runs")
def list_job_runs(limit: int = 20) -> list[dict[str, Any]]:
    """获取最近跑批记录"""
    with db_session() as db:
        runs = (
            db.query(JobRun)
            .order_by(JobRun.started_at.desc())
            .limit(limit)
            .all()
        )
        return [
            {
                "id": r.id,
                "started_at": str(r.started_at) if r.started_at else None,
                "finished_at": str(r.finished_at) if r.finished_at else None,
                "status": r.status,
                "error": r.error[:200] if r.error else "",
                "stocks_screened": r.stocks_screened,
                "stocks_passed": r.stocks_passed,
                "duration_ms": r.duration_ms,
                "trade_date": str(r.trade_date) if r.trade_date else None,
            }
            for r in runs
        ]


@router.post("/run")
def trigger_run(background_tasks: BackgroundTasks) -> dict[str, str]:
    """
    手动触发跑批（后台异步执行）

    触发后立即返回 202，实际进度通过 GET /api/jobs/runs 查看
    """
    global _pipeline_running
    if _pipeline_running:
        raise HTTPException(status_code=409, detail="跑批任务正在进行中，请稍后再试")

    background_tasks.add_task(_run_pipeline_bg)
    return {"status": "triggered", "message": "跑批任务已启动，请通过 /api/jobs/runs 查看进度"}


@router.get("/status")
def pipeline_status() -> dict[str, Any]:
    """获取当前跑批状态"""
    with db_session() as db:
        latest = db.query(JobRun).order_by(JobRun.started_at.desc()).first()
        if not latest:
            return {"status": "no_runs", "running": _pipeline_running}
        return {
            "running": _pipeline_running,
            "latest": {
                "id": latest.id,
                "started_at": str(latest.started_at),
                "status": latest.status,
                "trade_date": str(latest.trade_date) if latest.trade_date else None,
                "stocks_passed": latest.stocks_passed,
                "duration_ms": latest.duration_ms,
            },
        }


def _run_pipeline_bg() -> None:
    """后台跑批任务"""
    global _pipeline_running
    _pipeline_running = True
    try:
        from app.jobs.scheduler import trigger_now
        trigger_now()
    except Exception as e:
        logger.exception("后台跑批失败", error=str(e))
    finally:
        _pipeline_running = False
