"""
领域数据类 — 纯 Python dataclass，无 SQLAlchemy / FastAPI 依赖

v1: 初始版本
v2 (P0): StockInfo / StockSnapshot 扩展
v3 (P1): QuarterRecord 补充杜邦/盈余质量字段；新增 DividendRecord；
         FactorResult 新增 dupont/earnings_quality/dividend_continuity 字段
v4 (T4+T5): FactorResult 加 close/pct_chg/amount；StockSnapshot 加 5 维分项 + fail_reasons
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date


@dataclass
class QuarterRecord:
    """季度财务报告原始数据（TTM / 杜邦计算用）"""

    ts_code: str
    end_date: date
    q_dtprofit: float
    total_cur_assets: float = 0.0
    total_liab: float = 0.0
    op_revenue: float = 0.0
    source: str = "tushare"

    roe: float | None = None
    npta: float | None = None
    debt_to_assets: float | None = None
    assets_turn: float | None = None
    eqt_multiplier: float | None = None
    netprofit_yoy: float | None = None
    op_yoy: float | None = None
    n_cashflow_act: float | None = None
    cfo_to_np: float | None = None


@dataclass
class DividendRecord:
    """单次分红记录（来自 Tushare dividend 接口）"""

    ts_code: str
    end_date: date
    div_proc: str = ""
    stk_div: float | None = None
    cash_div: float | None = None
    cash_div_tax: float | None = None
    ann_date: date | None = None
    base_date: date | None = None
    pay_date: date | None = None
    record_date: date | None = None
    ex_date: date | None = None


@dataclass
class FactorResult:
    """单只股票某交易日的因子计算结果"""

    ts_code: str
    trade_date: date

    # ── 原始因子 ─────────────────────────────────────────────────
    pe_ttm: float | None = None
    pe_deduct_ttm: float | None = None
    pb: float | None = None
    dv_ttm: float | None = None
    total_mv: float | None = None
    growth_rate: float | None = None
    volatility: float | None = None
    safety_margin: float | None = None

    # ── z-score 归一化评分（0-100，横截面）───────────────────────
    value_score: float | None = None
    growth_score: float | None = None
    stability_score: float | None = None
    dividend_score: float | None = None
    safety_score: float | None = None

    # ── 综合 ─────────────────────────────────────────────────────
    composite_score: float | None = None
    composite_rank: float | None = None

    # ── 筛选结论 ─────────────────────────────────────────────────
    passed_screening: bool = False
    fail_reasons: list[str] = field(default_factory=list)

    # ── P1：杜邦 ─────────────────────────────────────────────────
    dupont_score: float | None = None
    high_leverage_flag: bool = False

    # ── P1：盈余质量 ──────────────────────────────────────────────
    earnings_quality_score: float | None = None
    cfo_to_np_ratio: float | None = None

    # ── P1：分红连续性 ────────────────────────────────────────────
    dividend_continuity: int | None = None
    dividend_continuity_score: float | None = None
    clearance_dividend_flag: bool = False

    # ── T4 新增：行情基础字段（供硬过滤使用，不参与评分）──────────
    close: float | None = None       # 当日收盘价（元）
    pct_chg: float | None = None     # 当日涨跌幅 %
    amount: float | None = None      # 当日成交额（万元），None/0 表示停牌


@dataclass
class StockInfo:
    """股票基础信息"""

    ts_code: str
    name: str
    industry: str = ""
    exchange: str = ""
    list_date: date | None = None
    act_ent_type: str = ""
    act_name: str = ""
    is_private: bool = False


@dataclass
class StockSnapshot:
    """
    某一交易日的股票完整快照（行情 + 因子），可直接序列化给前端

    v4 (T5)：补充 5 维分项分 + fail_reasons，服务多 Sheet 导出和前端显示
    """

    ts_code: str
    name: str
    trade_date: date
    close: float | None = None
    pct_chg: float | None = None
    pe_ttm: float | None = None
    pe_deduct_ttm: float | None = None
    pb: float | None = None
    dv_ttm: float | None = None
    total_mv_yi: float | None = None
    composite_score: float | None = None
    composite_rank: float | None = None
    passed_screening: bool = False
    industry: str = ""

    # ── P3：因子详情 ──────────────────────────────────────────────
    dupont_score: float | None = None
    dividend_continuity: int | None = None
    high_leverage_flag: bool = False
    earnings_quality_score: float | None = None

    # ── T5 新增：5 维分项分（导出多 Sheet / 前端雷达图）──────────
    value_score: float | None = None
    growth_score: float | None = None
    stability_score: float | None = None
    dividend_score: float | None = None
    safety_score: float | None = None
    fail_reasons: list[str] = field(default_factory=list)


@dataclass
class AIInsight:
    """AI 解读结果（5 个维度 + P3 financial_red_flags）"""

    ts_code: str
    overseas_revenue_pattern: str = ""
    localization_level: str = ""
    core_business_logic: str = ""
    moat_assessment: str = ""
    key_risks: str = ""
    financial_red_flags: list[str] = field(default_factory=list)
    model: str = ""
    status: str = "pending"
    created_at: str | None = None
    from_cache: bool = False


@dataclass
class JobRunInfo:
    """跑批任务摘要"""

    id: int
    started_at: str
    finished_at: str | None
    status: str
    error: str | None
    stocks_screened: int = 0
    stocks_passed: int = 0
    duration_ms: int | None = None
