"""
TTM 计算 — 修正原代码季度对齐 Bug

核心修复：
1. 严格按 end_date 降序排列后取最近 4 个季度
2. 验证 4 个季度是否连续（无缺口）
3. 不连续或数据不足时返回 None，而非错误数字

季度连续性判断规则：
  Q1(03-31) → Q4(12-31,上年) → Q3(09-30,上年) → Q2(06-30,上年) 一年内
  即相邻两季 end_date 差值应为 3 个月（允许 ±3 天误差）
"""
from __future__ import annotations

from app.core.logging import get_logger
from app.domain.models import QuarterRecord

logger = get_logger("ttm")

# 季度间隔天数范围（3 个月 = 约 89-92 天）
_QUARTER_DAYS_MIN = 85
_QUARTER_DAYS_MAX = 97


def _quarters_are_continuous(quarters: list[QuarterRecord]) -> bool:
    """
    检验 quarters（已按 end_date 降序排列）中相邻两季是否连续

    连续定义：end_date[i] - end_date[i+1] 在 [85, 97] 天范围内
    """
    for i in range(len(quarters) - 1):
        delta = (quarters[i].end_date - quarters[i + 1].end_date).days
        if not (_QUARTER_DAYS_MIN <= delta <= _QUARTER_DAYS_MAX):
            logger.debug(
                "季度不连续",
                code=quarters[i].ts_code,
                q1=quarters[i].end_date,
                q2=quarters[i + 1].end_date,
                delta=delta,
            )
            return False
    return True


def compute_ttm_deduct_profit(
    quarters: list[QuarterRecord],
    min_quarters: int = 4,
) -> float | None:
    """
    计算 TTM 扣非净利润（元）

    Args:
        quarters: 该股票的所有季度财务记录（顺序无要求）
        min_quarters: 最少需要的季度数

    Returns:
        TTM 扣非净利润（元），若无法计算则返回 None
    """
    if len(quarters) < min_quarters:
        return None

    # 严格降序排列
    sorted_qs = sorted(quarters, key=lambda x: x.end_date, reverse=True)
    last_four = sorted_qs[:4]

    if len(last_four) < 4:
        return None

    # 连续性校验
    if not _quarters_are_continuous(last_four):
        logger.debug(
            "TTM 计算跳过：季度不连续",
            ts_code=last_four[0].ts_code,
            end_dates=[str(q.end_date) for q in last_four],
        )
        return None

    # 单季数据有效性检查
    for q in last_four:
        if q.q_dtprofit is None or q.q_dtprofit != q.q_dtprofit:  # NaN 检查
            logger.debug(
                "TTM 计算跳过：单季扣非利润缺失",
                ts_code=q.ts_code,
                end_date=str(q.end_date),
            )
            return None

    ttm_profit = sum(q.q_dtprofit for q in last_four)
    return ttm_profit


def compute_ttm_deduct_pe(
    quarters: list[QuarterRecord],
    total_mv_wan: float,
) -> float | None:
    """
    计算扣非 TTM PE

    Args:
        quarters:     季度财务记录
        total_mv_wan: 当日总市值（万元，Tushare 单位）

    Returns:
        扣非 PE TTM，若无法计算则返回 None
    """
    ttm_profit = compute_ttm_deduct_profit(quarters)
    if ttm_profit is None:
        return None

    if ttm_profit <= 0:
        # 亏损企业 PE 无意义
        return None

    # total_mv 单位：万元 → 利润单位：元
    # total_mv * 10000 = 元
    total_mv_yuan = total_mv_wan * 10000
    pe = total_mv_yuan / ttm_profit
    return round(pe, 2)


def compute_ttm_revenue(quarters: list[QuarterRecord]) -> float | None:
    """计算 TTM 营业收入（元）"""
    if len(quarters) < 4:
        return None

    sorted_qs = sorted(quarters, key=lambda x: x.end_date, reverse=True)
    last_four = sorted_qs[:4]

    if not _quarters_are_continuous(last_four):
        return None

    revenues = [q.op_revenue for q in last_four]
    if any(r is None or r != r for r in revenues):
        return None

    return sum(revenues)


def compute_growth_rate(quarters: list[QuarterRecord]) -> float | None:
    """
    计算 TTM 营收同比增速（%）

    对比最近 TTM vs 一年前 TTM
    需要至少 8 个连续季度
    """
    if len(quarters) < 8:
        return None

    sorted_qs = sorted(quarters, key=lambda x: x.end_date, reverse=True)

    # 最近 4 季
    recent_four = sorted_qs[:4]
    # 一年前 4 季
    prev_four = sorted_qs[4:8]

    if not _quarters_are_continuous(recent_four) or not _quarters_are_continuous(prev_four):
        return None

    recent_rev = sum(q.op_revenue or 0 for q in recent_four)
    prev_rev = sum(q.op_revenue or 0 for q in prev_four)

    if prev_rev <= 0:
        return None

    growth = (recent_rev - prev_rev) / prev_rev * 100
    return round(growth, 2)
