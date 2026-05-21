"""
AkShare Provider 单元测试 — P2

覆盖：
- _code_to_ts_code 辅助函数（SH / SZ / BJ 后缀映射）
- get_stock_basic 字段映射
- get_daily 字段映射 + 单位转换（amount/total_mv/circ_mv）
- get_daily_basic 字段映射 + 单位转换（亿元→万元）
- get_financial_quarterly 循环 + period 过滤
- get_dividend 字段映射
- get_dividend_batch 返回空 DataFrame
- get_trade_calendar 日期过滤
- AkShare 不可用时抛出 DataProviderError

所有测试通过 unittest.mock.patch 模拟 akshare 接口，不发起真实网络请求。
"""
from __future__ import annotations

from datetime import date
from unittest.mock import patch

import pandas as pd
import pytest

from app.core.exceptions import DataProviderError
from app.data.akshare_provider import (
    _AKSHARE_TO_TUSHARE_FIELDS,
    AkShareProvider,
    _code_to_ts_code,
    _to_float_safe,
)

# ─────────────────────────────────────────────────────────────────────────────
# Fixture
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture()
def provider() -> AkShareProvider:
    """创建 AkShareProvider，patch _AKSHARE_AVAILABLE=True 以绕过安装检查"""
    with patch("app.data.akshare_provider._AKSHARE_AVAILABLE", True):
        return AkShareProvider()


# ─────────────────────────────────────────────────────────────────────────────
# 辅助函数
# ─────────────────────────────────────────────────────────────────────────────


class TestCodeToTsCode:
    def test_sh_main_board(self) -> None:
        assert _code_to_ts_code("600000") == "600000.SH"
        assert _code_to_ts_code("601398") == "601398.SH"

    def test_sh_kcb(self) -> None:
        """科创板 688xxx → .SH"""
        assert _code_to_ts_code("688001") == "688001.SH"

    def test_sz_main_board(self) -> None:
        assert _code_to_ts_code("000001") == "000001.SZ"
        assert _code_to_ts_code("001979") == "001979.SZ"

    def test_sz_chinext(self) -> None:
        """创业板 300xxx → .SZ"""
        assert _code_to_ts_code("300750") == "300750.SZ"

    def test_bj_4(self) -> None:
        """北交所 4xxxxx → .BJ"""
        assert _code_to_ts_code("430001") == "430001.BJ"

    def test_bj_8(self) -> None:
        """北交所 8xxxxx → .BJ"""
        assert _code_to_ts_code("830001") == "830001.BJ"

    def test_zero_padded_input(self) -> None:
        """输入可能带前导零，应正确处理"""
        assert _code_to_ts_code("000001") == "000001.SZ"
        assert _code_to_ts_code("1") == "000001.SZ"   # lstrip + zfill


class TestToFloatSafe:
    def test_valid_number(self) -> None:
        assert _to_float_safe(3.14) == pytest.approx(3.14)
        assert _to_float_safe("1.5") == pytest.approx(1.5)

    def test_none_returns_none(self) -> None:
        assert _to_float_safe(None) is None

    def test_nan_returns_none(self) -> None:
        import math  # noqa: PLC0415
        assert _to_float_safe(math.nan) is None

    def test_inf_returns_none(self) -> None:
        import math  # noqa: PLC0415
        assert _to_float_safe(math.inf) is None

    def test_invalid_string_returns_none(self) -> None:
        assert _to_float_safe("not_a_number") is None


# ─────────────────────────────────────────────────────────────────────────────
# get_stock_basic
# ─────────────────────────────────────────────────────────────────────────────


