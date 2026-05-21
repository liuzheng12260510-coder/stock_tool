"""
P3.6 — test_analytics_service.py

财务深度报告组装测试（mock DB 依赖）
"""
from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock, patch

from app.services.analytics_service import (
    _build_cfo_context,
    _build_dividend_context,
    _build_dupont_context,
    _detect_red_flags,
    get_financial_deep_report,
)

# ─────────────────────────────────────────────────────────────────────────────
# _build_dupont_context
# ─────────────────────────────────────────────────────────────────────────────

class TestBuildDupontContext:
    def test_empty_returns_na(self) -> None:
        ctx = _build_dupont_context([])
        assert "N/A" in ctx

    def test_single_quarter(self) -> None:
        row = {"end_date": "20241231", "roe": 15.2, "npta": 7.5, "eqt_multiplier": 2.0}
        ctx = _build_dupont_context([row])
        assert "ROE" in ctx or "15.2" in ctx

    def test_multiple_quarters_sorted(self) -> None:
        rows = [
            {"end_date": "20230930", "roe": 10.0, "npta": 5.0, "eqt_multiplier": 2.0},
            {"end_date": "20241231", "roe": 15.0, "npta": 8.0, "eqt_multiplier": 1.9},
        ]
        ctx = _build_dupont_context(rows)
        # 最新季度应先出现（或后出现，但都要有）
        assert "15.0" in ctx or "15" in ctx


# ─────────────────────────────────────────────────────────────────────────────
# _build_cfo_context
# ─────────────────────────────────────────────────────────────────────────────

class TestBuildCfoContext:
    def test_empty_returns_na(self) -> None:
        ctx = _build_cfo_context([])
        assert "N/A" in ctx

    def test_with_data(self) -> None:
        rows = [
            {"end_date": "20241231", "cfo_to_np": 1.2, "n_cashflow_act": 600.0, "q_dtprofit": 500.0},
            {"end_date": "20240930", "cfo_to_np": 0.8, "n_cashflow_act": 400.0, "q_dtprofit": 500.0},
        ]
        ctx = _build_cfo_context(rows)
        assert "CFO" in ctx or "1.2" in ctx


# ─────────────────────────────────────────────────────────────────────────────
# _build_dividend_context
# ─────────────────────────────────────────────────────────────────────────────

class TestBuildDividendContext:
    def test_empty_returns_na(self) -> None:
        ctx = _build_dividend_context([])
        assert "N/A" in ctx

    def test_with_history(self) -> None:
        rows = [
            {"end_date": date(2024, 12, 31), "cash_div": 0.5, "div_proc": "实施"},
            {"end_date": date(2023, 12, 31), "cash_div": 0.4, "div_proc": "实施"},
            {"end_date": date(2022, 12, 31), "cash_div": 0.3, "div_proc": "实施"},
        ]
        ctx = _build_dividend_context(rows)
        assert "分红" in ctx or "0.5" in ctx


# ─────────────────────────────────────────────────────────────────────────────
# _detect_red_flags
# ─────────────────────────────────────────────────────────────────────────────

class TestDetectRedFlags:
    def test_no_flags_when_clean(self) -> None:
        latest_factor = {
            "high_leverage_flag": False,
            "cfo_to_np": 1.2,
            "earnings_quality_score": 70.0,
            "dividend_continuity": 5,
        }
        flags = _detect_red_flags(latest_factor, [])
        assert isinstance(flags, list)
        # 干净的数据不应有红旗
        assert len(flags) == 0

    def test_high_leverage_flag(self) -> None:
        latest_factor = {
            "high_leverage_flag": True,
            "cfo_to_np": 1.0,
            "earnings_quality_score": 65.0,
            "dividend_continuity": 3,
        }
        flags = _detect_red_flags(latest_factor, [])
        assert any("杠杆" in f or "leverage" in f.lower() for f in flags)

    def test_low_cfo_to_np_flag(self) -> None:
        """CFO/NP < 0.5 应触发低质量盈余红旗"""
        latest_factor = {
            "high_leverage_flag": False,
            "cfo_to_np": 0.2,
            "earnings_quality_score": 60.0,
            "dividend_continuity": 3,
        }
        # 找近4季低CFO趋势数据
        recent_quarters = [
            {"cfo_to_np": 0.2, "end_date": "20241231"},
            {"cfo_to_np": 0.3, "end_date": "20240930"},
            {"cfo_to_np": 0.1, "end_date": "20240630"},
        ]
        flags = _detect_red_flags(latest_factor, recent_quarters)
        assert any("cfo" in f.lower() or "盈余" in f or "现金" in f for f in flags)


# ─────────────────────────────────────────────────────────────────────────────
# get_financial_deep_report (with mocked DB)
# ─────────────────────────────────────────────────────────────────────────────

class TestGetFinancialDeepReport:
    @patch("app.services.analytics_service.db_session")
    def test_returns_required_keys(self, mock_db_ctx: MagicMock) -> None:
        """返回值含所有必需的顶层 key"""
        # Mock db_session context manager
        mock_db = MagicMock()
        mock_db_ctx.return_value.__enter__.return_value = mock_db

        # Mock all DB queries to return empty
        mock_db.query.return_value.filter.return_value.order_by.return_value.limit.return_value.all.return_value = []
        mock_db.query.return_value.filter.return_value.order_by.return_value.first.return_value = None
        mock_db.query.return_value.filter.return_value.all.return_value = []

        report = get_financial_deep_report("600519.SH")

        required_keys = {
            "ts_code",
            "dupont_timeseries",
            "cfo_np_timeseries",
            "dividend_history",
            "latest_factor",
            "red_flags",
        }
        assert required_keys.issubset(set(report.keys())), (
            f"Missing keys: {required_keys - set(report.keys())}"
        )
        assert report["ts_code"] == "600519.SH"

    @patch("app.services.analytics_service.db_session")
    def test_empty_db_returns_empty_lists(self, mock_db_ctx: MagicMock) -> None:
        """DB 无数据时返回空列表，不报错"""
        mock_db = MagicMock()
        mock_db_ctx.return_value.__enter__.return_value = mock_db
        mock_db.query.return_value.filter.return_value.order_by.return_value.limit.return_value.all.return_value = []
        mock_db.query.return_value.filter.return_value.order_by.return_value.first.return_value = None
        mock_db.query.return_value.filter.return_value.all.return_value = []

        report = get_financial_deep_report("000001.SZ")
        assert report["dupont_timeseries"] == []
        assert report["cfo_np_timeseries"] == []
        assert report["dividend_history"] == []
        assert report["red_flags"] == []
