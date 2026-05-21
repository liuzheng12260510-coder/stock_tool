"""
Tushare 数据提供者 — 全市场批量接口优先，令牌桶限速，tenacity 重试
"""
from __future__ import annotations

import time
from datetime import date, datetime
from typing import Any

import pandas as pd
import tushare as ts
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from app.core.config import settings
from app.core.exceptions import DataProviderError, NoDataError, RateLimitError
from app.core.logging import get_logger
from app.core.ratelimit import get_tushare_limiter

logger = get_logger("tushare_provider")


class TushareProvider:
    """
    封装 Tushare Pro API，特点：
    - 全市场批量接口优先（daily/daily_basic 一次拉全市场）
    - 令牌桶限速器控制调用频率
    - tenacity 自动重试（最多 3 次，指数退避）
    """

    def __init__(self) -> None:
        self._api = ts.pro_api(settings.tushare_token)
        self._limiter = get_tushare_limiter(settings.tushare_rate_limit)
        logger.info("TushareProvider 初始化完成", rate_limit=settings.tushare_rate_limit)

    # ── 内部通用调用封装 ──────────────────────────────────────────────────

    def _call(self, func_name: str, **kwargs: Any) -> pd.DataFrame:
        """
        调用 Tushare API 通用入口：
        1. 令牌桶 acquire（阻塞等待）
        2. 调用 API
        3. 429 / 流量限制时抛 RateLimitError 触发重试
        """
        if not self._limiter.acquire(timeout=120):
            raise RateLimitError(f"Tushare 限速器超时，无法获取令牌 ({func_name})")

        try:
            func = getattr(self._api, func_name)
            df: pd.DataFrame = func(**kwargs)
            if df is None:
                df = pd.DataFrame()
            return df
        except Exception as e:
            err_msg = str(e)
            if "每分钟最多访问" in err_msg or "抱歉，您每分钟最多访问" in err_msg:
                logger.warning("触发 Tushare 流量限制，退避重试", func=func_name)
                time.sleep(10)
                raise RateLimitError(err_msg)
            raise DataProviderError(f"Tushare API 调用失败 [{func_name}]: {err_msg}") from e

    @retry(
        retry=retry_if_exception_type(RateLimitError),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, min=10, max=60),
        reraise=True,
    )
    def _call_with_retry(self, func_name: str, **kwargs: Any) -> pd.DataFrame:
        return self._call(func_name, **kwargs)

    # ── 公共接口 ─────────────────────────────────────────────────────────

    def get_stock_basic(self) -> pd.DataFrame:
        """全市场股票基础信息（含上交所+深交所，北交所按配置排除）"""
        logger.info("拉取全市场股票列表...")
        fields = "ts_code,symbol,name,area,industry,market,exchange,list_date,is_hs"
        df = self._call_with_retry(
            "stock_basic",
            exchange="",
            list_status="L",
            fields=fields,
        )
        logger.info("股票列表拉取完成", count=len(df))
        return df

    def get_daily(self, trade_date: str) -> pd.DataFrame:
        """全市场日行情（一次请求）"""
        logger.info("拉取日行情", trade_date=trade_date)
        df = self._call_with_retry(
            "daily",
            trade_date=trade_date,
            fields="ts_code,trade_date,open,high,low,close,pct_chg,vol,amount",
        )
        logger.info("日行情拉取完成", trade_date=trade_date, count=len(df))
        return df

    def get_daily_basic(self, trade_date: str) -> pd.DataFrame:
        """全市场估值数据（一次请求）"""
        logger.info("拉取估值数据", trade_date=trade_date)
        df = self._call_with_retry(
            "daily_basic",
            trade_date=trade_date,
            fields="ts_code,trade_date,pe_ttm,pb,dv_ttm,total_mv,circ_mv",
        )
        logger.info("估值数据拉取完成", trade_date=trade_date, count=len(df))
        return df

    # 财务接口字段（fina_indicator 和 fina_indicator_vip 共用）
    _FINA_FIELDS = "ts_code,ann_date,end_date,q_dtprofit,op_revenue"

    def get_financial_quarterly(
        self,
        ts_code: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        period: str | None = None,
        ts_codes: list[str] | None = None,
    ) -> pd.DataFrame:
        """
        季度财务指标
        - period='20240930' → 优先尝试 fina_indicator_vip 全市场一次拉取
          vip 接口失败（权限不足）时：
            - 若提供了 ts_codes → 逐只调用 fina_indicator 兜底
            - 否则返回空 DataFrame
        - ts_code='600519.SH' → 拉单股历史（直接用 fina_indicator）
        """
        kwargs: dict[str, Any] = {"fields": self._FINA_FIELDS}
        if ts_code:
            kwargs["ts_code"] = ts_code
        if period:
            kwargs["period"] = period
        if start_date:
            kwargs["start_date"] = start_date
        if end_date:
            kwargs["end_date"] = end_date

        # ── 1. 尝试 vip 接口（一次全市场）──────────────────────────────
        try:
            df = self._call_with_retry("fina_indicator_vip", **kwargs)
            return df
        except DataProviderError as e:
            logger.warning(
                "fina_indicator_vip 不可用，降级到 fina_indicator",
                ts_code=ts_code,
                period=period,
                reason=str(e)[:120],
            )

        # ── 2. 单股场景：直接用 fina_indicator ──────────────────────────
        if ts_code:
            return self._call_with_retry("fina_indicator", **kwargs)

        # ── 3. 全市场场景：逐只兜底（需要 ts_codes 列表）────────────────
        if not ts_codes:
            logger.warning(
                "fina_indicator_vip 不可用且未提供 ts_codes，季度财务跳过",
                period=period,
            )
            return pd.DataFrame()

        logger.warning(
            "启用逐只兜底拉取 fina_indicator",
            period=period,
            total=len(ts_codes),
        )
        return self._loop_fina_indicator(ts_codes, period=period)

    def _loop_fina_indicator(
        self,
        ts_codes: list[str],
        period: str | None = None,
    ) -> pd.DataFrame:
        """
        逐只调用 fina_indicator，拼合成全市场 DataFrame。
        受限速控制，约 180 只/分钟，5500 只需约 30 分钟/报告期。
        """
        frames: list[pd.DataFrame] = []
        for i, code in enumerate(ts_codes, 1):
            try:
                kwargs: dict[str, Any] = {
                    "ts_code": code,
                    "fields": self._FINA_FIELDS,
                }
                if period:
                    kwargs["period"] = period
                df = self._call_with_retry("fina_indicator", **kwargs)
                if not df.empty:
                    frames.append(df)
            except DataProviderError as e:
                logger.warning(
                    "单股财务拉取失败，跳过",
                    ts_code=code,
                    error=str(e)[:100],
                )
            if i % 200 == 0:
                logger.info(
                    "逐只财务拉取进度",
                    done=i,
                    total=len(ts_codes),
                    period=period,
                )
        if not frames:
            return pd.DataFrame()
        return pd.concat(frames, ignore_index=True)

    def get_trade_calendar(self, start_date: str, end_date: str) -> list[date]:
        """交易日历"""
        df = self._call_with_retry(
            "trade_cal",
            exchange="SSE",
            start_date=start_date,
            end_date=end_date,
            is_open="1",
            fields="cal_date",
        )
        if df.empty:
            return []
        return [
            datetime.strptime(str(d), "%Y%m%d").date()
            for d in df["cal_date"].tolist()
        ]

    def get_latest_trade_date(self) -> date:
        """获取最近的交易日"""
        import datetime as dt_module
        today = datetime.now()
        start = (today - dt_module.timedelta(days=14)).strftime("%Y%m%d")
        end = today.strftime("%Y%m%d")
        dates = self.get_trade_calendar(start_date=start, end_date=end)
        if not dates:
            raise NoDataError("无法获取最近交易日")
        return max(dates)

    def get_company_info(self, ts_code: str) -> dict:
        """公司基本信息（含实际控制人类型）"""
        df = self._call_with_retry(
            "stock_company",
            ts_code=ts_code,
            fields="ts_code,act_ent_type,act_name",
        )
        if df.empty:
            return {}
        return df.iloc[0].to_dict()

    def get_company_info_batch(self) -> pd.DataFrame:
        """
        批量拉取所有公司信息（用于初始化或周更新民企字段）
        注意：此接口每次只能拉全量，单次限速消耗较多
        """
        logger.info("拉取全量公司信息（民企识别）...")
        df = self._call_with_retry(
            "stock_company",
            exchange="",
            fields="ts_code,act_ent_type,act_name",
        )
        logger.info("公司信息拉取完成", count=len(df))
        return df

    def get_price_history(self, ts_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        """单股历史收盘价（用于波动率计算）"""
        df = self._call_with_retry(
            "daily",
            ts_code=ts_code,
            start_date=start_date,
            end_date=end_date,
            fields="ts_code,trade_date,close,pct_chg",
        )
        return df