class TestGetStockBasic:
    def test_field_mapping(self, provider: AkShareProvider) -> None:
        mock_df = pd.DataFrame({
            "code": ["600000", "000001"],
            "name": ["浦发银行", "平安银行"],
        })
        with patch("app.data.akshare_provider.ak") as mock_ak:
            mock_ak.stock_info_a_code_name.return_value = mock_df
            result = provider.get_stock_basic()

        assert len(result) == 2
        assert set(result.columns) >= {"ts_code", "name", "exchange"}

    def test_ts_code_conversion(self, provider: AkShareProvider) -> None:
        mock_df = pd.DataFrame({
            "code": ["600000", "000001"],
            "name": ["浦发银行", "平安银行"],
        })
        with patch("app.data.akshare_provider.ak") as mock_ak:
            mock_ak.stock_info_a_code_name.return_value = mock_df
            result = provider.get_stock_basic()

        ts_dict = dict(zip(result["name"], result["ts_code"], strict=False))
        assert ts_dict["浦发银行"] == "600000.SH"
        assert ts_dict["平安银行"] == "000001.SZ"

    def test_exchange_mapping(self, provider: AkShareProvider) -> None:
        mock_df = pd.DataFrame({
            "code": ["600000", "000001"],
            "name": ["浦发银行", "平安银行"],
        })
        with patch("app.data.akshare_provider.ak") as mock_ak:
            mock_ak.stock_info_a_code_name.return_value = mock_df
            result = provider.get_stock_basic()

        ex_dict = dict(zip(result["ts_code"], result["exchange"], strict=False))
        assert ex_dict["600000.SH"] == "SSE"
        assert ex_dict["000001.SZ"] == "SZSE"

    def test_empty_input_returns_empty(self, provider: AkShareProvider) -> None:
        with patch("app.data.akshare_provider.ak") as mock_ak:
            mock_ak.stock_info_a_code_name.return_value = pd.DataFrame()
            result = provider.get_stock_basic()
        assert result.empty

    def test_api_error_raises_data_provider_error(self, provider: AkShareProvider) -> None:
        with patch("app.data.akshare_provider.ak") as mock_ak:
            mock_ak.stock_info_a_code_name.side_effect = RuntimeError("network error")
            with pytest.raises(DataProviderError, match="get_stock_basic"):
                provider.get_stock_basic()


# ─────────────────────────────────────────────────────────────────────────────
# get_daily
# ─────────────────────────────────────────────────────────────────────────────


class TestGetDaily:
    def _make_spot_df(self) -> pd.DataFrame:
        return pd.DataFrame({
            "代码": ["600000"],
            "名称": ["浦发银行"],
            "最新价": [10.50],
            "今开": [10.30],
            "最高": [10.80],
            "最低": [10.20],
            "涨跌幅": [1.5],
            "成交量": [1000],        # 手
            "成交额": [10_500_000.0],  # 元 → 应转为 10500 千元
            "总市值": [1_000_000_000.0],  # 元 → 应转为 100000 万元
            "流通市值": [800_000_000.0],   # 元 → 应转为 80000 万元
        })

    def test_ts_code_in_result(self, provider: AkShareProvider) -> None:
        with patch("app.data.akshare_provider.ak") as mock_ak:
            mock_ak.stock_zh_a_spot_em.return_value = self._make_spot_df()
            result = provider.get_daily("20250519")

        assert len(result) == 1
        assert result["ts_code"].values[0] == "600000.SH"

    def test_amount_unit_conversion(self, provider: AkShareProvider) -> None:
        """成交额：元 / 1000 = 千元"""
        with patch("app.data.akshare_provider.ak") as mock_ak:
            mock_ak.stock_zh_a_spot_em.return_value = self._make_spot_df()
            result = provider.get_daily("20250519")

        assert result["amount"].values[0] == pytest.approx(10_500.0, rel=1e-3)

    def test_total_mv_unit_conversion(self, provider: AkShareProvider) -> None:
        """总市值：元 / 10000 = 万元"""
        with patch("app.data.akshare_provider.ak") as mock_ak:
            mock_ak.stock_zh_a_spot_em.return_value = self._make_spot_df()
            result = provider.get_daily("20250519")

        assert result["total_mv"].values[0] == pytest.approx(100_000.0, rel=1e-3)

    def test_circ_mv_unit_conversion(self, provider: AkShareProvider) -> None:
        """流通市值：元 / 10000 = 万元"""
        with patch("app.data.akshare_provider.ak") as mock_ak:
            mock_ak.stock_zh_a_spot_em.return_value = self._make_spot_df()
            result = provider.get_daily("20250519")

        assert result["circ_mv"].values[0] == pytest.approx(80_000.0, rel=1e-3)

    def test_trade_date_set(self, provider: AkShareProvider) -> None:
        with patch("app.data.akshare_provider.ak") as mock_ak:
            mock_ak.stock_zh_a_spot_em.return_value = self._make_spot_df()
            result = provider.get_daily("20250519")

        assert result["trade_date"].values[0] == "20250519"

    def test_empty_response_returns_empty(self, provider: AkShareProvider) -> None:
        with patch("app.data.akshare_provider.ak") as mock_ak:
            mock_ak.stock_zh_a_spot_em.return_value = pd.DataFrame()
            result = provider.get_daily("20250519")
        assert result.empty

    def test_api_error_raises(self, provider: AkShareProvider) -> None:
        with patch("app.data.akshare_provider.ak") as mock_ak:
            mock_ak.stock_zh_a_spot_em.side_effect = RuntimeError("timeout")
            with pytest.raises(DataProviderError):
                provider.get_daily("20250519")


