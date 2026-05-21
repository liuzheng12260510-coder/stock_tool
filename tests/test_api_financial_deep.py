"""
P3.6 — test_api_financial_deep.py

GET /api/stocks/{ts_code}/financial-deep 端到端测试（TestClient + mock service）
"""
from __future__ import annotations

from unittest.mock import patch

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

_MOCK_REPORT = {
    "ts_code": "600519.SH",
    "dupont_timeseries": [
        {
            "end_date": "2024-12-31",
            "roe": 35.2,
            "npta": 18.5,
            "assets_turn": 0.42,
            "eqt_multiplier": 4.5,
        }
    ],
    "cfo_np_timeseries": [
        {
            "end_date": "2024-12-31",
            "cfo_to_np": 1.05,
            "n_cashflow_act": 30000.0,
            "q_dtprofit": 28000.0,
        }
    ],
    "dividend_history": [
        {
            "end_date": "2024-12-31",
            "cash_div": 27.59,
            "div_proc": "实施",
        }
    ],
    "latest_factor": {
        "composite_score": 88.5,
        "dupont_score": 92.0,
        "earnings_quality_score": 85.0,
        "dividend_continuity": 20,
        "high_leverage_flag": False,
    },
    "red_flags": [],
}

_MOCK_PERCENTILE = {
    "roe_percentile": 95.0,
    "npta_percentile": 88.0,
    "industry": "白酒",
    "peer_count": 15,
}


# ─────────────────────────────────────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestFinancialDeepEndpoint:
    @patch("app.api.routes.stocks.get_financial_deep_report", return_value=_MOCK_REPORT)
    @patch("app.api.routes.stocks.get_dupont_industry_percentile", return_value=_MOCK_PERCENTILE)
    def test_success_200(self, mock_percentile, mock_report) -> None:
        """正常返回 200 含所有必需字段"""
        resp = client.get("/api/stocks/600519.SH/financial-deep")
        assert resp.status_code == 200
        data = resp.json()

        assert data["ts_code"] == "600519.SH"
        assert "dupont_timeseries" in data
        assert "cfo_np_timeseries" in data
        assert "dividend_history" in data
        assert "latest_factor" in data
        assert "red_flags" in data
        assert "dupont_percentile" in data

    @patch("app.api.routes.stocks.get_financial_deep_report", return_value=_MOCK_REPORT)
    @patch("app.api.routes.stocks.get_dupont_industry_percentile", return_value=_MOCK_PERCENTILE)
    def test_dupont_timeseries_structure(self, mock_percentile, mock_report) -> None:
        """dupont_timeseries 每条有 end_date + roe"""
        resp = client.get("/api/stocks/600519.SH/financial-deep")
        data = resp.json()
        for item in data["dupont_timeseries"]:
            assert "end_date" in item
            assert "roe" in item

    @patch("app.api.routes.stocks.get_financial_deep_report", side_effect=Exception("DB error"))
    def test_service_error_returns_500(self, mock_report) -> None:
        """service 层异常 → HTTP 500"""
        resp = client.get("/api/stocks/999999.XY/financial-deep")
        assert resp.status_code == 500
        assert "财务数据获取失败" in resp.json()["detail"]

    @patch("app.api.routes.stocks.get_financial_deep_report", return_value=_MOCK_REPORT)
    @patch(
        "app.api.routes.stocks.get_dupont_industry_percentile",
        side_effect=Exception("percentile error"),
    )
    def test_percentile_error_graceful(self, mock_percentile, mock_report) -> None:
        """行业百分位查询失败不影响主报告，返回空 dict"""
        resp = client.get("/api/stocks/600519.SH/financial-deep")
        assert resp.status_code == 200
        data = resp.json()
        # 降级：percentile 为空 dict 而非报错
        assert data["dupont_percentile"] == {}

    @patch("app.api.routes.stocks.get_financial_deep_report", return_value=_MOCK_REPORT)
    @patch("app.api.routes.stocks.get_dupont_industry_percentile", return_value={})
    def test_red_flags_is_list(self, mock_percentile, mock_report) -> None:
        """red_flags 始终是 list"""
        resp = client.get("/api/stocks/600519.SH/financial-deep")
        data = resp.json()
        assert isinstance(data["red_flags"], list)
