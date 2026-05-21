"""
Web 页面路由 — Jinja2 渲染
"""
from __future__ import annotations

import contextlib
from datetime import date
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

web_router = APIRouter(tags=["web"])
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


@web_router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, trade_date: str | None = None) -> HTMLResponse:
    """主看板 — 当日筛选结果"""
    from app.services.screening import get_industry_distribution, get_screened_stocks

    parsed_date = None
    if trade_date:
        with contextlib.suppress(ValueError):
            parsed_date = date.fromisoformat(trade_date)

    snapshots, total, actual_date = get_screened_stocks(
        trade_date=parsed_date, min_score=0, page=1, page_size=200
    )
    industry_dist = get_industry_distribution(actual_date)

    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "snapshots": snapshots,
            "total": total,
            "trade_date": str(actual_date),
            "industry_dist": industry_dist,
        },
    )


@web_router.get("/stock/{ts_code}", response_class=HTMLResponse)
def stock_detail(request: Request, ts_code: str) -> HTMLResponse:
    """单股详情页"""
    from app.services.screening import get_stock_detail, get_stock_history

    snapshot = get_stock_detail(ts_code)
    history = get_stock_history(ts_code, days=90)

    return templates.TemplateResponse(
        request=request,
        name="stock_detail.html",
        context={
            "snapshot": snapshot,
            "ts_code": ts_code,
            "history": history,
        },
    )


@web_router.get("/jobs", response_class=HTMLResponse)
def jobs_page(request: Request) -> HTMLResponse:
    """跑批历史页"""
    from app.db.models import JobRun
    from app.db.session import db_session

    with db_session() as db:
        runs = db.query(JobRun).order_by(JobRun.started_at.desc()).limit(50).all()
        run_list = [
            {
                "id": r.id,
                "started_at": str(r.started_at)[:19] if r.started_at else "-",
                "finished_at": str(r.finished_at)[:19] if r.finished_at else "-",
                "status": r.status,
                "error": (r.error[:100] + "...") if r.error and len(r.error) > 100 else (r.error or ""),
                "stocks_screened": r.stocks_screened,
                "stocks_passed": r.stocks_passed,
                "duration_s": round(r.duration_ms / 1000, 1) if r.duration_ms else None,
                "trade_date": str(r.trade_date) if r.trade_date else "-",
            }
            for r in runs
        ]

    return templates.TemplateResponse(
        request=request,
        name="jobs.html",
        context={
            "runs": run_list,
        },
    )
