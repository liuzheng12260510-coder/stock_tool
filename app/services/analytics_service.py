"""
单股财务深度报告服务 — P3.2 新增

职责：
- 提供杜邦三因子时序数据
- 提供 CFO/NP 时序数据
- 提供实施分红历史
- 汇总财务红旗徽章
- 支持 /api/stocks/{ts_code}/financial-deep 端点
- 提供 AI Prompt 上下文构建（_build_dupont_context / _build_cfo_context / _build_dividend_context）
"""
from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from typing import Any

from sqlalchemy import select

from app.core.logging import get_logger
from app.db.models import DividendHistory, FactorScore, FinancialQuarter, Stock
from app.db.session import db_session

logger = get_logger("analytics_service")


def get_financial_deep_report(ts_code: str) -> dict:
    """
    单股财务深度报告

    汇总：
    · 杜邦三因子近 8 季时序（roe / npta / assets_turn / eqt_multiplier）
    · CFO/NP 近 8 季时序（n_cashflow_act / cfo_to_np）
    · 近 5 年实施分红历史（cash_div per share）
    · 财务红旗（latest_factor.high_leverage_flag 等）

    Args:
        ts_code: 股票代码，e.g. '600519.SH'

    Returns:
        dict，结构见下方 Returns 注释
    """
    with db_session() as db:
        # ── 1. 股票基本信息 ───────────────────────────────────────────
        stock = db.execute(
            select(Stock).where(Stock.ts_code == ts_code)
        ).scalar_one_or_none()
        stock_name = stock.name if stock else ts_code

        # ── 2. 季度财务时序（近 8 季，按报告期降序）─────────────────
        fq_rows = db.execute(
            select(FinancialQuarter)
            .where(FinancialQuarter.ts_code == ts_code)
            .order_by(FinancialQuarter.end_date.desc())
            .limit(8)
        ).scalars().all()

        dupont_timeseries = []
        cfo_np_timeseries = []
        for fq in reversed(fq_rows):  # 升序展示（时间从旧到新）
            period_str = fq.end_date.strftime("%Y-%m-%d")
            dupont_timeseries.append({
                "period": period_str,
                "roe": fq.roe,
                "npta": fq.npta,
                "assets_turn": fq.assets_turn,
                "eqt_multiplier": fq.eqt_multiplier,
                "debt_to_assets": fq.debt_to_assets,
            })
            cfo_np_timeseries.append({
                "period": period_str,
                "n_cashflow_act": fq.n_cashflow_act,
                "cfo_to_np": fq.cfo_to_np,
                "netprofit_yoy": fq.netprofit_yoy,
                "op_yoy": fq.op_yoy,
            })

        # ── 3. 实施分红历史（近 5 年，dispatch=实施）─────────────────
        dh_rows = db.execute(
            select(DividendHistory)
            .where(
                DividendHistory.ts_code == ts_code,
                DividendHistory.div_proc == "实施",
            )
            .order_by(DividendHistory.end_date.desc())
            .limit(5)
        ).scalars().all()

        dividend_history = []
        for dh in reversed(dh_rows):  # 升序
            dividend_history.append({
                "year": dh.end_date.year,
                "end_date": dh.end_date.strftime("%Y-%m-%d"),
                "cash_div": dh.cash_div,
                "cash_div_tax": dh.cash_div_tax,
                "stk_div": dh.stk_div,
                "pay_date": dh.pay_date.strftime("%Y-%m-%d") if dh.pay_date else None,
            })

        # ── 4. 最新因子分（来自 factor_scores 最近一日）──────────────
        latest_score = db.execute(
            select(FactorScore)
            .where(FactorScore.ts_code == ts_code)
            .order_by(FactorScore.trade_date.desc())
            .limit(1)
        ).scalar_one_or_none()

        latest_factor: dict = {}
        if latest_score:
            latest_factor = {
                "trade_date": str(latest_score.trade_date),
                "dupont_score": latest_score.dupont_score,
                "high_leverage_flag": bool(latest_score.high_leverage_flag),
                "earnings_quality_score": latest_score.earnings_quality_score,
                "dividend_continuity": latest_score.dividend_continuity,
                "dividend_continuity_score": latest_score.dividend_continuity_score,
                "composite_score": latest_score.composite_score,
                "composite_rank": latest_score.composite_rank,
                "passed_screening": latest_score.passed_screening,
            }

    # ── 5. 计算财务红旗概要 ──────────────────────────────────────────
    red_flags: list[str] = _compute_red_flags(
        latest_score=latest_score,
        fq_rows=fq_rows,
        dh_rows=dh_rows,
    )

    logger.info(
        "analytics_service.financial_deep.done",
        ts_code=ts_code,
        fq_count=len(fq_rows),
        div_count=len(dh_rows),
        red_flags=red_flags,
    )

    return {
        "ts_code": ts_code,
        "name": stock_name,
        "dupont_timeseries": dupont_timeseries,
        "cfo_np_timeseries": cfo_np_timeseries,
        "dividend_history": dividend_history,
        "latest_factor": latest_factor,
        "red_flags": red_flags,
    }


