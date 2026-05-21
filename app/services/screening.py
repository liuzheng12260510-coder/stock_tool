"""
筛选服务 — 聚合查询 DB，返回筛选结果列表
"""
from __future__ import annotations

from datetime import date

import json

from app.core.logging import get_logger
from app.data.trade_calendar import get_latest_trade_date
from app.db.models import DailySnapshot, FactorScore, Stock
from app.db.session import db_session
from app.domain.models import StockSnapshot

logger = get_logger("screening_service")


def get_screened_stocks(
    trade_date: date | None = None,
    min_score: float = 0.0,
    page: int = 1,
    page_size: int = 100,
) -> tuple[list[StockSnapshot], int, date]:
    """
    获取指定日期的筛选结果

    Args:
        trade_date:  交易日，None = 最近交易日
        min_score:   最低综合分过滤
        page:        页码（从 1 开始）
        page_size:   每页条数

    Returns:
        (snapshots, total_count, actual_trade_date)
    """
    if trade_date is None:
        trade_date = _resolve_latest_date()

    with db_session() as db:
        query = (
            db.query(FactorScore, DailySnapshot, Stock)
            .join(
                DailySnapshot,
                (DailySnapshot.ts_code == FactorScore.ts_code)
                & (DailySnapshot.trade_date == FactorScore.trade_date),
                isouter=True,
            )
            .join(Stock, Stock.ts_code == FactorScore.ts_code, isouter=True)
            .filter(
                FactorScore.trade_date == trade_date,
                FactorScore.passed_screening.is_(True),
            )
        )

        if min_score > 0:
            query = query.filter(FactorScore.composite_score >= min_score)

        total_count = query.count()

        rows = (
            query.order_by(FactorScore.composite_score.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
            .all()
        )

        snapshots = []
        for fs, snap, stock in rows:
            total_mv_yi = None
            if snap and snap.total_mv:
                total_mv_yi = snap.total_mv / 10_000.0

            # T2：dv_ttm 优先用 FactorScore（直接来自 daily_basic），
            #     fallback 到 DailySnapshot（两者应一致，但 FactorScore 更可靠）
            dv_ttm_val = fs.dv_ttm
            if dv_ttm_val is None and snap:
                dv_ttm_val = snap.dv_ttm

            snapshots.append(StockSnapshot(
                ts_code=fs.ts_code,
                name=stock.name if stock else fs.ts_code,
                trade_date=trade_date,
                close=snap.close if snap else None,
                pct_chg=snap.pct_chg if snap else None,
                pe_ttm=snap.pe_ttm if snap else None,
                pe_deduct_ttm=fs.pe_deduct_ttm,
                pb=snap.pb if snap else None,
                dv_ttm=dv_ttm_val,
                total_mv_yi=total_mv_yi,
                composite_score=fs.composite_score,
                composite_rank=fs.composite_rank,
                passed_screening=True,
                industry=stock.industry if stock else "",
                dupont_score=fs.dupont_score,
                dividend_continuity=fs.dividend_continuity,
                high_leverage_flag=bool(fs.high_leverage_flag) if fs.high_leverage_flag is not None else False,
                earnings_quality_score=fs.earnings_quality_score,
                # T5 新增：5 维分项分 + fail_reasons
                value_score=fs.value_score,
                growth_score=fs.growth_score,
                stability_score=fs.stability_score,
                dividend_score=fs.dividend_score,
                safety_score=fs.safety_score,
                fail_reasons=json.loads(fs.fail_reasons) if fs.fail_reasons else [],
            ))

    return snapshots, total_count, trade_date


def get_stock_detail(ts_code: str) -> StockSnapshot | None:
    """获取单股最新快照"""
    with db_session() as db:
        stock = db.query(Stock).filter(Stock.ts_code == ts_code).first()

        snap = (
            db.query(DailySnapshot)
            .filter(DailySnapshot.ts_code == ts_code)
            .order_by(DailySnapshot.trade_date.desc())
            .first()
        )

        score = (
            db.query(FactorScore)
            .filter(FactorScore.ts_code == ts_code)
            .order_by(FactorScore.trade_date.desc())
            .first()
        )

        if not snap:
            return None

        total_mv_yi = None
        if snap.total_mv:
            total_mv_yi = snap.total_mv / 10000.0

        return StockSnapshot(
            ts_code=ts_code,
            name=stock.name if stock else ts_code,
            trade_date=snap.trade_date,
            close=snap.close,
            pct_chg=snap.pct_chg,
            pe_ttm=snap.pe_ttm,
            pe_deduct_ttm=score.pe_deduct_ttm if score else None,
            pb=snap.pb,
            dv_ttm=snap.dv_ttm,
            total_mv_yi=total_mv_yi,
            composite_score=score.composite_score if score else None,
            composite_rank=score.composite_rank if score else None,
            passed_screening=score.passed_screening if score else False,
            industry=stock.industry if stock else "",
        )


def get_stock_history(ts_code: str, days: int = 90) -> list[dict]:
    """获取单股历史评分趋势"""
    from datetime import timedelta

    cutoff = date.today() - timedelta(days=days)

    with db_session() as db:
        scores = (
            db.query(FactorScore, DailySnapshot)
            .join(
                DailySnapshot,
                (DailySnapshot.ts_code == FactorScore.ts_code)
                & (DailySnapshot.trade_date == FactorScore.trade_date),
                isouter=True,
            )
            .filter(
                FactorScore.ts_code == ts_code,
                FactorScore.trade_date >= cutoff,
            )
            .order_by(FactorScore.trade_date.asc())
            .all()
        )

        return [
            {
                "date": str(fs.trade_date),
                "composite_score": fs.composite_score,
                "composite_rank": fs.composite_rank,
                "close": snap.close if snap else None,
                "pe_deduct_ttm": fs.pe_deduct_ttm,
                "dv_ttm": snap.dv_ttm if snap else None,
            }
            for fs, snap in scores
        ]


def get_industry_distribution(trade_date: date | None = None) -> list[dict]:
    """获取通过筛选的股票行业分布"""
    if trade_date is None:
        trade_date = _resolve_latest_date()

    with db_session() as db:
        from sqlalchemy import func
        rows = (
            db.query(Stock.industry, func.count(Stock.ts_code))
            .join(
                FactorScore,
                (FactorScore.ts_code == Stock.ts_code)
                & (FactorScore.trade_date == trade_date)
                & (FactorScore.passed_screening.is_(True)),
            )
            .group_by(Stock.industry)
            .order_by(func.count(Stock.ts_code).desc())
            .limit(15)
            .all()
        )
        return [{"industry": r[0] or "其他", "count": r[1]} for r in rows]


def _resolve_latest_date() -> date:
    """从 DB 获取最近有数据的交易日"""
    with db_session() as db:
        row = db.query(FactorScore.trade_date).order_by(
            FactorScore.trade_date.desc()
        ).first()
        if row:
            return row[0]
    return get_latest_trade_date()
