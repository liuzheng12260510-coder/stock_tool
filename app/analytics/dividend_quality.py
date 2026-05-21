"""
分红质量分析模块

职责：
1. compute_continuity    — 计算连续分红年数（向前回溯，遇断年即止）
2. detect_clearance_div  — 识别"清仓式分红"（单年派现占近 5 年总额 > 70%）
3. score_div_continuity  — 连续年数 → 0-100 分

"清仓式分红"识别逻辑：
  - 取有效派现记录（cash_div > 0）中时间最近的 5 条
  - 若有任一年的 cash_div 占 5 年合计 > 70%，则疑似清仓分红
  - 警告：这可能意味着大股东在减持前通过高额分红套现

连续年数评分（0-100）：
  - 0 年 → 0 分
  - >= 5 年 → 100 分
  - 1-4 年 → 线性插值
"""
from __future__ import annotations

from app.core.logging import get_logger
from app.domain.models import DividendRecord

logger = get_logger("dividend_quality")

# 满分阈值
_FULL_SCORE_YEARS = 5

# 清仓式分红阈值（单年占比超过此值视为清仓式）
_CLEARANCE_THRESHOLD = 0.70


def compute_continuity(
    dividends: list[DividendRecord],
    current_year: int,
) -> int:
    """
    计算连续分红年数（从 current_year - 1 向前回溯）

    只统计已实施的现金分红（cash_div > 0）；遇到断年立即停止计数。

    Args:
        dividends:    该股票所有历史分红记录
        current_year: 当前年份（通常为 trade_date.year）

    Returns:
        连续分红年数（>= 0）
    """
    if not dividends:
        return 0

    # 收集有实际现金派发的年份
    dividend_years: set[int] = set()
    for d in dividends:
        if d.cash_div is not None and d.cash_div > 0:
            dividend_years.add(d.end_date.year)

    if not dividend_years:
        return 0

    # 从最近完成的财年起向前统计连续年数
    consecutive = 0
    for year in range(current_year - 1, current_year - 15, -1):
        if year in dividend_years:
            consecutive += 1
        else:
            break

    logger.debug(
        "dividend_quality.continuity",
        current_year=current_year,
        dividend_years=sorted(dividend_years, reverse=True)[:6],
        consecutive=consecutive,
    )
    return consecutive


def detect_clearance_dividend(dividends: list[DividendRecord]) -> bool:
    """
    识别"清仓式分红"：单年派现占近 5 年总额 > 70%

    策略：
    - 过滤出有效现金派现记录（cash_div > 0）
    - 按财年倒序取最近 5 条
    - 若任一年的 cash_div 占合计 > 70% 则触发红旗

    Args:
        dividends: 该股票所有历史分红记录

    Returns:
        True = 疑似清仓式分红；False = 正常
    """
    valid = [d for d in dividends if d.cash_div is not None and d.cash_div > 0]
    if len(valid) < 2:
        # 数据不足，无法判断
        return False

    # 按财年倒序，取最近 5 年
    recent_five = sorted(valid, key=lambda d: d.end_date.year, reverse=True)[:5]
    total = sum(d.cash_div for d in recent_five)  # type: ignore[misc]

    if total <= 0:
        return False

    for d in recent_five:
        ratio = d.cash_div / total  # type: ignore[operator]
        if ratio > _CLEARANCE_THRESHOLD:
            logger.warning(
                "dividend_quality.clearance_detected",
                ts_code=d.ts_code,
                year=d.end_date.year,
                ratio=round(ratio, 3),
                cash_div=d.cash_div,
                total_5y=round(total, 4),
            )
            return True

    return False


def score_div_continuity(continuity_years: int) -> float:
    """
    连续分红年数 → 0-100 分

    满分阈值：>= 5 年 → 100 分
    线性插值：1-4 年
    0 年 → 0 分

    Args:
        continuity_years: 连续分红年数

    Returns:
        0-100 分（浮点数）
    """
    if continuity_years <= 0:
        return 0.0
    if continuity_years >= _FULL_SCORE_YEARS:
        return 100.0
    return round(continuity_years / _FULL_SCORE_YEARS * 100.0, 2)
