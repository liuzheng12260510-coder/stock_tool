"""
tests/test_earnings_quality.py — 盈余质量模块单元测试

覆盖：
- CFO/NP 直接字段优先使用
- 从 n_cashflow_act + q_dtprofit 估算兜底
- 连续季度缺失降级（返回 None）
- score_earnings_quality 评分规则正确性
"""
from __future__ import annotations

from datetime import date

import pytest

from app.analytics.earnings_quality import compute_cfo_to_np_ratio, score_earnings_quality
from app.domain.models import QuarterRecord


def _make_quarter(
    ts_code: str = "000001.SZ",
    year: int = 2024,
    month: int = 9,
    q_dtprofit: float = 1_000_000.0,
    n_cashflow_act: float | None = None,
    cfo_to_np: float | None = None,
) -> QuarterRecord:
    return QuarterRecord(
        ts_code=ts_code,
        end_date=date(year, month, 30 if month in (6, 9) else 31),
        q_dtprofit=q_dtprofit,
        n_cashflow_act=n_cashflow_act,
        cfo_to_np=cfo_to_np,
    )


class TestComputeCfoToNpRatio:
    def test_direct_cfo_to_np_used_first(self) -> None:
        """优先使用 cfo_to_np 字段（来自 Tushare）"""
        quarters = [
            _make_quarter(year=2024, month=9, cfo_to_np=1.2),
            _make_quarter(year=2024, month=6, cfo_to_np=0.9),
        ]
        result = compute_cfo_to_np_ratio(quarters)
        assert result is not None
        assert result == pytest.approx(1.05, abs=0.01)

    def test_fallback_from_cashflow_and_profit(self) -> None:
        """cfo_to_np 不可用时，从 n_cashflow_act / q_dtprofit 估算"""
        quarters = [
            _make_quarter(year=2024, month=9, q_dtprofit=1_000_000, n_cashflow_act=900_000),
            _make_quarter(year=2024, month=6, q_dtprofit=800_000, n_cashflow_act=700_000),
            _make_quarter(year=2024, month=3, q_dtprofit=1_200_000, n_cashflow_act=1_100_000),
        ]
        result = compute_cfo_to_np_ratio(quarters)
        # total_cashflow = 2_700_000, total_profit = 3_000_000
        # ratio = 2_700_000 / 3_000_000 = 0.9
        assert result is not None
        assert result == pytest.approx(0.9, abs=0.01)

    def test_empty_quarters_returns_none(self) -> None:
        """空列表返回 None"""
        assert compute_cfo_to_np_ratio([]) is None

    def test_insufficient_cashflow_data(self) -> None:
        """cashflow 数据只有 1 季，返回 None（需要至少 2 季）"""
        quarters = [
            _make_quarter(year=2024, month=9, q_dtprofit=1_000_000, n_cashflow_act=800_000),
        ]
        result = compute_cfo_to_np_ratio(quarters)
        # Only 1 cashflow value → insufficient
        assert result is None

    def test_zero_profit_returns_none(self) -> None:
        """分母（利润合计）为 0 时返回 None，避免除零"""
        quarters = [
            _make_quarter(year=2024, month=9, q_dtprofit=0.0, n_cashflow_act=100_000),
            _make_quarter(year=2024, month=6, q_dtprofit=0.0, n_cashflow_act=200_000),
        ]
        result = compute_cfo_to_np_ratio(quarters)
        assert result is None

    def test_takes_most_recent_four_quarters(self) -> None:
        """有超过 4 季时只取最近 4 季"""
        quarters = [
            _make_quarter(year=2022, month=3, cfo_to_np=10.0),  # 旧的，不应被采用
            _make_quarter(year=2023, month=6, cfo_to_np=0.5),
            _make_quarter(year=2023, month=9, cfo_to_np=0.6),
            _make_quarter(year=2023, month=12, cfo_to_np=0.7),
            _make_quarter(year=2024, month=3, cfo_to_np=0.8),
        ]
        result = compute_cfo_to_np_ratio(quarters)
        # 最近 4 季：Q1-24(0.8), Q4-23(0.7), Q3-23(0.6), Q2-23(0.5)
        # avg = (0.8+0.7+0.6+0.5)/4 = 0.65
        assert result is not None
        assert result == pytest.approx(0.65, abs=0.01)


class TestScoreEarningsQuality:
    def test_none_returns_none(self) -> None:
        assert score_earnings_quality(None) is None

    def test_excellent_above_08(self) -> None:
        """>= 0.8 → 满分 100"""
        assert score_earnings_quality(0.8) == 100.0
        assert score_earnings_quality(1.2) == 100.0
        assert score_earnings_quality(2.0) == 100.0

    def test_failing_below_03(self) -> None:
        """< 0.3 → 0 分"""
        assert score_earnings_quality(0.3 - 0.001) == 0.0
        assert score_earnings_quality(0.0) == 0.0
        assert score_earnings_quality(-0.5) == 0.0

    def test_linear_mid_range(self) -> None:
        """0.3 ~ 0.8 线性插值"""
        # 0.55 正好在中间 → 50 分
        score = score_earnings_quality(0.55)
        assert score is not None
        assert score == pytest.approx(50.0, abs=0.1)

    def test_boundary_exactly_03(self) -> None:
        """恰好 0.3 → 0 分（边界值）"""
        assert score_earnings_quality(0.3) == 0.0

    def test_boundary_just_above_08(self) -> None:
        """刚好超过 0.8 → 100 分"""
        assert score_earnings_quality(0.801) == 100.0
