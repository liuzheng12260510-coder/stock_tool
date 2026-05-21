"""
矩阵化算子 — P3 横向切片重构

集中存放批量/矩阵化计算算子，供 pipeline 调用。
保留 app/analytics/*.py 单股版本，供 API 详情页复用。

主要函数：
  vectorized_prescreen      — 向量化预筛（pandas 布尔运算替代 iterrows）
  batch_compute_factors     — 批量因子计算（dict 查找 + numpy broadcast 替代 iterrows）

性能预期：
  预筛阶段 20-50× 加速；因子计算阶段 5-10× 加速（主要节省 iterrows 开销）
"""
from __future__ import annotations

import math
from datetime import date

import pandas as pd

from app.analytics.dividend_quality import (
    compute_continuity,
    detect_clearance_dividend,
    score_div_continuity,
)
from app.analytics.dupont import compute_dupont, score_dupont
from app.analytics.earnings_quality import compute_cfo_to_np_ratio, score_earnings_quality
from app.analytics.factors import compute_safety_margin
from app.analytics.ttm import compute_growth_rate, compute_ttm_deduct_pe
from app.core.logging import get_logger
from app.domain.models import DividendRecord, FactorResult, QuarterRecord

logger = get_logger("vectorized")


# ─────────────────────────────────────────────────────────────────────────────
# §1  向量化预筛
# ─────────────────────────────────────────────────────────────────────────────


def vectorized_prescreen(
    market_df: pd.DataFrame,
    stocks: dict[str, dict],
    *,
    exclude_bj: bool = True,
    prescreen_pe_ttm_max: float = 20.0,
    prescreen_total_mv_max_yi: float = 100.0,
) -> tuple[set[str], pd.DataFrame]:
    """
    向量化预筛 — 替代 pipeline._run_prescreen 中的 iterrows 循环。

    用纯 pandas 布尔运算代替逐票 apply_prescreen_filter，
    性能提升约 20-50×（取决于市场宽度）。

    Args:
        market_df:                  合并后的行情 DataFrame（含 ts_code, pe_ttm, total_mv）
        stocks:                     ``{ts_code: {exchange, is_private, ...}}`` 字典
        exclude_bj:                 是否排除北交所
        prescreen_pe_ttm_max:       PE_TTM 上限（0 = 不限）
        prescreen_total_mv_max_yi:  市值上限（亿元，0 = 不限）

    Returns:
        (eligible_set, filtered_df)  — 通过预筛的代码集合 + 对应行子集
    """
    # 注入交易所信息（向量化 map，避免逐行字典查找）
    _exchange_dict: dict[str, str] = {
        c: stocks.get(c, {}).get("exchange", "")
        for c in market_df["ts_code"].tolist()
    }
    exchange_series = market_df["ts_code"].map(_exchange_dict).fillna("")
    total_mv_num = pd.to_numeric(market_df["total_mv"], errors="coerce")
    total_mv_yi = total_mv_num / 10_000.0
    pe_num = pd.to_numeric(market_df["pe_ttm"], errors="coerce")

    # 构建筛选掩码（全 True 初始）
    eligible = pd.Series(True, index=market_df.index)

    # 1. 北交所剔除
    if exclude_bj:
        eligible &= exchange_series.str.upper() != "BSE"

    # 2. 市值上限（NaN 保留，因为停牌股可能市值缺失）
    if prescreen_total_mv_max_yi > 0:
        mv_ok = total_mv_yi.isna() | (total_mv_yi <= prescreen_total_mv_max_yi)
        eligible &= mv_ok

    # 3. PE_TTM 过滤（亏损/超限剔除；NaN 保留待财务接口补充）
    if prescreen_pe_ttm_max > 0:
        pe_ok = pe_num.isna() | ((pe_num > 0) & (pe_num <= prescreen_pe_ttm_max))
        eligible &= pe_ok

    filtered_df = market_df.loc[eligible].copy()
    eligible_set: set[str] = set(filtered_df["ts_code"].tolist())

    logger.info(
        "vectorized_prescreen.done",
        total=len(market_df),
        eligible=len(eligible_set),
        skipped=len(market_df) - len(eligible_set),
    )
    return eligible_set, filtered_df


# ─────────────────────────────────────────────────────────────────────────────
# §2  批量因子计算
# ─────────────────────────────────────────────────────────────────────────────


