"""
股票相关 API 路由（P3 增强）

P3 变更：
- 新增 GET /api/stocks/{ts_code}/financial-deep：杜邦时序 + CFO/NP + 分红历史
- _snapshot_to_dict 增加 P3 因子详情列（dupont_score / dividend_continuity / flags）
"""
from __future__ import annotations

from datetime import date
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.core.logging import get_logger
from app.services.analytics_service import (
    get_dupont_industry_percentile,
    get_financial_deep_report,
)
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
    trade_date: str | None = Query(None, description="交易日 YYYY-MM-DD，默认最近"),
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
            raise HTTPException(status_code=400, detail=f"日期格式错误: {trade_date}") from None

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
    trade_date: str | None = Query(None),
) -> list[dict]:
    """获取行业分布（用于饼图）"""
    parsed_date = None
    if trade_date:
        try:
            parsed_date = date.fromisoformat(trade_date)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"日期格式错误: {trade_date}") from None

    return get_industry_distribution(parsed_date)


@router.get("/{ts_code}/financial-deep")
def get_financial_deep(ts_code: str) -> dict[str, Any]:
    """
    单股财务深度报告（P3 新增）

    返回：
    · dupont_timeseries:  杜邦三因子近 8 季时序
    · cfo_np_timeseries:  CFO/NP 近 8 季时序
    · dividend_history:   近 5 年实施分红历史
    · latest_factor:      最新因子评分摘要
    · red_flags:          规则引擎识别的财务红旗列表
    · dupont_percentile:  同行业杜邦百分位

    响应示例见 `/api/docs`
    """
    try:
        report = get_financial_deep_report(ts_code)
    except Exception as e:
        logger.error("financial_deep.error", ts_code=ts_code, error=str(e))
        raise HTTPException(status_code=500, detail=f"财务数据获取失败: {e!s}") from e

    # 补充行业百分位（单独查询，失败不影响主报告）
    try:
        percentile = get_dupont_industry_percentile(ts_code)
    except Exception:
        percentile = {}

    report["dupont_percentile"] = percentile
    return report


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


def _snapshot_to_dict(s: Any) -> dict[str, Any]:
    """StockSnapshot → JSON 字典（P3：补充因子详情列）"""
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
        # ── P3 新增字段 ──────────────────────────────────────────────
        "dupont_score": s.dupont_score,
        "dividend_continuity": s.dividend_continuity,
        "high_leverage_flag": s.high_leverage_flag,
        "earnings_quality_score": s.earnings_quality_score,
    }