# ─────────────────────────────────────────────────────────────────────────────
# get_daily_basic
# ─────────────────────────────────────────────────────────────────────────────


class TestGetDailyBasic:
    def _make_indicator_df(self) -> pd.DataFrame:
        return pd.DataFrame({
            "代码": ["600000", "000001"],
            "市盈率(TTM)": [12.5, 6.8],
            "市净率": [1.2, 0.9],
            "股息率": [3.0, 4.2],
            "总市值": [100.0, 200.0],   # 亿元 → 应转为 1000000 / 2000000 万元
            "流通市值": [80.0, 160.0],  # 亿元
        })

    def test_pe_pb_dv_fields(self, provider: AkShareProvider) -> None:
        with patch("app.data.akshare_provider.ak") as mock_ak:
            mock_ak.stock_a_lg_indicator.return_value = self._make_indicator_df()
            result = provider.get_daily_basic("20250519")

        assert "pe_ttm" in result.columns
        assert "pb" in result.columns
        assert "dv_ttm" in result.columns

    def test_total_mv_unit_conversion(self, provider: AkShareProvider) -> None:
        """亿元 × 10000 = 万元"""
        with patch("app.data.akshare_provider.ak") as mock_ak:
            mock_ak.stock_a_lg_indicator.return_value = self._make_indicator_df()
            result = provider.get_daily_basic("20250519")

        row_600000 = result[result["ts_code"] == "600000.SH"]
        assert not row_600000.empty
        assert row_600000["total_mv"].values[0] == pytest.approx(1_000_000.0, rel=1e-3)

    def test_empty_response_returns_empty(self, provider: AkShareProvider) -> None:
        with patch("app.data.akshare_provider.ak") as mock_ak:
            mock_ak.stock_a_lg_indicator.return_value = pd.DataFrame()
            result = provider.get_daily_basic("20250519")
        assert result.empty


# ─────────────────────────────────────────────────────────────────────────────
# get_financial_quarterly
# ─────────────────────────────────────────────────────────────────────────────


