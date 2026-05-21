"""
领域数据类 — 纯 Python dataclass，无 SQLAlchemy / FastAPI 依赖

v1: 初始版本
v2 (P0): StockInfo / StockSnapshot 扩展
v3 (P1): QuarterRecord 补充杜邦/盈余质量字段；新增 DividendRecord；
         FactorResult 新增 dupont/earnings_quality/dividend_continuity 字段
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date


@dataclass
class QuarterRecord:
    """季度财务报告原始数据（TTM / 杜邦计算用）"""

    ts_code: str
    end_date: date          # 报告期，e.g. 2024-09-30
    q_dtprofit: float       # 单季度扣非净利润（元）
    total_cur_assets: float = 0.0
    total_liab: float = 0.0
    op_revenue: float = 0.0
    source: str = "tushare"

    # ── P1 新增：杜邦分解字段 ─────────────────────────────────────
    roe: float | None = None              # ROE 净资产收益率 %
    npta: float | None = None             # 总资产净利率 %
    debt_to_assets: float | None = None   # 资产负债率 %
    assets_turn: float | None = None      # 总资产周转率（次）
    eqt_multiplier: float | None = None   # 权益乘数

    # ── P1 新增：同比增速字段 ──────────────────────────────────────
    netprofit_yoy: float | None = None    # 净利润同比增速 %
    op_yoy: float | None = None           # 营收同比增速 %

    # ── P1 新增：盈余质量字段 ──────────────────────────────────────
    n_cashflow_act: float | None = None   # 经营活动现金流净额（万元）
    cfo_to_np: float | None = None        # CFO / 净利润（盈余质量核心）


@dataclass
class DividendRecord:
    """单次分红记录（来自 Tushare dividend 接口）"""

    ts_code: str
    end_date: date           # 分红年度（财年末，e.g. 2023-12-31）
    div_proc: str = ""       # 实施进度（"实施"=已派现）
    stk_div: float | None = None      # 每股送股
    cash_div: float | None = None     # 每股派现（税前）
    cash_div_tax: float | None = None # 每股派现（税后）
    ann_date: date | None = None      # 公告日
    base_date: date | None = None     # 股权登记日
    pay_date: date | None = None      # 派息日
    record_date: date | None = None   # 债权登记日
    ex_date: date | None = None       # 除权除息日


@dataclass
class FactorResult:
    """单只股票某交易日的因子计算结果"""

    ts_code: str
    trade_date: date

    # ── 原始因子 ─────────────────────────────────────────────────
    pe_ttm: float | None = None          # 市盈率 TTM（来自行情，未修正）
    pe_deduct_ttm: float | None = None   # 扣非 PE TTM（自算）
    pb: float | None = None              # 市净率
    dv_ttm: float | None = None          # 股息率 TTM %
    total_mv: float | None = None        # 总市值（万元）
    growth_rate: float | None = None     # 营收/利润增速 %
    volatility: float | None = None      # 近60日波动率 %
    safety_margin: float | None = None   # 安全边际分（0-100）

    # ── z-score 归一化评分（0-100，横截面）───────────────────────
    value_score: float | None = None
    growth_score: float | None = None
    stability_score: float | None = None
    dividend_score: float | None = None
    safety_score: float | None = None

    # ── 综合 ─────────────────────────────────────────────────────
    composite_score: float | None = None
    composite_rank: float | None = None  # 全市场百分位 0-100

    # ── 筛选结论 ─────────────────────────────────────────────────
    passed_screening: bool = False
    fail_reasons: list[str] = field(default_factory=list)

    # ── P1 新增：杜邦综合分（绝对分，0-100，非横截面）────────────
    dupont_score: float | None = None
    high_leverage_flag: bool = False      # 高杠杆粉饰 ROE 红旗

    # ── P1 新增：盈余质量分（绝对分，0-100）─────────────────────
    earnings_quality_score: float | None = None
    cfo_to_np_ratio: float | None = None  # 近4季 CFO/NP 平均

    # ── P1 新增：分红连续性（年数 + 分）──────────────────────────
    dividend_continuity: int | None = None         # 连续分红年数
    dividend_continuity_score: float | None = None # 分红连续性分（0-100）
    clearance_dividend_flag: bool = False           # 清仓式分红红旗


@dataclass
class StockInfo:
    """股票基础信息"""

    ts_code: str
    name: str
    industry: str = ""
    exchange: str = ""
    list_date: date | None = None
    act_ent_type: str = ""   # 实际控制人类型（Tushare 字段）
    act_name: str = ""       # 实际控制人名称
    is_private: bool = False  # 派生：是否民企


@dataclass
class StockSnapshot:
    """
    某一交易日的股票完整快照（行情 + 因子），
    可直接序列化给前端

    v4 (P3)：补充 dupont_score / dividend_continuity / high_leverage_flag
    用于看板列表新增列和 AI prompt 增强
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
    total_mv_yi: float | None = None    # 亿元
    composite_score: float | None = None
    composite_rank: float | None = None
    passed_screening: bool = False
    industry: str = ""

    # ── P3 新增：因子详情（供列表列、AI Prompt 使用）────────────────
    dupont_score: float | None = None           # 杜邦综合分（0-100）
    dividend_continuity: int | None = None      # 连续分红年数
    high_leverage_flag: bool = False            # 高杠杆红旗
    earnings_quality_score: float | None = None # 盈余质量分（0-100）


@dataclass
class AIInsight:
    """AI 解读结果（5 个维度 + P3 financial_red_flags）"""

    ts_code: str
    overseas_revenue_pattern: str = ""    # 出海营收模式
    localization_level: str = ""          # 本地化程度
    core_business_logic: str = ""         # 核心商业逻辑
    moat_assessment: str = ""             # 护城河评估
    key_risks: str = ""                   # 关键风险
    # ── P3 新增：AI 综合识别的财务红旗 ─────────────────────────────────
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
