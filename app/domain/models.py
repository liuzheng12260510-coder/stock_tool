"""
领域数据类 — 纯 Python dataclass，无 SQLAlchemy / FastAPI 依赖
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Optional


@dataclass
class QuarterRecord:
    """季度财务报告原始数据（TTM 计算用）"""

    ts_code: str
    end_date: date          # 报告期，e.g. 2024-09-30
    q_dtprofit: float       # 单季度扣非净利润（元）
    total_cur_assets: float = 0.0
    total_liab: float = 0.0
    op_revenue: float = 0.0
    source: str = "tushare"


@dataclass
class FactorResult:
    """单只股票某交易日的因子计算结果"""

    ts_code: str
    trade_date: date

    # ── 原始因子 ─────────────────────────────────────────────────
    pe_ttm: Optional[float] = None          # 市盈率 TTM（来自行情，未修正）
    pe_deduct_ttm: Optional[float] = None   # 扣非 PE TTM（自算）
    pb: Optional[float] = None              # 市净率
    dv_ttm: Optional[float] = None          # 股息率 TTM %
    total_mv: Optional[float] = None        # 总市值（万元）
    growth_rate: Optional[float] = None     # 营收/利润增速 %
    volatility: Optional[float] = None      # 近60日波动率 %
    safety_margin: Optional[float] = None   # 安全边际分（0-100）

    # ── z-score 归一化评分（0-100）──────────────────────────────
    value_score: Optional[float] = None
    growth_score: Optional[float] = None
    stability_score: Optional[float] = None
    dividend_score: Optional[float] = None
    safety_score: Optional[float] = None

    # ── 综合 ─────────────────────────────────────────────────────
    composite_score: Optional[float] = None
    composite_rank: Optional[float] = None  # 全市场百分位 0-100

    # ── 筛选结论 ─────────────────────────────────────────────────
    passed_screening: bool = False
    fail_reasons: list[str] = field(default_factory=list)


@dataclass
class StockInfo:
    """股票基础信息"""

    ts_code: str
    name: str
    industry: str = ""
    exchange: str = ""
    list_date: Optional[date] = None
    act_ent_type: str = ""   # 实际控制人类型（Tushare 字段）
    act_name: str = ""       # 实际控制人名称
    is_private: bool = False  # 派生：是否民企


@dataclass
class StockSnapshot:
    """
    某一交易日的股票完整快照（行情 + 因子），
    可直接序列化给前端
    """

    ts_code: str
    name: str
    trade_date: date
    close: Optional[float] = None
    pct_chg: Optional[float] = None
    pe_ttm: Optional[float] = None
    pe_deduct_ttm: Optional[float] = None
    pb: Optional[float] = None
    dv_ttm: Optional[float] = None
    total_mv_yi: Optional[float] = None    # 亿元
    composite_score: Optional[float] = None
    composite_rank: Optional[float] = None
    passed_screening: bool = False
    industry: str = ""


@dataclass
class AIInsight:
    """AI 解读结果（5 个维度）"""

    ts_code: str
    overseas_revenue_pattern: str = ""    # 出海营收模式
    localization_level: str = ""          # 本地化程度
    core_business_logic: str = ""         # 核心商业逻辑
    moat_assessment: str = ""             # 护城河评估
    key_risks: str = ""                   # 关键风险
    model: str = ""
    status: str = "pending"
    created_at: Optional[str] = None
    from_cache: bool = False


@dataclass
class JobRunInfo:
    """跑批任务摘要"""

    id: int
    started_at: str
    finished_at: Optional[str]
    status: str
    error: Optional[str]
    stocks_screened: int = 0
    stocks_passed: int = 0
    duration_ms: Optional[int] = None
