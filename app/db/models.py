"""
SQLAlchemy ORM 模型 — 6 张核心表
"""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


# ──────────────────────────────────────────────────────────────────────────────
# 1. stocks — 股票基础信息
# ──────────────────────────────────────────────────────────────────────────────
class Stock(Base):
    __tablename__ = "stocks"

    ts_code: Mapped[str] = mapped_column(String(12), primary_key=True)
    name: Mapped[str] = mapped_column(String(32), nullable=False)
    industry: Mapped[str] = mapped_column(String(64), default="")
    exchange: Mapped[str] = mapped_column(String(8), default="")   # SSE / SZSE / BSE
    list_date: Mapped[date | None] = mapped_column(Date, nullable=True)

    # 企业性质（Tushare company 接口）
    act_ent_type: Mapped[str] = mapped_column(String(32), default="")
    act_name: Mapped[str] = mapped_column(String(64), default="")
    is_private: Mapped[bool] = mapped_column(Boolean, default=False)

    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=func.now(), onupdate=func.now()
    )


# ──────────────────────────────────────────────────────────────────────────────
# 2. daily_snapshots — 每个交易日的行情快照
# ──────────────────────────────────────────────────────────────────────────────
class DailySnapshot(Base):
    __tablename__ = "daily_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts_code: Mapped[str] = mapped_column(String(12), nullable=False, index=True)
    trade_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)

    # 行情字段
    open: Mapped[float | None] = mapped_column(Float)
    high: Mapped[float | None] = mapped_column(Float)
    low: Mapped[float | None] = mapped_column(Float)
    close: Mapped[float | None] = mapped_column(Float)
    pct_chg: Mapped[float | None] = mapped_column(Float)
    vol: Mapped[float | None] = mapped_column(Float)
    amount: Mapped[float | None] = mapped_column(Float)

    # 估值字段（daily_basic）
    pe_ttm: Mapped[float | None] = mapped_column(Float)
    pb: Mapped[float | None] = mapped_column(Float)
    dv_ttm: Mapped[float | None] = mapped_column(Float)   # 股息率 %
    total_mv: Mapped[float | None] = mapped_column(Float)  # 万元
    circ_mv: Mapped[float | None] = mapped_column(Float)   # 万元

    __table_args__ = (
        UniqueConstraint("ts_code", "trade_date", name="uq_snapshot_code_date"),
        Index("ix_snapshot_date", "trade_date"),
        Index("ix_snapshot_code_date", "ts_code", "trade_date"),
    )


# ──────────────────────────────────────────────────────────────────────────────
# 3. financial_quarters — 季度财务原始数据
# ──────────────────────────────────────────────────────────────────────────────
class FinancialQuarter(Base):
    __tablename__ = "financial_quarters"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts_code: Mapped[str] = mapped_column(String(12), nullable=False, index=True)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)   # 报告期

    # 核心财务字段
    q_dtprofit: Mapped[float | None] = mapped_column(Float)   # 单季扣非净利润（元）
    total_cur_assets: Mapped[float | None] = mapped_column(Float)
    total_liab: Mapped[float | None] = mapped_column(Float)
    op_revenue: Mapped[float | None] = mapped_column(Float)

    # 年报累计字段（用于增速计算）
    ann_date: Mapped[date | None] = mapped_column(Date)       # 披露日期
    source: Mapped[str] = mapped_column(String(16), default="tushare")

    __table_args__ = (
        UniqueConstraint("ts_code", "end_date", name="uq_fq_code_enddate"),
        Index("ix_fq_code_enddate", "ts_code", "end_date"),
    )


# ──────────────────────────────────────────────────────────────────────────────
# 4. factor_scores — 因子计算结果
# ──────────────────────────────────────────────────────────────────────────────
class FactorScore(Base):
    __tablename__ = "factor_scores"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts_code: Mapped[str] = mapped_column(String(12), nullable=False, index=True)
    trade_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)

    # 原始因子
    pe_deduct_ttm: Mapped[float | None] = mapped_column(Float)
    growth_rate: Mapped[float | None] = mapped_column(Float)
    volatility: Mapped[float | None] = mapped_column(Float)
    safety_margin: Mapped[float | None] = mapped_column(Float)

    # 维度得分（z-score 归一化 0-100）
    value_score: Mapped[float | None] = mapped_column(Float)
    growth_score: Mapped[float | None] = mapped_column(Float)
    stability_score: Mapped[float | None] = mapped_column(Float)
    dividend_score: Mapped[float | None] = mapped_column(Float)
    safety_score: Mapped[float | None] = mapped_column(Float)

    # 综合
    composite_score: Mapped[float | None] = mapped_column(Float)
    composite_rank: Mapped[float | None] = mapped_column(Float)   # 全市场百分位

    # 筛选结论
    passed_screening: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    fail_reasons: Mapped[str] = mapped_column(Text, default="")   # JSON 列表

    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())

    __table_args__ = (
        UniqueConstraint("ts_code", "trade_date", name="uq_factor_code_date"),
        Index("ix_factor_passed_score", "passed_screening", "composite_score"),
    )


# ──────────────────────────────────────────────────────────────────────────────
# 5. ai_insights — AI 解读结果
# ──────────────────────────────────────────────────────────────────────────────
class AIInsight(Base):
    __tablename__ = "ai_insights"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts_code: Mapped[str] = mapped_column(String(12), nullable=False, index=True)

    # 5 维输出
    overseas_revenue_pattern: Mapped[str] = mapped_column(Text, default="")
    localization_level: Mapped[str] = mapped_column(Text, default="")
    core_business_logic: Mapped[str] = mapped_column(Text, default="")
    moat_assessment: Mapped[str] = mapped_column(Text, default="")
    key_risks: Mapped[str] = mapped_column(Text, default="")

    model: Mapped[str] = mapped_column(String(64), default="")
    prompt_hash: Mapped[str] = mapped_column(String(64), default="")
    status: Mapped[str] = mapped_column(String(16), default="pending")
    error_msg: Mapped[str] = mapped_column(Text, default="")

    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint("ts_code", "prompt_hash", name="uq_insight_code_hash"),
    )


# ──────────────────────────────────────────────────────────────────────────────
# 6. job_runs — 跑批历史
# ──────────────────────────────────────────────────────────────────────────────
class JobRun(Base):
    __tablename__ = "job_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="running")   # running/success/failed
    error: Mapped[str] = mapped_column(Text, default="")

    stocks_screened: Mapped[int] = mapped_column(Integer, default=0)
    stocks_passed: Mapped[int] = mapped_column(Integer, default=0)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    trade_date: Mapped[date | None] = mapped_column(Date, nullable=True)