def _compute_red_flags(
    latest_score: FactorScore | None,
    fq_rows: Sequence[Any],
    dh_rows: Sequence[Any],
) -> list[str]:
    """综合识别财务红旗，返回红旗描述列表"""
    flags: list[str] = []

    if latest_score is None:
        return flags

    # 高杠杆红旗
    if latest_score.high_leverage_flag:
        flags.append("高杠杆：权益乘数>3 或资产负债率>70%，ROE 含水分")

    # 低盈余质量
    if (
        latest_score.earnings_quality_score is not None
        and latest_score.earnings_quality_score < 30
    ):
        flags.append(f"低盈余质量：CFO/NP 评分={latest_score.earnings_quality_score:.0f}/100，现金流质量不足")

    # 连续分红年数不足
    if (
        latest_score.dividend_continuity is not None
        and latest_score.dividend_continuity < 3
    ):
        flags.append(f"分红连续性弱：仅连续分红 {latest_score.dividend_continuity} 年")

    # 近两季 CFO 转负（盈余恶化）
    recent_cfo = [fq.cfo_to_np for fq in fq_rows[:2] if fq.cfo_to_np is not None]
    if recent_cfo and all(v < 0 for v in recent_cfo):
        flags.append("连续季度 CFO/NP 为负，盈余质量持续恶化")

    # 营收同比连续下滑
    recent_yoy = [fq.op_yoy for fq in fq_rows[:2] if fq.op_yoy is not None]
    if len(recent_yoy) == 2 and all(v < 0 for v in recent_yoy):
        flags.append("营收同比连续两季下滑")

    return flags


def get_dupont_industry_percentile(
    ts_code: str, trade_date: date | None = None
) -> dict:
    """
    计算该股票杜邦指标在同行业的百分位（横截面）

    Args:
        ts_code:     股票代码
        trade_date:  参考日期，None = 取最新季报

    Returns:
        dict, 含 roe_pct / npta_pct / eqt_multiplier_pct 百分位（0-100）
    """
    import numpy as np

    with db_session() as db:
        # 取目标股票最新季报
        target_fq = db.execute(
            select(FinancialQuarter)
            .where(FinancialQuarter.ts_code == ts_code)
            .order_by(FinancialQuarter.end_date.desc())
            .limit(1)
        ).scalar_one_or_none()

        if target_fq is None:
            return {}

        # 同行业股票（via Stock.industry）
        target_stock = db.execute(
            select(Stock).where(Stock.ts_code == ts_code)
        ).scalar_one_or_none()
        industry = target_stock.industry if target_stock else ""

        if not industry:
            return {}

        # 取该行业所有股票在相同报告期的数据
        peer_ts_codes = db.execute(
            select(Stock.ts_code).where(Stock.industry == industry)
        ).scalars().all()

        peer_fqs = db.execute(
            select(FinancialQuarter)
            .where(
                FinancialQuarter.ts_code.in_(peer_ts_codes),
                FinancialQuarter.end_date == target_fq.end_date,
            )
        ).scalars().all()

    def _percentile(values: list[float | None], target: float | None) -> float | None:
        if target is None:
            return None
        valid = [v for v in values if v is not None]
        if len(valid) < 2:  # noqa: PLR2004
            return None
        arr = np.array(valid, dtype=float)
        rank = float(np.sum(arr <= target)) / len(arr) * 100
        return round(rank, 1)

    peer_roes = [fq.roe for fq in peer_fqs]
    peer_nptas = [fq.npta for fq in peer_fqs]
    peer_eqt = [fq.eqt_multiplier for fq in peer_fqs]

    return {
        "period": str(target_fq.end_date),
        "industry": industry,
        "peer_count": len(peer_fqs),
        "roe_pct": _percentile(peer_roes, target_fq.roe),
        "npta_pct": _percentile(peer_nptas, target_fq.npta),
        "eqt_multiplier_pct": _percentile(peer_eqt, target_fq.eqt_multiplier),
    }


# ─────────────────────────────────────────────────────────────────────────────
# AI Prompt Context Builders
# 供 app/ai/service.py 注入杜邦/CFO/分红摘要到 LLM Prompt
# 也供 tests/ 独立测试
# ─────────────────────────────────────────────────────────────────────────────

