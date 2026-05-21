"""
AkShare 兜底数据提供者 — 当 Tushare 不可用或积分不足时使用
"""
from __future__ import annotations

from datetime import date, datetime

import pandas as pd

from app.core.exceptions import DataProviderError, NoDataError
from app.core.logging import get_logger

logger = get_logger("akshare_provider")

try:
    import akshare as ak  # type: ignore[import-untyped]
    _AKSHARE_AVAILABLE = True
except ImportError:
    ak = None  # type: ignore[assignment]
    _AKSHARE_AVAILABLE = False
    logger.warning("akshare 未安装，兜底 provider 不可用")


class AkShareProvider:
    """
    AkShare 兜底实现，覆盖 Tushare 部分接口
    注意：AkShare 字段名与 Tushare 不同，需做映射
    """

    def __init__(self) -> None:
        if not _AKSHARE_AVAILABLE:
            raise DataProviderError("akshare 未安装")
        logger.info("AkShareProvider 初始化完成")

    def get_stock_basic(self) -> pd.DataFrame:
        """A 股上市公司基础信息（映射到 Tushare 格式）"""
        try:
            df = ak.stock_info_a_code_name()  # type: ignore[union-attr]
            # 字段映射
            df = df.rename(columns={"code": "ts_code", "name": "name"})
            # 补全缺失字段
            df["industry"] = ""
            df["exchange"] = df["ts_code"].apply(
                lambda x: "SSE" if str(x).startswith("6") else "SZSE"
            )
            df["list_date"] = None
            df["act_ent_type"] = ""
            df["act_name"] = ""
            return df
        except Exception as e:
            raise DataProviderError(f"AkShare get_stock_basic 失败: {e}") from e

    def get_daily(self, trade_date: str) -> pd.DataFrame:
        """
        日行情 — AkShare 不提供全市场单日接口，
        此方法作为 fallback 仅用于紧急补数
        """
        logger.warning("AkShare get_daily 不支持全市场批量，仅供单股补数")
        return pd.DataFrame()

    def get_daily_basic(self, trade_date: str) -> pd.DataFrame:
        """估值数据 — AkShare 兜底"""
        try:
            df = ak.stock_a_lg_indicator(symbol="all")  # type: ignore[union-attr, attr-defined]
            if df.empty:
                return pd.DataFrame()
            # 字段映射
            rename_map = {
                "代码": "ts_code",
                "市盈率(TTM)": "pe_ttm",
                "市净率": "pb",
                "股息率": "dv_ttm",
                "总市值": "total_mv",
                "流通市值": "circ_mv",
            }
            df = df.rename(columns={k: v for k, v in rename_map.items() if k in df.columns})
            df["trade_date"] = trade_date
            # 总市值 AkShare 单位为亿元，转为万元
            if "total_mv" in df.columns:
                df["total_mv"] = pd.to_numeric(df["total_mv"], errors="coerce") * 10000
            if "circ_mv" in df.columns:
                df["circ_mv"] = pd.to_numeric(df["circ_mv"], errors="coerce") * 10000
            return df
        except Exception as e:
            raise DataProviderError(f"AkShare get_daily_basic 失败: {e}") from e

    def get_financial_quarterly(
        self,
        ts_code: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        period: str | None = None,
    ) -> pd.DataFrame:
        """季度财务数据兜底（单股）"""
        if not ts_code:
            logger.warning("AkShare 不支持全市场财务批量，跳过")
            return pd.DataFrame()
        try:
            symbol = ts_code.split(".")[0]
            df = ak.stock_financial_report_sina(stock=symbol, symbol="利润表")  # type: ignore[union-attr]
            if df.empty:
                return pd.DataFrame()
            # 简化处理：只提取扣非净利润近似值
            df_out = pd.DataFrame()
            df_out["ts_code"] = ts_code
            df_out["end_date"] = pd.to_datetime(df.index if df.index.name else df.columns[0])
            df_out["q_dtprofit"] = 0.0  # AkShare 无直接单季扣非字段
            df_out["source"] = "akshare"
            return df_out
        except Exception as e:
            raise DataProviderError(f"AkShare get_financial_quarterly 失败: {e}") from e

    def get_trade_calendar(self, start_date: str, end_date: str) -> list[date]:
        """交易日历"""
        try:
            df = ak.tool_trade_date_hist_sina()  # type: ignore[union-attr]
            dates = pd.to_datetime(df["trade_date"]).dt.date.tolist()
            start = datetime.strptime(start_date, "%Y%m%d").date()
            end = datetime.strptime(end_date, "%Y%m%d").date()
            return [d for d in dates if start <= d <= end]
        except Exception as e:
            raise DataProviderError(f"AkShare get_trade_calendar 失败: {e}") from e

    def get_company_info(self, ts_code: str) -> dict:
        """公司信息（AkShare 无实际控制人接口，返回空）"""
        logger.warning("AkShare 不提供实际控制人信息", ts_code=ts_code)
        return {"ts_code": ts_code, "act_ent_type": "", "act_name": ""}
