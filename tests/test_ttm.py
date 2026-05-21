"""
TTM 计算正确性测试 — 验证季度对齐修正

核心用例：
1. 4 个连续季度 → 返回正确 TTM 利润
2. 4 个不连续季度（有缺口）→ 返回 None
3. 数据不足 3 季 → 返回 None
4. 有负利润季度 → TTM 为负，PE 返回 None
"""
from __future__ import annotations

from datetime import date

import pytest

from app.analytics.ttm import (
    compute_ttm_deduct_pe,
    compute_ttm_deduct_profit,
    compute_growth_rate,
    _quarters_are_continuous,
)
from app.domain.models import QuarterRecord


def make_quarter(ts_code: str, year: int, q: int, profit: float, revenue: float = 1e9) -> QuarterRecord:
    """便捷构建 QuarterRecord"""
    month_day = {1: (3, 31), 2: (6, 30), 3: (9, 30), 4: (12, 31)}[q]
    end_date = date(year, month_day[0], month_day[1])
    return QuarterRecord(
        ts_code=ts_code,
        end_date=end_date,
        q_dtprofit=profit,
        op_revenue=revenue,
    )


class TestQuartersContinuity:
    def test_continuous_4_quarters(self):
        """4 个连续季度应判断为连续"""
        qs = [
            make_quarter("000001.SZ", 2024, 4, 1e8),
            make_quarter("000001.SZ", 2024, 3, 1e8),
            make_quarter("000001.SZ", 2024, 2, 1e8),
            make_quarter("000001.SZ", 2024, 1, 1e8),
        ]
        # 已降序排列
        qs_sorted = sorted(qs, key=lambda x: x.end_date, reverse=True)
        assert _quarters_are_continuous(qs_sorted) is True

    def test_gap_between_quarters(self):
        """有缺口时应返回 False"""
        qs = [
            make_quarter("000001.SZ", 2024, 4, 1e8),
            make_quarter("000001.SZ", 2024, 2, 1e8),  # 跳过 Q3
            make_quarter("000001.SZ", 2024, 1, 1e8),
            make_quarter("000001.SZ", 2023, 4, 1e8),
        ]
        qs_sorted = sorted(qs, key=lambda x: x.end_date, reverse=True)
        assert _quarters_are_continuous(qs_sorted) is False


class TestTTMProfit:
    def test_correct_sum_4_quarters(self):
        """4 个连续季度利润之和"""
        qs = [
            make_quarter("000001.SZ", 2024, 4, 1.0e8),
            make_quarter("000001.SZ", 2024, 3, 1.5e8),
            make_quarter("000001.SZ", 2024, 2, 2.0e8),
            make_quarter("000001.SZ", 2024, 1, 0.5e8),
        ]
        profit = compute_ttm_deduct_profit(qs)
        assert profit == pytest.approx(5.0e8)

    def test_insufficient_quarters(self):
        """少于 4 季返回 None"""
        qs = [
            make_quarter("000001.SZ", 2024, 4, 1e8),
            make_quarter("000001.SZ", 2024, 3, 1e8),
        ]
        assert compute_ttm_deduct_profit(qs) is None

    def test_gap_returns_none(self):
        """季度不连续返回 None，而非错误数字"""
        qs = [
            make_quarter("000001.SZ", 2024, 4, 1e8),
            make_quarter("000001.SZ", 2024, 2, 1e8),  # 跳过 Q3！
            make_quarter("000001.SZ", 2024, 1, 1e8),
            make_quarter("000001.SZ", 2023, 4, 1e8),
        ]
        # 这是原代码的核心 Bug：不检查连续性就累加
        assert compute_ttm_deduct_profit(qs) is None

    def test_negative_profit(self):
        """含负利润季度：TTM 为各季度之和（可为 0 或负）"""
        qs = [
            make_quarter("000001.SZ", 2024, 4, -1e8),
            make_quarter("000001.SZ", 2024, 3, -0.5e8),
            make_quarter("000001.SZ", 2024, 2,  1e8),
            make_quarter("000001.SZ", 2024, 1,  0.5e8),
        ]
        profit = compute_ttm_deduct_profit(qs)
        # -1e8 + (-0.5e8) + 1e8 + 0.5e8 = 0
        assert profit == pytest.approx(0.0)

    def test_all_negative_profit(self):
        """全负利润：TTM 为负值"""
        qs = [
            make_quarter("000001.SZ", 2024, 4, -1e8),
            make_quarter("000001.SZ", 2024, 3, -2e8),
            make_quarter("000001.SZ", 2024, 2, -1.5e8),
            make_quarter("000001.SZ", 2024, 1, -0.5e8),
        ]
        profit = compute_ttm_deduct_profit(qs)
        assert profit == pytest.approx(-5e8)


class TestTTMPE:
    def test_pe_calculation(self):
        """验证扣非 PE 计算：总市值 / TTM 利润"""
        qs = [
            make_quarter("000001.SZ", 2024, 4, 2.5e8),
            make_quarter("000001.SZ", 2024, 3, 2.5e8),
            make_quarter("000001.SZ", 2024, 2, 2.5e8),
            make_quarter("000001.SZ", 2024, 1, 2.5e8),
        ]
        # TTM 利润 = 10e8 元
        # 总市值 = 1000 万元 = 1000 * 10000 = 1e8 元？ → PE = 1e8 / 10e8 = 0.1
        # 更合理的：总市值 100亿元 = 100 * 1e8 = 1e10 元，利润 10e8 = 1e9 → PE = 10
        total_mv_wan = 100 * 10000  # 100亿元 = 100 * 10000 万元
        pe = compute_ttm_deduct_pe(qs, total_mv_wan=total_mv_wan)
        # TTM 利润 = 4 * 2.5e8 = 10e8
        # 总市值(元) = 100 * 10000 * 10000 = 1e10
        # PE = 1e10 / 1e9 = 10
        assert pe == pytest.approx(10.0)

    def test_pe_none_on_loss(self):
        """亏损时 PE 返回 None"""
        qs = [
            make_quarter("000001.SZ", 2024, 4, -1e8),
            make_quarter("000001.SZ", 2024, 3, -1e8),
            make_quarter("000001.SZ", 2024, 2, -1e8),
            make_quarter("000001.SZ", 2024, 1, -1e8),
        ]
        assert compute_ttm_deduct_pe(qs, total_mv_wan=1e6) is None


class TestGrowthRate:
    def test_growth_rate_positive(self):
        """营收增长 20% 的计算"""
        qs = []
        # 最近 4 季（2024）营收各 1.2e9
        for q in [4, 3, 2, 1]:
            qs.append(make_quarter("000001.SZ", 2024, q, 1e8, revenue=1.2e9))
        # 上年 4 季（2023）营收各 1e9
        for q in [4, 3, 2, 1]:
            qs.append(make_quarter("000001.SZ", 2023, q, 1e8, revenue=1.0e9))

        rate = compute_growth_rate(qs)
        # (4*1.2e9 - 4*1e9) / (4*1e9) * 100 = 20%
        assert rate == pytest.approx(20.0, abs=0.1)

    def test_growth_rate_insufficient(self):
        """少于 8 季返回 None"""
        qs = [make_quarter("000001.SZ", 2024, q, 1e8) for q in [4, 3, 2, 1]]
        assert compute_growth_rate(qs) is None