def _attr(obj: Any, key: str, default: Any = None) -> Any:
    """从 ORM 对象或 dict 中取属性值（duck typing，测试友好）"""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _build_dupont_context(fq_rows: Sequence[Any]) -> str:
    """
    构建杜邦三因子文字摘要，供 AI Prompt 注入。

    Args:
        fq_rows: FinancialQuarter ORM 对象序列（或同结构 dict 序列，测试用）

    Returns:
        多行文字摘要字符串，空列表时返回 "N/A"
    """
    if not fq_rows:
        return "N/A（无季报数据）"

    rows_sorted = sorted(
        fq_rows,
        key=lambda r: str(_attr(r, "end_date", "") or ""),
        reverse=True,
    )[:4]  # 最近 4 季

    lines = ["【杜邦三因子近4季趋势】"]
    for r in reversed(rows_sorted):
        period = str(_attr(r, "end_date", ""))[:10]
        roe = _attr(r, "roe")
        npta = _attr(r, "npta")
        eqt = _attr(r, "eqt_multiplier")
        lines.append(
            f"  {period}: ROE={roe or 'N/A'}%, 净利率={npta or 'N/A'}%, 权益乘数={eqt or 'N/A'}x"
        )
    return "\n".join(lines)


def _build_cfo_context(fq_rows: Sequence[Any]) -> str:
    """
    构建 CFO/NP 趋势文字摘要，供 AI Prompt 注入。

    Args:
        fq_rows: FinancialQuarter ORM 对象序列（或同结构 dict 序列，测试用）

    Returns:
        多行文字摘要字符串，空列表时返回 "N/A"
    """
    if not fq_rows:
        return "N/A（无季报数据）"

    rows_sorted = sorted(
        fq_rows,
        key=lambda r: str(_attr(r, "end_date", "") or ""),
        reverse=True,
    )[:4]

    lines = ["【CFO/NP 近4季趋势】"]
    for r in reversed(rows_sorted):
        period = str(_attr(r, "end_date", ""))[:10]
        cfo_to_np = _attr(r, "cfo_to_np")
        n_cfo = _attr(r, "n_cashflow_act")
        lines.append(
            f"  {period}: CFO/NP={cfo_to_np or 'N/A'}, 经营现金流={n_cfo or 'N/A'}万"
        )
    return "\n".join(lines)


def _build_dividend_context(dh_rows: Sequence[Any]) -> str:
    """
    构建分红历史文字摘要，供 AI Prompt 注入。

    Args:
        dh_rows: DividendHistory ORM 对象序列（或同结构 dict 序列，测试用）

    Returns:
        多行文字摘要字符串，空列表时返回 "N/A"
    """
    if not dh_rows:
        return "N/A（无分红历史）"

    rows_sorted = sorted(
        dh_rows,
        key=lambda r: str(_attr(r, "end_date", "") or ""),
        reverse=True,
    )[:5]

    lines = ["【近5年分红记录】"]
    for r in reversed(rows_sorted):
        end_date = _attr(r, "end_date", "")
        year = end_date.year if hasattr(end_date, "year") else str(end_date)[:4]
        cash_div = _attr(r, "cash_div")
        lines.append(f"  {year}年: 每股分红={cash_div or 'N/A'}元")
    return "\n".join(lines)


def _detect_red_flags(
    latest_factor: dict,
    recent_quarters: Sequence[Any],
) -> list[str]:
    """
    基于 latest_factor dict + 近几季 CFO 趋势，识别财务红旗。
    供 AI Prompt 中的财务健康检查，也供 tests/ 单独测试。

    Args:
        latest_factor:    由 get_financial_deep_report 组装的 latest_factor dict
        recent_quarters:  近几季 FinancialQuarter ORM 或 dict 序列

    Returns:
        红旗描述字符串列表，无红旗时为空列表
    """
    flags: list[str] = []

    if not latest_factor:
        return flags

    # 高杠杆红旗
    if latest_factor.get("high_leverage_flag"):
        flags.append("高杠杆：权益乘数>3 或资产负债率>70%，ROE 含水分")

    # 低盈余质量（评分 < 30）
    eq_score = latest_factor.get("earnings_quality_score")
    if eq_score is not None and eq_score < 30:
        flags.append(f"低盈余质量：CFO/NP 评分={eq_score:.0f}/100，现金流质量不足")

    # 分红连续性弱
    dc = latest_factor.get("dividend_continuity")
    if dc is not None and dc < 3:
        flags.append(f"分红连续性弱：仅连续分红 {dc} 年")

    # 近几季 CFO/NP 趋势
    recent_cfo = [
        _attr(r, "cfo_to_np")
        for r in list(recent_quarters)[:2]
        if _attr(r, "cfo_to_np") is not None
    ]
    if recent_cfo and all(v < 0 for v in recent_cfo):
        flags.append("连续季度 CFO/NP 为负，盈余质量持续恶化")

    return flags
