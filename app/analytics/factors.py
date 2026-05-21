"""
单因子计算 — PE扣非TTM, 安全边际, 增长率, 波动率, 股息率

P1 新增：
- 整合杜邦分析（dupont.py）
- 整合盈余质量（earnings_quality.py）
- 整合分红质量（dividend_quality.py）
- compute_all_factors 接受 dividends 参数，写入 FactorResult 新字段
"""
from __future__ import annotations

import numpy as np

from app.analytics.dividend_quality import (
    compute_continuity,
    detect_clearance_dividend,
    score_div_continuity,
)
from app.analytics.dupont import compute_dupont, score_dupont
from app.analytics.earnings_quality import compute_cfo_to_np_ratio, score_earnings_quality
from app.analytics.ttm import compute_growth_rate, compute_ttm_deduct_pe
from app.core.logging import get_logger
from app.domain.models import DividendRecord, FactorResult, QuarterRecord

logger = get_logger("factors")


def compute_volatility(pct_chg_series: list[float], window: int = 60) -> float | None:
    """
    计算近 N 日波动率（年化）

    Args:
        pct_chg_series: 日涨跌幅列表（%），按时间升序
        window: 窗口天数

    Returns:
        年化波动率 %，或 None（数据不足）
    """
    if len(pct_chg_series) < max(window // 2, 20):
        return None

    recent = pct_chg_series[-window:] if len(pct_chg_series) >= window else pct_chg_series
    arr = np.array(recent, dtype=float) / 100.0  # 转换为小数
    std = float(np.std(arr, ddof=1))
    annualized = std * np.sqrt(252) * 100  # 年化，转回 %
    return round(annualized, 2)


def compute_safety_margin(
    pe_deduct_ttm: float | None,
    pb: float | None,
    dv_ttm: float | None,
) -> float:
    """
    安全边际综合得分（绝对阈值版，0-100）

    这是"旧版绝对阈值健康度"的保留版本，供与新 z-score 评分对比验证
    """
    score = 50.0  # 基础分

    # PE 扣非评分（越低越好）
    if pe_deduct_ttm is not None and pe_deduct_ttm > 0:
        if pe_deduct_ttm < 8:
            score += 20
        elif pe_deduct_ttm < 12:
            score += 15
        elif pe_deduct_ttm < 15:
            score += 10
        elif pe_deduct_ttm < 20:
            score += 5
        else:
            score -= 10

    # PB 评分（越低越安全）
    if pb is not None and pb > 0:
        if pb < 1.0:
            score += 15
        elif pb < 1.5:
            score += 10
        elif pb < 2.0:
            score += 5
        elif pb > 3.0:
            score -= 5

    # 股息率评分（越高越好）
    if dv_ttm is not None:
        if dv_ttm >= 5.0:
            score += 15
        elif dv_ttm >= 3.0:
            score += 10
        elif dv_ttm >= 2.0:
            score += 5
        elif dv_ttm < 1.0:
            score -= 5

    return max(0.0, min(100.0, score))


def compute_all_factors(
    ts_code: str,
    trade_date,
    quarters: list[QuarterRecord],
    total_mv_wan: float | None,
    pe_ttm: float | None,
    pb: float | None,
    dv_ttm: float | None,
    pct_chg_series: list[float] | None = None,
    dividends: list[DividendRecord] | None = None,
) -> FactorResult:
    """
    计算单只股票所有原始因子（P1：含杜邦/盈余质量/分红连续性）

    Args:
        ts_code:         股票代码
        trade_date:      交易日期
        quarters:        季度财务记录列表
        total_mv_wan:    总市值（万元）
        pe_ttm:          市盈率 TTM（来自行情）
        pb:              市净率
        dv_ttm:          股息率 %
        pct_chg_series:  近 90+ 日涨跌幅列表（升序）
        dividends:       历史分红记录（P1 新增）

    Returns:
        FactorResult（原始因子 + P1 新增字段已填充，评分字段待 scoring.py 填充）
    """
    result = FactorResult(ts_code=ts_code, trade_date=trade_date)

    result.pe_ttm = pe_ttm
    result.pb = pb
    result.dv_ttm = dv_ttm
    result.total_mv = total_mv_wan

    # ── 扣非 PE TTM ──────────────────────────────────────────────────────
    if quarters and total_mv_wan and total_mv_wan > 0:
        result.pe_deduct_ttm = compute_ttm_deduct_pe(quarters, total_mv_wan)

    # ── 增长率 ────────────────────────────────────────────────────────────
    if quarters:
        result.growth_rate = compute_growth_rate(quarters)

    # ── 波动率 ────────────────────────────────────────────────────────────
    if pct_chg_series:
        result.volatility = compute_volatility(pct_chg_series)

    # ── 安全边际（绝对阈值版，保留对照）─────────────────────────────────
    result.safety_margin = compute_safety_margin(result.pe_deduct_ttm, pb, dv_ttm)

    # ── P1：杜邦分析 ──────────────────────────────────────────────────────
    if quarters:
        # 取最新一期季度数据做杜邦分解
        latest_q = sorted(quarters, key=lambda q: q.end_date, reverse=True)[0]
        dupont_result = compute_dupont(latest_q)
        result.dupont_score = score_dupont(dupont_result)
        result.high_leverage_flag = dupont_result.leverage_warning

        logger.debug(
            "factors.dupont",
            ts_code=ts_code,
            roe=dupont_result.roe,
            eqt_multiplier=dupont_result.eqt_multiplier,
            debt_to_assets=dupont_result.debt_to_assets,
            leverage_warning=dupont_result.leverage_warning,
            dupont_score=result.dupont_score,
        )

    # ── P1：盈余质量 ──────────────────────────────────────────────────────
    if quarters:
        cfo_ratio = compute_cfo_to_np_ratio(quarters)
        result.cfo_to_np_ratio = cfo_ratio
        result.earnings_quality_score = score_earnings_quality(cfo_ratio)

        logger.debug(
            "factors.earnings_quality",
            ts_code=ts_code,
            cfo_to_np=cfo_ratio,
            score=result.earnings_quality_score,
        )

    # ── P1：分红连续性 ────────────────────────────────────────────────────
    div_list = dividends or []
    current_year = trade_date.year if hasattr(trade_date, "year") else trade_date.year
    continuity = compute_continuity(div_list, current_year)
    result.dividend_continuity = continuity
    result.dividend_continuity_score = score_div_continuity(continuity)
    result.clearance_dividend_flag = detect_clearance_dividend(div_list)

    logger.debug(
        "factors.dividend_quality",
        ts_code=ts_code,
        continuity_years=continuity,
        continuity_score=result.dividend_continuity_score,
        clearance_flag=result.clearance_dividend_flag,
    )

    return result