class TestGetFinancialQuarterly:
    def _make_abstract_df(self) -> pd.DataFrame:
        return pd.DataFrame({
            "报告期": ["20241231", "20240930", "20240630"],
            "净利润": [1e9, 8e8, 6e8],
            "扣除非经常损益后的净利润": [9.5e8, 7.5e8, 5.5e8],
            "营业收入": [5e9, 4e9, 3e9],
            "净资产收益率": [15.0, 13.0, 10.0],
            "净利润增长率": [12.0, 10.0, 8.0],
        })

    def test_empty_codes_returns_empty(self, provider: AkShareProvider) -> None:
        result = provider.get_financial_quarterly()
        assert result.empty

    def test_single_code_loop(self, provider: AkShareProvider) -> None:
        with patch("app.data.akshare_provider.ak") as mock_ak:
            mock_ak.stock_financial_abstract.return_value = self._make_abstract_df()
            result = provider.get_financial_quarterly(ts_codes=["600000.SH"])

        assert len(result) == 3
        assert all(result["ts_code"] == "600000.SH")
        assert result["source"].values[0] == "akshare"

    def test_period_filter(self, provider: AkShareProvider) -> None:
        """period 参数应只保留对应报告期的行"""
        with patch("app.data.akshare_provider.ak") as mock_ak:
            mock_ak.stock_financial_abstract.return_value = self._make_abstract_df()
            result = provider.get_financial_quarterly(
                ts_codes=["600000.SH"],
                period="20241231",
            )

        assert len(result) == 1
        assert result["end_date"].values[0] == "20241231"

    def test_q_dtprofit_field_populated(self, provider: AkShareProvider) -> None:
        """q_dtprofit 应从 q_dtprofit_cum 映射（best-effort）"""
        with patch("app.data.akshare_provider.ak") as mock_ak:
            mock_ak.stock_financial_abstract.return_value = self._make_abstract_df()
            result = provider.get_financial_quarterly(ts_codes=["600000.SH"])

        assert "q_dtprofit" in result.columns
        assert result["q_dtprofit"].notna().any()

    def test_multiple_codes(self, provider: AkShareProvider) -> None:
        """多股票循环调用"""
        with patch("app.data.akshare_provider.ak") as mock_ak:
            mock_ak.stock_financial_abstract.return_value = self._make_abstract_df()
            result = provider.get_financial_quarterly(
                ts_codes=["600000.SH", "000001.SZ"],
            )

        # 每只股票 3 行 × 2 只 = 6 行
        assert len(result) == 6
        assert set(result["ts_code"].unique()) == {"600000.SH", "000001.SZ"}

    def test_single_code_api_error_degraded_gracefully(
        self, provider: AkShareProvider
    ) -> None:
        """单只股票 API 失败不应影响其他股票"""

        def side_effect(symbol: str):
            if symbol == "000001":
                raise RuntimeError("API error for 000001")
            return pd.DataFrame({
                "报告期": ["20241231"],
                "净利润": [1e9],
                "扣除非经常损益后的净利润": [9.5e8],
            })

        with patch("app.data.akshare_provider.ak") as mock_ak:
            mock_ak.stock_financial_abstract.side_effect = side_effect
            result = provider.get_financial_quarterly(
                ts_codes=["600000.SH", "000001.SZ"],
            )

        # Only 600000.SH should have data
        assert len(result) == 1
        assert result["ts_code"].values[0] == "600000.SH"


# ─────────────────────────────────────────────────────────────────────────────
# get_dividend
# ─────────────────────────────────────────────────────────────────────────────


class TestGetDividend:
    def _make_dividend_df(self) -> pd.DataFrame:
        return pd.DataFrame({
            "公告日期": ["2024-07-01"],
            "分红年度": ["20231231"],
            "每股送股比例": [0.0],
            "每股派息(税前)": [0.5],
            "每股派息(税后)": [0.45],
            "派息日": ["2024-08-01"],
            "股权登记日": ["2024-07-28"],
            "除权除息日": ["2024-07-29"],
        })

    def test_field_mapping(self, provider: AkShareProvider) -> None:
        with patch("app.data.akshare_provider.ak") as mock_ak:
            mock_ak.stock_history_dividend_detail.return_value = self._make_dividend_df()
            result = provider.get_dividend("000001.SZ")

        assert len(result) == 1
        assert result["ts_code"].values[0] == "000001.SZ"
        assert result["div_proc"].values[0] == "实施"
        assert result["cash_div"].values[0] == pytest.approx(0.5)
        assert result["cash_div_tax"].values[0] == pytest.approx(0.45)

    def test_date_format_converted_to_yyyymmdd(self, provider: AkShareProvider) -> None:
        with patch("app.data.akshare_provider.ak") as mock_ak:
            mock_ak.stock_history_dividend_detail.return_value = self._make_dividend_df()
            result = provider.get_dividend("000001.SZ")

        assert result["ann_date"].values[0] == "20240701"
        assert result["end_date"].values[0] == "20231231"

    def test_end_date_filter(self, provider: AkShareProvider) -> None:
        df_multi = pd.DataFrame({
            "公告日期": ["2024-01-01", "2023-01-01", "2022-01-01"],
            "分红年度": ["20231231", "20221231", "20211231"],
            "每股派息(税前)": [0.5, 0.4, 0.3],
            "每股派息(税后)": [0.45, 0.36, 0.27],
        })
        with patch("app.data.akshare_provider.ak") as mock_ak:
            mock_ak.stock_history_dividend_detail.return_value = df_multi
            result = provider.get_dividend(
                "000001.SZ",
                start_date="20220101",
                end_date="20231231",
            )

        # Should only include end_date in [20221231, 20231231]
        assert len(result) == 2
        assert "20211231" not in result["end_date"].values

    def test_empty_response_returns_empty(self, provider: AkShareProvider) -> None:
        with patch("app.data.akshare_provider.ak") as mock_ak:
            mock_ak.stock_history_dividend_detail.return_value = pd.DataFrame()
            result = provider.get_dividend("000001.SZ")
        assert result.empty

    def test_api_error_returns_empty(self, provider: AkShareProvider) -> None:
        """AkShare 分红接口失败应优雅降级返回空 DataFrame，不抛出异常"""
        with patch("app.data.akshare_provider.ak") as mock_ak:
            mock_ak.stock_history_dividend_detail.side_effect = Exception("network error")
            result = provider.get_dividend("000001.SZ")
        assert result.empty


