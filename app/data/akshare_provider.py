"""
AkShare 兜底数据提供者 — P2 完善版

P2 变更：
- 统一字段命名映射层（_AKSHARE_TO_TUSHARE_FIELDS）
- get_stock_basic: 通过 ak.stock_info_a_code_name() 获取全量，附加 exchange/ts_code
- get_daily: 通过 ak.stock_zh_a_spot_em() 获取全市场当日行情
- get_daily_basic: 通过 ak.stock_a_lg_indicator(symbol="all") 获取估值数据
- get_financial_quarterly: 通过 ak.stock_financial_abstract(symbol) 按股票循环（慢路径）
- get_dividend / get_dividend_batch: 单股分红循环 / 全市场批量（返回空，无 AkShare 支持）
- 新增辅助函数 _code_to_ts_code、_to_float_safe
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any

import pandas as pd

from app.core.exceptions import DataProviderError
from app.core.logging import get_logger

logger = get_logger("akshare_provider")

try:
    import akshare as ak  # type: ignore[import-untyped]
    _AKSHARE_AVAILABLE = True
except ImportError:
    ak = None  # type: ignore[assignment]
    _AKSHARE_AVAILABLE = False
    logger.warning("akshare 未安装，兜底 provider 不可用")


# ── 字段映射字典 ─────────────────────────────────────────────────────────────
# AkShare 中文字段名 → Tushare 风格英文字段名
_AKSHARE_TO_TUSHARE_FIELDS: dict[str, str] = {
    # stock_zh_a_spot_em 全市场行情
    "代码":   "ts_code_raw",
    "名称":   "name",
    "最新价": "close",
    "今开":   "open",
    "最高":   "high",
    "最低":   "low",
    "昨收":   "pre_close",
    "涨跌幅": "pct_chg",
    "涨跌额": "change",
    "成交量": "vol",       # 手
    "成交额": "amount_raw",  # 元，后续转换为千元
    "振幅":   "amplitude",
    "量比":   "volume_ratio",
    "换手率": "turnover_rate",
    "市盈率-动态": "pe_ttm",
    "市净率": "pb",
    "总市值": "total_mv_raw",   # 元，后续转换为万元
    "流通市值": "circ_mv_raw",  # 元
    # stock_a_lg_indicator 估值
    "市盈率(TTM)": "pe_ttm",
    "股息率":      "dv_ttm",
    # stock_financial_abstract 财务摘要
    "报告期":             "end_date",
    "净利润":             "net_profit",
    "净利润增长率":        "netprofit_yoy",
    "扣除非经常损益后的净利润": "q_dtprofit_cum",
    "营业收入":           "op_revenue",
    "营业收入增长率":      "op_yoy",
    "净资产收益率":        "roe",
    "每股收益":           "eps",
    "每股净资产":         "bps",
    "每股经营活动产生的现金流量": "cfps",
}


def _code_to_ts_code(code: str) -> str:
    """
    将 6 位纯数字代码转换为 Tushare 风格 ts_code（含交易所后缀）

    Rules:
      60xxxx → .SH（上交所主板）
      688xxx → .SH（科创板）
      4xxxxx / 8xxxxx → .BJ（北交所）
      其余    → .SZ（深交所）
    """
    code = str(code).strip().lstrip("0").zfill(6)
    if code.startswith("6"):
        return f"{code}.SH"
    if code.startswith(("4", "8")):
        return f"{code}.BJ"
    return f"{code}.SZ"


def _to_float_safe(v: Any) -> float | None:
    """安全转换为 float，NaN/Inf/None → None"""
    if v is None:
        return None
    try:
        import math  # noqa: PLC0415
        f = float(v)
        return None if math.isnan(f) or math.isinf(f) else f
    except (TypeError, ValueError):
        return None


class AkShareProvider:
    """
    AkShare 兜底实现（P2 完善版）

    所有接口返回与 Tushare 同结构 DataFrame；
    字段单位统一到与 Tushare 保持一致。
    """

    def __init__(self) -> None:
        if not _AKSHARE_AVAILABLE:
            raise DataProviderError("akshare 未安装")
        logger.info("AkShareProvider 初始化完成")

    # ─────────────────────────────────────────────────────────────────────
    # get_stock_basic
    # ─────────────────────────────────────────────────────────────────────

    def get_stock_basic(self) -> pd.DataFrame:
        """
        A 股上市公司基础信息（映射到 Tushare 格式）

        Source: ak.stock_info_a_code_name()
        输出字段: ts_code, name, exchange, list_date, industry, act_ent_type, act_name
        """
        try:
            df = ak.stock_info_a_code_name()  # type: ignore[union-attr]
            if df.empty:
                return pd.DataFrame()

            # 字段映射：code → ts_code（带后缀），name 直接保留
            df = df.rename(columns={"code": "ts_code_raw"})
            df["ts_code"] = df["ts_code_raw"].apply(_code_to_ts_code)

            # 补全 Tushare 标准字段（缺失值留空）
            df["industry"] = ""
            df["exchange"] = df["ts_code"].apply(
                lambda x: "SSE" if str(x).endswith(".SH")
                else ("BSE" if str(x).endswith(".BJ") else "SZSE")
            )
            df["list_date"] = None
            df["act_ent_type"] = ""
            df["act_name"] = ""

            return df[["ts_code", "name", "exchange", "industry", "list_date",
                        "act_ent_type", "act_name"]].copy()
        except Exception as e:
            raise DataProviderError(f"AkShare get_stock_basic 失败: {e}") from e

    # ─────────────────────────────────────────────────────────────────────
    # get_daily
    # ─────────────────────────────────────────────────────────────────────

    def get_daily(self, trade_date: str) -> pd.DataFrame:
        """
        全市场日行情（当日收盘数据）

        Source: ak.stock_zh_a_spot_em() — 返回 A 股实时/收盘行情
        输出字段: ts_code, trade_date, open, high, low, close,
                  pct_chg, vol, amount, pe_ttm, pb, total_mv, circ_mv
        单位：
          vol      — 手（与 Tushare 一致）
          amount   — 千元（Tushare 标准，AkShare 原始为元，需 /1000）
          total_mv — 万元（Tushare 标准，AkShare 原始为元，需 /10000）
          circ_mv  — 万元
        """
        try:
            raw = ak.stock_zh_a_spot_em()  # type: ignore[union-attr]
            if raw is None or raw.empty:
                logger.warning("AkShare get_daily 返回空 DataFrame")
                return pd.DataFrame()

            # 字段映射
            rename_map = {k: v for k, v in _AKSHARE_TO_TUSHARE_FIELDS.items() if k in raw.columns}
            df = raw.rename(columns=rename_map)

            # ts_code 转换（6位代码 → xxx.SH / xxx.SZ / xxx.BJ）
            if "ts_code_raw" in df.columns:
                df["ts_code"] = df["ts_code_raw"].apply(
                    lambda x: _code_to_ts_code(str(x))
                )
                df.drop(columns=["ts_code_raw"], inplace=True)

            df["trade_date"] = trade_date

            # 单位转换
            for col in ["amount_raw"]:
                if col in df.columns:
                    df["amount"] = pd.to_numeric(df[col], errors="coerce") / 1000.0
                    df.drop(columns=[col], inplace=True, errors="ignore")

            for col in ["total_mv_raw"]:
                if col in df.columns:
                    df["total_mv"] = pd.to_numeric(df[col], errors="coerce") / 10000.0
                    df.drop(columns=[col], inplace=True, errors="ignore")

            if "circ_mv_raw" in df.columns:
                df["circ_mv"] = pd.to_numeric(df["circ_mv_raw"], errors="coerce") / 10000.0
                df.drop(columns=["circ_mv_raw"], inplace=True)

            # 数值列统一转 float
            for col in ["open", "high", "low", "close", "pct_chg", "vol",
                        "amount", "pe_ttm", "pb", "total_mv", "circ_mv"]:
                if col in df.columns:
                    df[col] = pd.to_numeric(df[col], errors="coerce")

            logger.info("AkShare get_daily", trade_date=trade_date, count=len(df))
            return df
        except Exception as e:
            raise DataProviderError(f"AkShare get_daily 失败: {e}") from e

    # ─────────────────────────────────────────────────────────────────────
    # get_daily_basic
    # ─────────────────────────────────────────────────────────────────────

    def get_daily_basic(self, trade_date: str) -> pd.DataFrame:
        """
        全市场估值数据（pe_ttm, pb, dv_ttm, total_mv, circ_mv）

        Source: ak.stock_a_lg_indicator(symbol="all")
        单位：AkShare 返回 亿元 → 转换为 万元（×10000）
        """
        try:
            raw = ak.stock_a_lg_indicator(symbol="all")  # type: ignore[union-attr, attr-defined]
            if raw is None or raw.empty:
                logger.warning("AkShare get_daily_basic 返回空 DataFrame")
                return pd.DataFrame()

            rename_map = {k: v for k, v in _AKSHARE_TO_TUSHARE_FIELDS.items() if k in raw.columns}
            df = raw.rename(columns=rename_map)

            # ts_code 转换
            if "ts_code_raw" in df.columns:
                df["ts_code"] = df["ts_code_raw"].apply(lambda x: _code_to_ts_code(str(x)))
                df.drop(columns=["ts_code_raw"], inplace=True)
            elif "代码" not in df.columns and "ts_code" not in df.columns:
                # 尝试从"股票代码"等别名转换
                for alt in ("股票代码", "code"):
                    if alt in df.columns:
                        df["ts_code"] = df[alt].apply(lambda x: _code_to_ts_code(str(x)))
                        break

            df["trade_date"] = trade_date

            # 市值单位转换：亿元 → 万元（×10000）
            for col in ("total_mv", "circ_mv"):
                if col in df.columns:
                    df[col] = pd.to_numeric(df[col], errors="coerce") * 10000.0

            # 数值列转 float
            for col in ["pe_ttm", "pb", "dv_ttm", "total_mv", "circ_mv"]:
                if col in df.columns:
                    df[col] = pd.to_numeric(df[col], errors="coerce")

            logger.info("AkShare get_daily_basic", trade_date=trade_date, count=len(df))
            return df
        except Exception as e:
            raise DataProviderError(f"AkShare get_daily_basic 失败: {e}") from e

    # ─────────────────────────────────────────────────────────────────────
    # get_financial_quarterly
    # ─────────────────────────────────────────────────────────────────────

    def get_financial_quarterly(
        self,
        ts_code: str | None = None,
        ts_codes: list[str] | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        period: str | None = None,
    ) -> pd.DataFrame:
        """
        季度财务摘要（慢路径：按股票循环）

        Source: ak.stock_financial_abstract(symbol, indicator="净利润") 等
        映射到 Tushare financial_indicator 结构（best-effort）

        Args:
            ts_code:    单股代码（兼容旧调用）
            ts_codes:   多股代码列表（批量循环）
            period:     报告期（YYYYMMDD），用于结果过滤
        """
        codes: list[str] = ts_codes or ([ts_code] if ts_code else [])
        if not codes:
            logger.warning("AkShare get_financial_quarterly: 无 ts_codes，跳过")
            return pd.DataFrame()

        results: list[pd.DataFrame] = []
        for code in codes:
            symbol = code.split(".")[0]
            try:
                df = self._fetch_financial_abstract(symbol, code)
                if df.empty:
                    continue
                if period and "end_date" in df.columns:
                    df = df[df["end_date"] == period].copy()
                if not df.empty:
                    results.append(df)
            except Exception as e:
                logger.warning(
                    "AkShare fq.single_error",
                    ts_code=code,
                    error=str(e)[:120],
                )

        if not results:
            return pd.DataFrame()

        combined = pd.concat(results, ignore_index=True)
        logger.info(
            "AkShare get_financial_quarterly",
            period=period,
            codes_tried=len(codes),
            rows=len(combined),
        )
        return combined

    def _fetch_financial_abstract(self, symbol: str, ts_code: str) -> pd.DataFrame:
        """
        拉取单股财务摘要并映射字段（best-effort）

        Source: ak.stock_financial_abstract(symbol=symbol)
        若 API 变更导致列名不同，优雅降级返回空 DataFrame。
        """
        try:
            raw: pd.DataFrame = ak.stock_financial_abstract(  # type: ignore[union-attr]
                symbol=symbol
            )
        except Exception:
            return pd.DataFrame()

        if raw is None or raw.empty:
            return pd.DataFrame()

        # 动态字段映射（只映射存在的字段）
        rename_map = {k: v for k, v in _AKSHARE_TO_TUSHARE_FIELDS.items() if k in raw.columns}
        df = raw.rename(columns=rename_map)

        df["ts_code"] = ts_code
        df["source"] = "akshare"

        # 报告期标准化为 YYYYMMDD 字符串
        if "end_date" in df.columns:
            df["end_date"] = (
                pd.to_datetime(df["end_date"], errors="coerce")
                .dt.strftime("%Y%m%d")
            )

        # 关键数值字段转换
        for col in ["q_dtprofit_cum", "net_profit", "op_revenue",
                    "roe", "netprofit_yoy", "op_yoy"]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")

        # q_dtprofit：用 q_dtprofit_cum 近似（无真正的单季扣非字段）
        if "q_dtprofit_cum" in df.columns and "q_dtprofit" not in df.columns:
            df["q_dtprofit"] = df["q_dtprofit_cum"]
        elif "q_dtprofit" not in df.columns:
            df["q_dtprofit"] = None

        return df

    # ─────────────────────────────────────────────────────────────────────
    # get_dividend  /  get_dividend_batch
    # ─────────────────────────────────────────────────────────────────────

    def get_dividend(
        self,
        ts_code: str,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> pd.DataFrame:
        """
        单股分红历史（via AkShare stock_history_dividend_detail）

        Args:
            ts_code:    股票代码（如 "000001.SZ"）
            start_date: 起始年度（YYYYMMDD，按 end_date 过滤）
            end_date:   截止年度（YYYYMMDD，按 end_date 过滤）

        Returns:
            分红记录 DataFrame（字段已映射到 Tushare 风格），失败返回空 DataFrame
        """
        try:
            symbol = ts_code.split(".")[0]
            df: pd.DataFrame = ak.stock_history_dividend_detail(  # type: ignore[union-attr, call-arg]
                symbol, "分红"  # 位置参数规避 Pylance 参数名检查
            )
            if df is None or df.empty:
                return pd.DataFrame()

            rename_map = {
                "公告日期":       "ann_date",
                "分红年度":       "end_date",
                "每股送股比例":   "stk_div",
                "每股派息(税前)": "cash_div",
                "每股派息(税后)": "cash_div_tax",
                "派息日":         "pay_date",
                "股权登记日":     "record_date",
                "除权除息日":     "ex_date",
            }
            df = df.rename(columns={k: v for k, v in rename_map.items() if k in df.columns})
            df["ts_code"] = ts_code
            df["div_proc"] = "实施"   # AkShare 仅返回已实施记录

            # 日期格式统一 → YYYYMMDD
            for col in ["ann_date", "end_date", "pay_date", "record_date", "ex_date"]:
                if col in df.columns:
                    df[col] = pd.to_datetime(df[col], errors="coerce").dt.strftime("%Y%m%d")

            # 按 end_date 范围过滤
            if (start_date or end_date) and "end_date" in df.columns:
                if start_date:
                    df = df[df["end_date"] >= start_date]
                if end_date:
                    df = df[df["end_date"] <= end_date]

            return df
        except Exception as e:
            logger.warning("AkShare get_dividend 失败", ts_code=ts_code, error=str(e)[:120])
            return pd.DataFrame()

    def get_dividend_batch(self, end_date: str) -> pd.DataFrame:
        """
        全市场分红批量接口兜底

        AkShare 无全市场分红批量接口，此方法返回空 DataFrame。
        CompositeProvider 在 Tushare 不可用时将退化为逐只调用 get_dividend()。
        """
        logger.warning("AkShare get_dividend_batch 不可用，返回空 DataFrame")
        return pd.DataFrame()

    # ─────────────────────────────────────────────────────────────────────
    # get_trade_calendar
    # ─────────────────────────────────────────────────────────────────────

    def get_trade_calendar(self, start_date: str, end_date: str) -> list[date]:
        """
        交易日历（sina 全量历史）

        Source: ak.tool_trade_date_hist_sina()
        """
        try:
            df: pd.DataFrame = ak.tool_trade_date_hist_sina()  # type: ignore[union-attr]
            if df is None or df.empty:
                return []

            # 兼容不同列名
            date_col = "trade_date" if "trade_date" in df.columns else df.columns[0]
            dates: list[date] = pd.to_datetime(df[date_col], errors="coerce").dt.date.dropna().tolist()
            start = datetime.strptime(start_date, "%Y%m%d").date()
            end = datetime.strptime(end_date, "%Y%m%d").date()
            return [d for d in dates if start <= d <= end]
        except Exception as e:
            raise DataProviderError(f"AkShare get_trade_calendar 失败: {e}") from e

    # ─────────────────────────────────────────────────────────────────────
    # get_company_info / get_company_info_batch
    # ─────────────────────────────────────────────────────────────────────

    def get_company_info(self, ts_code: str) -> dict:
        """
        公司实际控制人信息（AkShare 无标准接口，返回空字段）

        降级处理：返回空字符串，由 is_private_enterprise() 关键词兜底判断
        """
        logger.warning("AkShare 不提供实际控制人信息", ts_code=ts_code)
        return {"ts_code": ts_code, "act_ent_type": "", "act_name": ""}

    def get_company_info_batch(self) -> pd.DataFrame:
        """全量公司信息（AkShare 无批量接口，返回空 DataFrame）"""
        logger.warning("AkShare get_company_info_batch 不可用，返回空 DataFrame")
        return pd.DataFrame()
