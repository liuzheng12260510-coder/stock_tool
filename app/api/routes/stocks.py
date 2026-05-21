"""
股票相关 API 路由
"""
from __future__ import annotations

from datetime import date
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query

from app.core.logging import get_logger
from app.services.screening import (
    get_industry_distribution,
    get_screened_stocks,
    get_stock_detail,
    get_stock_history,
)

logger = get_logger("stocks_router")
router = APIRouter(prefix="/api/stocks", tags=["stocks"])


@router.get("")
def list_screened_stocks(
    trade_date: Optional[str] = Query(None, description="交易日 YYYY-MM-DD，默认最近"),
    min_score: float = Query(0.0, description="最低综合评分"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
) -> dict[str, Any]:
    """获取筛选结果列表"""
    parsed_date = None
    if trade_date:
        try:
            parsed_date = date.fromisoformat(trade_date)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"日期格式错误: {trade_date}")

    snapshots, total, actual_date = get_screened_stocks(
        trade_date=parsed_date,
        min_score=min_score,
        page=page,
        page_size=page_size,
    )

    return {
        "trade_date": str(actual_date),
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": [_snapshot_to_dict(s) for s in snapshots],
    }


@router.get("/industry-distribution")
def industry_distribution(
    trade_date: Optional[str] = Query(None),
) -> list[dict]:
    """获取行业分布（用于饼图）"""
    parsed_date = None
    if trade_date:
        try:
            parsed_date = date.fromisoformat(trade_date)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"日期格式错误: {trade_date}")

    return get_industry_distribution(parsed_date)


@router.get("/{ts_code}")
def get_stock(ts_code: str) -> dict[str, Any]:
    """获取单股最新快照"""
    snapshot = get_stock_detail(ts_code)
    if not snapshot:
        raise HTTPException(status_code=404, detail=f"股票 {ts_code} 无行情数据")
    return _snapshot_to_dict(snapshot)


@router.get("/{ts_code}/history")
def get_history(
    ts_code: str,
    days: int = Query(90, ge=7, le=365),
) -> list[dict]:
    """获取单股历史评分趋势"""
    return get_stock_history(ts_code, days=days)


def _snapshot_to_dict(s) -> dict[str, Any]:
    return {
        "ts_code": s.ts_code,
        "name": s.name,
        "industry": s.industry,
        "trade_date": str(s.trade_date),
        "close": s.close,
        "pct_chg": s.pct_chg,
        "pe_ttm": s.pe_ttm,
        "pe_deduct_ttm": s.pe_deduct_ttm,
        "pb": s.pb,
        "dv_ttm": s.dv_ttm,
        "total_mv_yi": s.total_mv_yi,
        "composite_score": s.composite_score,
        "composite_rank": s.composite_rank,
        "passed_screening": s.passed_screening,
    }