# ─────────────────────────────────────────────────────────────────────────────
# get_dividend_batch
# ─────────────────────────────────────────────────────────────────────────────


def test_get_dividend_batch_returns_empty(provider: AkShareProvider) -> None:
    """全市场批量接口不可用，应始终返回空 DataFrame"""
    result = provider.get_dividend_batch("20241231")
    assert isinstance(result, pd.DataFrame)
    assert result.empty


# ─────────────────────────────────────────────────────────────────────────────
# get_trade_calendar
# ─────────────────────────────────────────────────────────────────────────────


class TestGetTradeCalendar:
    def test_date_range_filter(self, provider: AkShareProvider) -> None:
        mock_df = pd.DataFrame({
            "trade_date": ["2025-01-01", "2025-01-02", "2025-01-03",
                           "2025-01-06", "2025-01-07"],
        })
        with patch("app.data.akshare_provider.ak") as mock_ak:
            mock_ak.tool_trade_date_hist_sina.return_value = mock_df
            result = provider.get_trade_calendar("20250101", "20250105")

        # 20250101–20250105 范围内有 3 个日期
        assert len(result) == 3
        assert all(isinstance(d, date) for d in result)
        assert date(2025, 1, 6) not in result

    def test_empty_response_returns_empty_list(self, provider: AkShareProvider) -> None:
        with patch("app.data.akshare_provider.ak") as mock_ak:
            mock_ak.tool_trade_date_hist_sina.return_value = pd.DataFrame()
            result = provider.get_trade_calendar("20250101", "20250131")
        assert result == []

    def test_api_error_raises(self, provider: AkShareProvider) -> None:
        with patch("app.data.akshare_provider.ak") as mock_ak:
            mock_ak.tool_trade_date_hist_sina.side_effect = Exception("error")
            with pytest.raises(DataProviderError):
                provider.get_trade_calendar("20250101", "20250131")


# ─────────────────────────────────────────────────────────────────────────────
# provider 不可用时应抛出异常
# ─────────────────────────────────────────────────────────────────────────────


def test_not_available_raises() -> None:
    """akshare 未安装时构造 AkShareProvider 应抛出 DataProviderError"""
    with (
        patch("app.data.akshare_provider._AKSHARE_AVAILABLE", False),
        pytest.raises(DataProviderError, match="akshare 未安装"),
    ):
        AkShareProvider()


# ─────────────────────────────────────────────────────────────────────────────
# 字段映射字典完整性检查
# ─────────────────────────────────────────────────────────────────────────────


def test_field_mapping_dict_has_key_entries() -> None:
    """_AKSHARE_TO_TUSHARE_FIELDS 应包含关键中文列名"""
    required_keys = [
        "代码", "最新价", "今开", "成交额", "总市值",
        "市盈率(TTM)", "股息率", "报告期",
    ]
    for key in required_keys:
        assert key in _AKSHARE_TO_TUSHARE_FIELDS, f"缺少字段映射: '{key}'"


def test_field_mapping_values_are_english() -> None:
    """所有映射目标值（Tushare 风格）应为 ASCII 英文"""
    for cn_key, en_val in _AKSHARE_TO_TUSHARE_FIELDS.items():
        assert en_val.isascii(), (
            f"映射值 '{en_val}'（源字段 '{cn_key}'）应为 ASCII 英文字段名"
        )
