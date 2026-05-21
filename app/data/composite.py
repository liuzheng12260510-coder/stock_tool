"""
复合数据提供者 — Tushare 主用，AkShare 自动兜底
"""
from __future__ import annotations

from datetime import date
from typing import Any

import pandas as pd

from app.core.exceptions import DataProviderError
from app.core.logging import get_logger
from app.data.akshare_provider import AkShareProvider
from app.data.tushare_provider import TushareProvider

logger = get_logger("composite_provider")


class CompositeProvider:
    """
    主备切换策略：
    1. 优先调用 Tushare
    2. Tushare 失败 → 尝试 AkShare 兜底
    3. 两者都失败 → 抛出最后一个异常
    """

    def __init__(self) -> None:
        self._primary = TushareProvider()
        try:
            self._fallback: AkShareProvider | None = AkShareProvider()
        except DataProviderError:
            self._fallback = None
            logger.warning("AkShare 兜底不可用，仅使用 Tushare")

    def _with_fallback(self, method: str, *args: Any, **kwargs: Any) -> Any:
        """通用主备切换调用"""
        try:
            return getattr(self._primary, method)(*args, **kwargs)
        except DataProviderError as primary_err:
            if self._fallback is None:
                raise
            logger.warning(
                "Tushare 失败，切换到 AkShare 兜底",
                method=method,
                error=str(primary_err),
            )
            try:
                return getattr(self._fallback, method)(*args, **kwargs)
            except DataProviderError as fallback_err:
                logger.error(
                    "主备均失败",
                    method=method,
                    primary_error=str(primary_err),
                    fallback_error=str(fallback_err),
                )
                raise primary_err  # 抛出主源异常

    def get_stock_basic(self) -> pd.DataFrame:
        return self._with_fallback("get_stock_basic")

    def get_daily(self, trade_date: str) -> pd.DataFrame:
        return self._with_fallback("get_daily", trade_date)

    def get_daily_basic(self, trade_date: str) -> pd.DataFrame:
        return self._with_fallback("get_daily_basic", trade_date)

    def get_financial_quarterly(
        self,
        ts_code: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        period: str | None = None,
        ts_codes: list[str] | None = None,
    ) -> pd.DataFrame:
        return self._with_fallback(
            "get_financial_quarterly",
            ts_code=ts_code,
            start_date=start_date,
            end_date=end_date,
            period=period,
            ts_codes=ts_codes,
        )

    def get_trade_calendar(self, start_date: str, end_date: str) -> list[date]:
        return self._with_fallback("get_trade_calendar", start_date, end_date)

    def get_company_info(self, ts_code: str) -> dict:
        return self._with_fallback("get_company_info", ts_code)

    # 仅 Tushare 支持的接口（无兜底）
    def get_company_info_batch(self) -> pd.DataFrame:
        return self._primary.get_company_info_batch()

    def get_latest_trade_date(self) -> date:
        return self._primary.get_latest_trade_date()

    def get_price_history(self, ts_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        return self._primary.get_price_history(ts_code, start_date, end_date)