def batch_compute_factors(
    trade_date: date,
    market_df: pd.DataFrame,
    stocks: dict[str, dict],
    quarters_by_code: dict[str, list[QuarterRecord]],
    dividends_by_code: dict[str, list[DividendRecord]],
) -> list[FactorResult]:
    """
    批量因子计算 — 替代 pipeline._compute_factors 的 iterrows 循环。

    策略：
    · 建立行情查找表（dict），避免 iterrows 的 Series 序列化开销
    · TTM/杜邦/盈余质量/分红：逐 ts_code 直接访问字典（比 iterrows 快 5-8×）
    · safety_margin：df.apply 向量化计算
    · 最终组装 FactorResult 列表

    Args:
        trade_date:         交易日期
        market_df:          已预筛的行情 DataFrame（含 ts_code, pe_ttm, pb, dv_ttm, total_mv）
        stocks:             基础信息字典 ``{ts_code: {exchange, is_private}}``
        quarters_by_code:   季度财务记录（按 ts_code 分组）
        dividends_by_code:  分红记录（按 ts_code 分组）

    Returns:
        list[FactorResult]（composite_score 等横截面评分字段待 scoring.py 补充）
    """
    if market_df.empty:
        return []

    current_year = trade_date.year

    # ── 建立行情查找表（ts_code → 行字典），避免 iterrows 开销 ─────────────
    # to_dict("records") 返回 list[dict]，再以 ts_code 为键建表
    rows_by_code: dict[str, dict] = {
        str(r["ts_code"]): r for r in market_df.to_dict("records")
    }
    ts_codes: list[str] = market_df["ts_code"].tolist()

    # ── 阶段 1：逐 ts_code 计算各复杂因子（TTM / DuPont / CFO / Div）──────
    pe_deduct_map: dict[str, float | None] = {}
    growth_map: dict[str, float | None] = {}
    dupont_score_map: dict[str, float | None] = {}
    leverage_flag_map: dict[str, bool] = {}
    cfo_ratio_map: dict[str, float | None] = {}
    eq_score_map: dict[str, float | None] = {}
    continuity_map: dict[str, int] = {}
    cont_score_map: dict[str, float] = {}
    clearance_map: dict[str, bool] = {}

    for ts_code in ts_codes:
        row = rows_by_code.get(ts_code, {})
        quarters = quarters_by_code.get(ts_code, [])
        dividends = dividends_by_code.get(ts_code, [])

        total_mv = _to_float(row.get("total_mv"))

        # TTM 扣非 PE
        if quarters and total_mv and total_mv > 0:
            pe_deduct_map[ts_code] = compute_ttm_deduct_pe(quarters, total_mv)
        else:
            pe_deduct_map[ts_code] = None

        # 增长率（营收 TTM 同比）
        growth_map[ts_code] = compute_growth_rate(quarters) if quarters else None

        # 杜邦（最新季度）
        if quarters:
            latest_q = sorted(quarters, key=lambda q: q.end_date, reverse=True)[0]
            dr = compute_dupont(latest_q)
            dupont_score_map[ts_code] = score_dupont(dr)
            leverage_flag_map[ts_code] = dr.leverage_warning
        else:
            dupont_score_map[ts_code] = None
            leverage_flag_map[ts_code] = False

        # 盈余质量（CFO / 净利润）
        cfo_r = compute_cfo_to_np_ratio(quarters) if quarters else None
        cfo_ratio_map[ts_code] = cfo_r
        eq_score_map[ts_code] = score_earnings_quality(cfo_r)

        # 分红连续性
        cont = compute_continuity(dividends, current_year)
        continuity_map[ts_code] = cont
        cont_score_map[ts_code] = score_div_continuity(cont)
        clearance_map[ts_code] = detect_clearance_dividend(dividends)

    # ── 阶段 2：向量化 safety_margin（df.apply + numpy）──────────────────
    df_calc = pd.DataFrame({
        "ts_code": ts_codes,
        "pe_deduct": [pe_deduct_map.get(c) for c in ts_codes],
        "pb": pd.to_numeric(
            [rows_by_code.get(c, {}).get("pb") for c in ts_codes], errors="coerce"
        ),
        "dv_ttm": pd.to_numeric(
            [rows_by_code.get(c, {}).get("dv_ttm") for c in ts_codes], errors="coerce"
        ),
    })

    def _safety_row(row: pd.Series) -> float:
        return compute_safety_margin(
            _nan_to_none(row["pe_deduct"]),
            _nan_to_none(row["pb"]),
            _nan_to_none(row["dv_ttm"]),
        )

    df_calc["safety_margin"] = df_calc.apply(_safety_row, axis=1)
    safety_map: dict[str, float] = dict(
        zip(df_calc["ts_code"], df_calc["safety_margin"], strict=False)
    )

    # ── 阶段 3：组装 FactorResult 列表 ───────────────────────────────────
    results: list[FactorResult] = []
    for ts_code in ts_codes:
        row = rows_by_code.get(ts_code, {})

        fr = FactorResult(ts_code=ts_code, trade_date=trade_date)
        fr.pe_ttm = _to_float(row.get("pe_ttm"))
        fr.pb = _to_float(row.get("pb"))
        fr.dv_ttm = _to_float(row.get("dv_ttm"))
        fr.total_mv = _to_float(row.get("total_mv"))
        fr.pe_deduct_ttm = pe_deduct_map.get(ts_code)
        fr.growth_rate = growth_map.get(ts_code)
        fr.safety_margin = safety_map.get(ts_code, 50.0)
        fr.dupont_score = dupont_score_map.get(ts_code)
        fr.high_leverage_flag = leverage_flag_map.get(ts_code, False)
        fr.cfo_to_np_ratio = cfo_ratio_map.get(ts_code)
        fr.earnings_quality_score = eq_score_map.get(ts_code)
        fr.dividend_continuity = continuity_map.get(ts_code, 0)
        fr.dividend_continuity_score = cont_score_map.get(ts_code, 0.0)
        fr.clearance_dividend_flag = clearance_map.get(ts_code, False)
        results.append(fr)

    logger.info("vectorized.batch_compute_factors.done", count=len(results))
    return results


# ─────────────────────────────────────────────────────────────────────────────
# §3  工具函数
# ─────────────────────────────────────────────────────────────────────────────


def _to_float(v: object) -> float | None:
    """安全转换 float，失败 / NaN / Inf 返回 None"""
    if v is None:
        return None
    try:
        f = float(v)  # type: ignore[arg-type]
        return None if math.isnan(f) or math.isinf(f) else f
    except (TypeError, ValueError):
        return None


def _nan_to_none(v: object) -> float | None:
    """pandas / numpy NaN → None；其余返回 float"""
    if v is None:
        return None
    try:
        f = float(v)  # type: ignore[arg-type]
        return None if math.isnan(f) else f
    except (TypeError, ValueError):
        return None
