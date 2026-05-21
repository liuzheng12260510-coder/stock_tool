"""
复合数据提供者 — Tushare 主用，AkShare 自动兜底

P0 变更：
- 感知熔断器状态：BreakerOpenError / TushareQuotaError → 直接走 fallback，
  避免回调已熔断的主源浪费时间
- 兜底 3 个高价值接口：daily / daily_basic / 财务（通过 AkShare 的 get_financial_quarterly）

P1 变更：
- 新增 get_dividend(ts_code, start_date, end_date) 代理方法（带 AkShare 兜底）
- 新增 get_dividend_batch(end_date) 代理方法（仅 Tushare，无兜底，失败返回空）
"""
from __future__ import annotations

from datetime import date
from typing import Any

import pandas as pd

from app.core.exceptions import BreakerOpenError, DataProviderError, TushareQuotaError
from app.core.logging import get_logger
from app.data.akshare_provider import AkShareProvider
from app.data.tushare_provider import TushareProvider

logger = get_logger("composite_provider")

# 异常类型：命中这些异常时直接走 fallback，不再重试主源
_DIRECT_FALLBACK_EXCEPTIONS = (BreakerOpenError, TushareQuotaError)


class CompositeProvider:
    """
    主备切换策略：
    1. 优先调用 Tushare
       - 若熔断器 OPEN（BreakerOpenError）→ 直接走 fallback，不浪费时间
       - 若积分不足（TushareQuotaError）→ 直接走 fallback
       - 其他 DataProviderError → 尝试 fallback
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
        """
        通用主备切换调用

        BreakerOpenError / TushareQuotaError → 立即走 fallback（不打 error 日志，属预期行为）
        其他 DataProviderError → 尝试 fallback
        两者都失败 → 抛主源异常
        """
        primary_err: Exception | None = None
        try:
            return getattr(self._primary, method)(*args, **kwargs)
        except _DIRECT_FALLBACK_EXCEPTIONS as e:
            # 熔断/积分不足 — 不写 error，直接切换（属于预期降级路径）
            logger.info(
                "composite.direct_fallback",
                method=method,
                reason=type(e).__name__,
                detail=str(e)[:120],
            )
            primary_err = e
        except DataProviderError as e:
            logger.warning(
                "Tushare 失败，切换到 AkShare 兜底",
                method=method,
                error=str(e)[:120],
            )
            primary_err = e

        # 如果没有 fallback，直接重新抛主源异常
        if self._fallback is None:
            raise primary_err  # type: ignore[misc]

        try:
            result = getattr(self._fallback, method)(*args, **kwargs)
            logger.info(
                "composite.fallback_success",
                method=method,
            )
            return result
        except DataProviderError as fallback_err:
            logger.error(
                "主备均失败",
                method=method,
                primary_error=str(primary_err)[:100],
                fallback_error=str(fallback_err)[:100],
            )
            raise primary_err from None  # type: ignore[misc]  # 抛出主源异常（更有诊断价值）

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

    def get_dividend(
        self,
        ts_code: str,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> pd.DataFrame:
        """单股分红历史（带 AkShare 兜底）"""
        return self._with_fallback("get_dividend", ts_code, start_date, end_date)

    def get_dividend_batch(self, end_date: str) -> pd.DataFrame:
        """
        全市场年度分红批量拉取（仅 Tushare，无 AkShare 兜底）

        失败时返回空 DataFrame，而非抛出异常（分红数据非核心强依赖）。
        """
        try:
            return self._primary.get_dividend_batch(end_date)
        except Exception as e:
            logger.warning(
                "composite.dividend_batch_failed",
                end_date=end_date,
                error=str(e)[:120],
            )
            return pd.DataFrame()

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
