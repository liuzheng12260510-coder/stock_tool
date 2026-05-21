"""
tests/test_dividend_quality.py — 分红质量模块单元测试

覆盖：
- compute_continuity 连续年数计算（含断年、空记录）
- detect_clearance_dividend 清仓式分红识别
- score_div_continuity 评分正确性
"""
from __future__ import annotations

from datetime import date

import pytest

from app.analytics.dividend_quality import (
    compute_continuity,
    detect_clearance_dividend,
    score_div_continuity,
)
from app.domain.models import DividendRecord


def _make_div(
    ts_code: str = "000001.SZ",
    year: int = 2023,
    cash_div: float | None = 1.0,
    div_proc: str = "实施",
) -> DividendRecord:
    return DividendRecord(
        ts_code=ts_code,
        end_date=date(year, 12, 31),
        div_proc=div_proc,
        cash_div=cash_div,
    )


class TestComputeContinuity:
    def test_empty_returns_zero(self) -> None:
        """空记录返回 0"""
        assert compute_continuity([], 2024) == 0

    def test_no_cash_div_returns_zero(self) -> None:
        """只有送股（无现金派息）返回 0"""
        divs = [
            DividendRecord(ts_code="000001.SZ", end_date=date(2023, 12, 31),
                           cash_div=None, div_proc="实施"),
            DividendRecord(ts_code="000001.SZ", end_date=date(2022, 12, 31),
                           cash_div=0.0, div_proc="实施"),
        ]
        assert compute_continuity(divs, 2024) == 0

    def test_single_year_continuous(self) -> None:
        """最近一年有分红 → 连续 1 年"""
        divs = [_make_div(year=2023)]
        assert compute_continuity(divs, 2024) == 1

    def test_five_consecutive_years(self) -> None:
        """连续 5 年分红"""
        divs = [_make_div(year=y) for y in range(2019, 2024)]
        assert compute_continuity(divs, 2024) == 5

    def test_gap_breaks_streak(self) -> None:
        """2021 年无分红，2022 年分红 → 连续仅 1 年（从 2023 向前，2022 有，2021 无）"""
        divs = [
            _make_div(year=2023, cash_div=1.0),
            _make_div(year=2022, cash_div=0.5),
            # 2021 没有
            _make_div(year=2020, cash_div=0.8),
        ]
        # current_year=2024: 2023→1, 2022→2, 2021 miss → stop
        assert compute_continuity(divs, 2024) == 2

    def test_gap_in_middle(self) -> None:
        """中间有断年，从 current_year-1 向前算到断年为止"""
        divs = [
            _make_div(year=2023),
            # 2022 缺失
            _make_div(year=2021),
            _make_div(year=2020),
        ]
        assert compute_continuity(divs, 2024) == 1

    def test_current_year_not_counted(self) -> None:
        """当年（财年未结束）分红不应被计入"""
        divs = [
            _make_div(year=2024),  # 当年，不算
            _make_div(year=2023),
            _make_div(year=2022),
        ]
        # current_year=2024: 从 2023 开始计数
        assert compute_continuity(divs, 2024) == 2


class TestDetectClearanceDividend:
    def test_normal_dividends_no_clearance(self) -> None:
        """均匀分布的分红，不触发清仓式分红"""
        divs = [_make_div(year=y, cash_div=1.0) for y in range(2019, 2024)]
        assert detect_clearance_dividend(divs) is False

    def test_single_year_spike_clearance(self) -> None:
        """单年派现占 5 年总额 > 70% → 触发清仓式分红"""
        divs = [
            _make_div(year=2023, cash_div=7.0),   # 占 7/10 = 70% → 严格大于才触发
            _make_div(year=2022, cash_div=1.0),
            _make_div(year=2021, cash_div=1.0),
            _make_div(year=2020, cash_div=1.0),
        ]
        # total = 10, spike = 7/10 = 70% 不超过 → 不触发
        assert detect_clearance_dividend(divs) is False

    def test_spike_above_threshold(self) -> None:
        """单年派现占 71% → 触发"""
        divs = [
            _make_div(year=2023, cash_div=7.1),
            _make_div(year=2022, cash_div=1.0),
            _make_div(year=2021, cash_div=1.0),
            _make_div(year=2020, cash_div=0.9),
        ]
        assert detect_clearance_dividend(divs) is True

    def test_only_one_record_no_clearance(self) -> None:
        """只有 1 条记录，数据不足，返回 False"""
        divs = [_make_div(year=2023, cash_div=10.0)]
        assert detect_clearance_dividend(divs) is False

    def test_empty_returns_false(self) -> None:
        """空记录返回 False"""
        assert detect_clearance_dividend([]) is False

    def test_zero_cash_div_excluded(self) -> None:
        """cash_div=0 的记录不参与计算"""
        divs = [
            _make_div(year=2023, cash_div=8.0),
            _make_div(year=2022, cash_div=0.0),  # 不计入
            _make_div(year=2021, cash_div=2.0),
        ]
        # valid = [8.0, 2.0], total = 10, ratio 8/10=0.8 > 0.7
        assert detect_clearance_dividend(divs) is True


class TestScoreDivContinuity:
    def test_zero_years_zero_score(self) -> None:
        assert score_div_continuity(0) == 0.0

    def test_negative_years_zero_score(self) -> None:
        assert score_div_continuity(-1) == 0.0

    def test_five_years_full_score(self) -> None:
        assert score_div_continuity(5) == 100.0

    def test_more_than_five_full_score(self) -> None:
        assert score_div_continuity(10) == 100.0

    def test_linear_interpolation(self) -> None:
        """1~4 年线性插值"""
        assert score_div_continuity(1) == pytest.approx(20.0, abs=0.01)
        assert score_div_continuity(2) == pytest.approx(40.0, abs=0.01)
        assert score_div_continuity(3) == pytest.approx(60.0, abs=0.01)
        assert score_div_continuity(4) == pytest.approx(80.0, abs=0.01)
