"""
Tushare 数据提供者 — 全市场批量接口优先，令牌桶限速，tenacity 重试，熔断器保护

P0 变更：
- 接入 Circuit Breaker（daily / daily_basic / fina_indicator_vip 三个高频接口）
- 精确识别 TushareQuotaError（积分/权限不足）→ 熔断器 QUOTA 类型
- enable_fina_loop_rescue=False（默认）时，VIP 失败直接降级到 AkShare 或返回空

P1 变更：
- _FINA_FIELDS 扩充 14 个字段（杜邦/盈余质量/同比增速）
- 新增 get_dividend(ts_code, start_date, end_date) 单股分红历史
- 新增 get_dividend_batch(end_date) 全市场年度分红横向切片
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
from app.core.exceptions import (
    BreakerOpenError,
    DataProviderError,
    NoDataError,
    RateLimitError,
    TushareQuotaError,
)
from app.core.logging import get_logger
from app.core.ratelimit import get_tushare_limiter
from app.data.breaker import ErrorType, classify_tushare_error, get_breaker

logger = get_logger("tushare_provider")

# 接入熔断器的接口列表（provider="tushare"）
_BREAKERED_ENDPOINTS = frozenset({
    "daily",
    "daily_basic",
    "fina_indicator_vip",
})

# P1：扩充财务字段（兼容非VIP和VIP接口）
# 包含：基础字段 + 杜邦三要素 + 同比增速 + 盈余质量 + EPS
_FINA_FIELDS = (
    "ts_code,ann_date,end_date"
    ",q_dtprofit,op_revenue,total_cur_assets,total_liab"
    ",roe,npta,debt_to_assets,assets_turn,eqt_multiplier"
    ",netprofit_yoy,op_yoy"
    ",n_cashflow_act,ocf_to_profit"
    ",eps,dt_eps"
)

# 分红接口字段
_DIVIDEND_FIELDS = (
    "ts_code,ann_date,end_date,div_proc"
    ",stk_div,cash_div,cash_div_tax"
    ",base_date,pay_date,record_date,ex_date"
)


class TushareProvider:
    """
    封装 Tushare Pro API，特点：
    - 全市场批量接口优先（daily/daily_basic 一次拉全市场）
    - 令牌桶限速器控制调用频率
    - tenacity 自动重试（最多 3 次，指数退避）
    - Circuit Breaker 保护高频/高消耗接口
    """

    def __init__(self) -> None:
        self._api = ts.pro_api(settings.tushare_token)
        self._limiter = get_tushare_limiter(settings.tushare_rate_limit)
        # 初始化受保护接口的熔断器（使用全局配置阈值）
        for ep in _BREAKERED_ENDPOINTS:
            get_breaker(
                "tushare",
                ep,
                failure_threshold=settings.breaker_failure_threshold,
                cooldown_seconds=settings.breaker_cooldown_seconds,
            )
        logger.info("TushareProvider 初始化完成", rate_limit=settings.tushare_rate_limit)

    # ── 内部通用调用封装 ──────────────────────────────────────────────────

    def _call(self, func_name: str, **kwargs: Any) -> pd.DataFrame:
        """
        调用 Tushare API 通用入口：
        1. 令牌桶 acquire（阻塞等待）
        2. 调用 API
        3. 精确分类异常（Quota / RateLimit / Network）
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
            error_type = classify_tushare_error(err_msg)

            if error_type == ErrorType.RATE_LIMIT:
                logger.warning("触发 Tushare 流量限制，退避重试", func=func_name)
                time.sleep(10)
                raise RateLimitError(err_msg) from e

            if error_type == ErrorType.QUOTA:
                logger.warning(
                    "Tushare 积分/权限不足",
                    func=func_name,
                    error=err_msg[:120],
                )
                raise TushareQuotaError(f"Tushare 积分/权限不足 [{func_name}]: {err_msg}") from e

            raise DataProviderError(f"Tushare API 调用失败 [{func_name}]: {err_msg}") from e

    @retry(
        retry=retry_if_exception_type(RateLimitError),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, min=10, max=60),
        reraise=True,
    )
    def _call_with_retry(self, func_name: str, **kwargs: Any) -> pd.DataFrame:
        return self._call(func_name, **kwargs)

    def _call_with_breaker(self, func_name: str, **kwargs: Any) -> pd.DataFrame:
        """
        带熔断器的调用入口（用于 _BREAKERED_ENDPOINTS 中的接口）

        流程：
        1. 检查熔断器状态，OPEN 状态抛 BreakerOpenError
        2. 调用并记录成功/失败
        3. 根据异常类型上报给熔断器
        """
        breaker = get_breaker(
            "tushare",
            func_name,
            failure_threshold=settings.breaker_failure_threshold,
            cooldown_seconds=settings.breaker_cooldown_seconds,
        )

        if not breaker.allow():
            status = breaker.status()
            logger.warning(
                "breaker.rejected",
                provider="tushare",
                endpoint=func_name,
                open_until=status.open_until.isoformat() if status.open_until else None,
            )
            raise BreakerOpenError(
                f"熔断器 OPEN，跳过 tushare.{func_name}，"
                f"恢复时间: {status.open_until}"
            )

        try:
            result = self._call_with_retry(func_name, **kwargs)
            breaker.record_success()
            return result
        except TushareQuotaError as e:
            breaker.record_failure(ErrorType.QUOTA, str(e))
            raise
        except RateLimitError as e:
            breaker.record_failure(ErrorType.RATE_LIMIT, str(e))
            raise
        except DataProviderError as e:
            err_type = classify_tushare_error(str(e))
            breaker.record_failure(err_type, str(e))
            raise

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
        """全市场日行情（一次请求）—— 熔断器保护"""
        logger.info("拉取日行情", trade_date=trade_date)
        df = self._call_with_breaker(
            "daily",
            trade_date=trade_date,
            fields="ts_code,trade_date,open,high,low,close,pct_chg,vol,amount",
        )
        logger.info("日行情拉取完成", trade_date=trade_date, count=len(df))
        return df

    def get_daily_basic(self, trade_date: str) -> pd.DataFrame:
        """全市场估值数据（一次请求）—— 熔断器保护"""
        logger.info("拉取估值数据", trade_date=trade_date)
        df = self._call_with_breaker(
            "daily_basic",
            trade_date=trade_date,
            fields="ts_code,trade_date,pe_ttm,pb,dv_ttm,total_mv,circ_mv",
        )
        logger.info("估值数据拉取完成", trade_date=trade_date, count=len(df))
        return df

    def get_financial_quarterly(
        self,
        ts_code: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        period: str | None = None,
        ts_codes: list[str] | None = None,
    ) -> pd.DataFrame:
        """
        季度财务指标（P1：扩充 14 个新字段，含杜邦/盈余质量/同比增速）

        优先级（全市场场景 period 模式）：
          1. fina_indicator_vip（VIP 接口，一次全市场）
          2. fina_indicator（逐只兜底，需 enable_fina_loop_rescue=True）
          3. 单股场景（ts_code 参数）直接用 fina_indicator
        """
        kwargs: dict[str, Any] = {"fields": _FINA_FIELDS}
        if ts_code:
            kwargs["ts_code"] = ts_code
        if period:
            kwargs["period"] = period
        if start_date:
            kwargs["start_date"] = start_date
        if end_date:
            kwargs["end_date"] = end_date

        # ── 1. 单股场景：直接用 fina_indicator ──────────────────────────
        if ts_code:
            return self._call_with_retry("fina_indicator", **kwargs)

        # ── 2. 全市场场景：尝试 VIP 接口（带熔断器）────────────────────
        try:
            df = self._call_with_breaker("fina_indicator_vip", **kwargs)
            return df
        except BreakerOpenError:
            logger.warning(
                "fina_indicator_vip 熔断器 OPEN，降级处理",
                period=period,
            )
        except TushareQuotaError as e:
            logger.warning(
                "fina_indicator_vip 积分/权限不足，降级处理",
                period=period,
                reason=str(e)[:120],
            )
        except DataProviderError as e:
            logger.warning(
                "fina_indicator_vip 不可用，降级处理",
                period=period,
                reason=str(e)[:120],
            )

        # ── 3. VIP 失败后的降级路径 ──────────────────────────────────────
        if not settings.enable_fina_loop_rescue:
            logger.warning(
                "fina_indicator_vip 失败，enable_fina_loop_rescue=False，"
                "跳过当期财务数据（可设置 ENABLE_FINA_LOOP_RESCUE=true 开启逐只兜底）",
                period=period,
            )
            return pd.DataFrame()

        # ── 4. 安全阀：逐只兜底（需要 enable_fina_loop_rescue=True）──────
        if not ts_codes:
            logger.warning(
                "enable_fina_loop_rescue=True 但未提供 ts_codes，季度财务跳过",
                period=period,
            )
            return pd.DataFrame()

        logger.warning(
            "启用逐只兜底拉取 fina_indicator（高积分消耗模式）",
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

        ⚠️ 此方法仅在 enable_fina_loop_rescue=True 时被调用（高积分消耗）。
        """
        frames: list[pd.DataFrame] = []
        for i, code in enumerate(ts_codes, 1):
            try:
                kwargs: dict[str, Any] = {
                    "ts_code": code,
                    "fields": _FINA_FIELDS,
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

    def get_dividend(
        self,
        ts_code: str,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> pd.DataFrame:
        """
        单股分红历史

        Args:
            ts_code:    股票代码（如 "000001.SZ"）
            start_date: 起始日期（YYYYMMDD，基于 ann_date 过滤）
            end_date:   截止日期（YYYYMMDD，基于 ann_date 过滤）

        Returns:
            分红记录 DataFrame
        """
        logger.debug("拉取单股分红历史", ts_code=ts_code)
        kwargs: dict[str, Any] = {
            "ts_code": ts_code,
            "fields": _DIVIDEND_FIELDS,
        }
        if start_date:
            kwargs["start_date"] = start_date
        if end_date:
            kwargs["end_date"] = end_date
        try:
            df = self._call_with_retry("dividend", **kwargs)
        except DataProviderError as e:
            logger.warning("单股分红历史拉取失败", ts_code=ts_code, error=str(e)[:120])
            return pd.DataFrame()
        return df

    def get_dividend_batch(self, end_date: str) -> pd.DataFrame:
        """
        一次拉某年度全市场分红（横向切片，基于 end_date 过滤）

        用于 Pipeline Step 6.5：按年度批量拉取 5 年分红历史。
        每次调用一个年度，共 5 次，远比逐只调用高效。

        Args:
            end_date: 财年截止日期（YYYYMMDD，如 "20231231"）

        Returns:
            该年度所有股票的分红记录 DataFrame
        """
        logger.info("拉取年度全市场分红", end_date=end_date)
        try:
            df = self._call_with_retry(
                "dividend",
                end_date=end_date,
                fields=_DIVIDEND_FIELDS,
            )
        except DataProviderError as e:
            logger.warning("年度分红批量拉取失败", end_date=end_date, error=str(e)[:120])
            return pd.DataFrame()
        logger.info("年度分红拉取完成", end_date=end_date, count=len(df))
        return df

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
        import datetime as dt_module  # noqa: PLC0415
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
