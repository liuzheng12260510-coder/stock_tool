"""
数据提供者协议定义 — Protocol 接口，不依赖具体实现
"""
from __future__ import annotations

from datetime import date
from typing import Protocol

import pandas as pd


class MarketDataProvider(Protocol):
    """市场数据提供者协议"""

    def get_stock_basic(self) -> pd.DataFrame:
        """
        获取全市场股票基础信息
        返回列：ts_code, name, industry, exchange, list_date, act_ent_type, act_name
        """
        ...

    def get_daily(self, trade_date: str) -> pd.DataFrame:
        """
        获取指定交易日全市场行情
        trade_date: YYYYMMDD 格式
        返回列：ts_code, open, high, low, close, pct_chg, vol, amount
        """
        ...

    def get_daily_basic(self, trade_date: str) -> pd.DataFrame:
        """
        获取指定交易日估值数据
        返回列：ts_code, pe_ttm, pb, dv_ttm, total_mv, circ_mv
        """
        ...

    def get_financial_quarterly(
        self,
        ts_code: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> pd.DataFrame:
        """
        获取季度财务指标（单股或全市场某报告期）
        返回列：ts_code, end_date, q_dtprofit, total_cur_assets, total_liab, op_revenue, ann_date
        """
        ...

    def get_trade_calendar(self, start_date: str, end_date: str) -> list[date]:
        """
        获取交易日历
        返回指定范围内的交易日列表
        """
        ...

    def get_company_info(self, ts_code: str) -> dict:
        """
        获取公司信息（民企识别用）
        返回：{act_ent_type, act_name, ...}
        """
        ...
