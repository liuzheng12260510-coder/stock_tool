"""
盈余质量分析模块

核心指标：CFO / Net Profit（经营现金流 / 净利润）
- 比值 >= 0.8：现金流质量优秀，满分
- 比值 0.3~0.8：线性递减
- 比值 < 0.3：盈余质量不及格

数据来源：QuarterRecord.n_cashflow_act（经营现金流）+ 近 4 季利润估算
若 cfo_to_np 字段直接从 Tushare 获取且有效，则优先使用该值。
"""
from __future__ import annotations

from app.core.logging import get_logger
from app.domain.models import QuarterRecord

logger = get_logger("earnings_quality")

# 评分阈值
_CFO_TO_NP_EXCELLENT = 0.8   # >= 此值：100 分
_CFO_TO_NP_PASSING   = 0.3   # < 此值：0 分
# 区间分线性插值


def compute_cfo_to_np_ratio(quarters: list[QuarterRecord]) -> float | None:
    """
    计算近 4 季 CFO/NP 比值均值（盈余质量核心指标）

    优先使用 QuarterRecord.cfo_to_np 字段（来自 Tushare fina_indicator）；
    若 cfo_to_np 不可用，则尝试从 n_cashflow_act 和 q_dtprofit 估算：
      近 4 季经营现金流合计 / 近 4 季扣非净利润合计

    Args:
        quarters: 按报告期排序的季度记录列表（近期在前）

    Returns:
        CFO/NP 比值，或 None（数据不足）
    """
    if not quarters:
        return None

    # 取最近 4 季
    recent = sorted(quarters, key=lambda q: q.end_date, reverse=True)[:4]

    # 优先：直接使用 cfo_to_np 字段（可能来自 Tushare）
    direct_values = [q.cfo_to_np for q in recent if q.cfo_to_np is not None]
    if len(direct_values) >= 1:
        avg = sum(direct_values) / len(direct_values)
        logger.debug(
            "earnings_quality.cfo_to_np_direct",
            quarters=len(direct_values),
            avg=round(avg, 4),
        )
        return round(avg, 4)

    # 兜底：从 n_cashflow_act 和 q_dtprofit 估算
    cashflow_values = [q.n_cashflow_act for q in recent if q.n_cashflow_act is not None]
    profit_values = [q.q_dtprofit for q in recent if q.q_dtprofit is not None and q.q_dtprofit != 0]

    if len(cashflow_values) < 2 or len(profit_values) < 2:
        logger.debug(
            "earnings_quality.insufficient_data",
            ts_code=recent[0].ts_code if recent else "",
            cashflow_quarters=len(cashflow_values),
            profit_quarters=len(profit_values),
        )
        return None

    total_cashflow = sum(cashflow_values)
    total_profit = sum(profit_values)

    if abs(total_profit) < 1e-6:
        return None

    ratio = total_cashflow / total_profit
    logger.debug(
        "earnings_quality.cfo_to_np_estimated",
        quarters=len(recent),
        total_cashflow=total_cashflow,
        total_profit=total_profit,
        ratio=round(ratio, 4),
    )
    return round(ratio, 4)


def score_earnings_quality(cfo_to_np: float | None) -> float | None:
    """
    将 CFO/NP 比值转换为 0-100 盈余质量分

    规则：
    - >= 0.8 → 100 分（优秀）
    - 0.3~0.8 → 线性插值（0.3→0, 0.8→100）
    - < 0.3 → 0 分（不及格）
    - None → None（数据缺失）

    Args:
        cfo_to_np: CFO/净利润比值

    Returns:
        0-100 分，数据缺失返回 None
    """
    if cfo_to_np is None:
        return None

    if cfo_to_np >= _CFO_TO_NP_EXCELLENT:
        return 100.0

    if cfo_to_np < _CFO_TO_NP_PASSING:
        return 0.0

    # 线性插值：[0.3, 0.8] → [0, 100]
    score = (cfo_to_np - _CFO_TO_NP_PASSING) / (_CFO_TO_NP_EXCELLENT - _CFO_TO_NP_PASSING) * 100.0
    return round(score, 2)
