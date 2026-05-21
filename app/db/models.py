"""
SQLAlchemy ORM 模型 — 11 张核心表

v1: 初始表结构
v2(P0): 新增 Blacklist、SchemaMigration 表；Stock 增 is_blacklisted / delist_date / is_st / is_new_listing 列
v3(P1): FinancialQuarter 增杜邦/盈余质量字段；新增 DividendHistory 表；
        FactorScore 增 dupont/earnings_quality/dividend_continuity 字段
v4(P2): JobRun 增断点续传字段；新增 JobCheckpoint 断点明细表
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

    # ── P0 新增列（v2 migration）──────────────────────────────────────────
    # 是否在全局黑名单中（冗余字段，加速 pipeline 预筛）
    is_blacklisted: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    # 是否 ST / *ST / PT
    is_st: Mapped[bool] = mapped_column(Boolean, default=False)
    # 是否次新股（上市不足 min_list_years 年）
    is_new_listing: Mapped[bool] = mapped_column(Boolean, default=False)
    # 退市日期（从 stock_basic list_status 推断）
    delist_date: Mapped[date | None] = mapped_column(Date, nullable=True)

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

    # ── P1 新增：杜邦分解字段 ─────────────────────────────────────────────
    roe: Mapped[float | None] = mapped_column(Float)              # ROE %
    npta: Mapped[float | None] = mapped_column(Float)             # 总资产净利率 %
    debt_to_assets: Mapped[float | None] = mapped_column(Float)   # 资产负债率 %
    assets_turn: Mapped[float | None] = mapped_column(Float)      # 总资产周转率
    eqt_multiplier: Mapped[float | None] = mapped_column(Float)   # 权益乘数

    # ── P1 新增：同比增速字段 ──────────────────────────────────────────────
    netprofit_yoy: Mapped[float | None] = mapped_column(Float)    # 净利润同比 %
    op_yoy: Mapped[float | None] = mapped_column(Float)           # 营收同比 %

    # ── P1 新增：盈余质量字段 ──────────────────────────────────────────────
    n_cashflow_act: Mapped[float | None] = mapped_column(Float)   # 经营现金流净额（万元）
    cfo_to_np: Mapped[float | None] = mapped_column(Float)        # CFO/净利润

    __table_args__ = (
        UniqueConstraint("ts_code", "end_date", name="uq_fq_code_enddate"),
        Index("ix_fq_code_enddate", "ts_code", "end_date"),
    )


# ──────────────────────────────────────────────────────────────────────────────
# 4. dividend_history — 分红历史（P1 新增）
# ──────────────────────────────────────────────────────────────────────────────
class DividendHistory(Base):
    """
    历史分红记录（来自 Tushare dividend 接口）

    UNIQUE(ts_code, end_date)：同一财年只保留一条实施记录
    """

    __tablename__ = "dividend_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts_code: Mapped[str] = mapped_column(String(10), nullable=False, index=True)
    ann_date: Mapped[date | None] = mapped_column(Date, nullable=True)    # 公告日
    end_date: Mapped[date] = mapped_column(Date, nullable=False)           # 分红年度
    div_proc: Mapped[str] = mapped_column(String(20), default="")          # 实施进度
    stk_div: Mapped[float | None] = mapped_column(Float)                   # 每股送股
    cash_div: Mapped[float | None] = mapped_column(Float)                  # 每股派现（税前）
    cash_div_tax: Mapped[float | None] = mapped_column(Float)              # 每股派现（税后）
    base_date: Mapped[date | None] = mapped_column(Date, nullable=True)    # 股权登记日
    pay_date: Mapped[date | None] = mapped_column(Date, nullable=True)     # 派息日
    record_date: Mapped[date | None] = mapped_column(Date, nullable=True)  # 债权登记日
    ex_date: Mapped[date | None] = mapped_column(Date, nullable=True)      # 除权除息日

    __table_args__ = (
        UniqueConstraint("ts_code", "end_date", name="uq_dividend_code_enddate"),
        Index("ix_dividend_ts_code", "ts_code"),
        Index("ix_dividend_end_date", "end_date"),
    )


# ──────────────────────────────────────────────────────────────────────────────
# 5. factor_scores — 因子计算结果
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

    # ── P1 新增：杜邦综合分 ───────────────────────────────────────────────
    dupont_score: Mapped[float | None] = mapped_column(Float)
    high_leverage_flag: Mapped[bool | None] = mapped_column(Boolean, default=False)

    # ── P1 新增：盈余质量分 ───────────────────────────────────────────────
    earnings_quality_score: Mapped[float | None] = mapped_column(Float)

    # ── P1 新增：分红连续性 ───────────────────────────────────────────────
    dividend_continuity: Mapped[int | None] = mapped_column(Integer)
    dividend_continuity_score: Mapped[float | None] = mapped_column(Float)

    __table_args__ = (
        UniqueConstraint("ts_code", "trade_date", name="uq_factor_code_date"),
        Index("ix_factor_passed_score", "passed_screening", "composite_score"),
    )


# ──────────────────────────────────────────────────────────────────────────────
# 6. ai_insights — AI 解读结果
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

    # ── P3 新增：AI 综合识别的财务红旗（JSON 数组 → Text）──────────────
    financial_red_flags: Mapped[str] = mapped_column(Text, default="[]")

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
# 7. job_runs — 跑批历史
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

    # ── P2 新增：断点续传字段 ───────────────────────────────────────────────
    # 当前正在执行的 step 名称（用于监控与恢复）
    current_step: Mapped[str] = mapped_column(String(50), default="")
    # 子阶段已处理项数（如已完成的 ts_codes 数量）
    step_progress: Mapped[int] = mapped_column(Integer, default=0)
    # JSON 字符串，存储中间态数据（当前 step 的 checkpoint 快照）
    checkpoint_data: Mapped[str] = mapped_column(Text, default="")
    # 最近心跳时间，用于识别挂起的 job（超过 1 小时无更新视为崩溃）
    last_heartbeat: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


# ──────────────────────────────────────────────────────────────────────────────
# 8. job_checkpoints — 细粒度跑批断点表（P2 新增）
# ──────────────────────────────────────────────────────────────────────────────
class JobCheckpoint(Base):
    """
    细粒度跑批断点明细表（P2 新增）

    每条记录对应一个 job_run 内的单个执行 step。
    step_name 取值：
      stock_basic / blacklist / daily / daily_basic / prescreen /
      snapshot_save / fina_q1 / fina_q2 / fina_q3 / fina_q4 /
      dividend / factor_compute / scoring / save_scores

    status 取值：pending / running / done / failed
    payload: 可选 JSON，记录中间态（如 dividend 已完成的年度列表）
    """

    __tablename__ = "job_checkpoints"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_run_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    step_name: Mapped[str] = mapped_column(String(50), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), default="pending"
    )  # pending / running / done / failed
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # 可选 JSON（如已处理的 ts_codes 集合 / 已完成年度列表）
    payload: Mapped[str] = mapped_column(Text, default="")

    __table_args__ = (
        UniqueConstraint("job_run_id", "step_name", name="uq_checkpoint_run_step"),
        Index("ix_checkpoint_job_run_id", "job_run_id"),
    )


# ──────────────────────────────────────────────────────────────────────────────
# 9. blacklist — 全局股票黑名单（P0 新增）
# ──────────────────────────────────────────────────────────────────────────────
class Blacklist(Base):
    """
    全局股票黑名单

    reason 类型：
      ST          — 名称前缀含 ST（含空格/不含空格）
      *ST         — 名称前缀含 *ST 或 ＊ST
      PT          — 名称前缀含 PT
      delist_risk — 名称含"退市"
      bse         — 北交所股票（.BJ 后缀）
      new_listing — 次新股（上市不足 min_list_years 年）
      manual      — 手工添加
    """

    __tablename__ = "blacklist"

    ts_code: Mapped[str] = mapped_column(String(12), primary_key=True)
    reason: Mapped[str] = mapped_column(String(32), nullable=False)
    # expires_at 为 NULL 表示永久黑名单；次新股类型有到期时间
    expires_at: Mapped[date | None] = mapped_column(Date, nullable=True)
    added_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())

    __table_args__ = (
        Index("ix_blacklist_reason", "reason"),
        Index("ix_blacklist_expires", "expires_at"),
    )


# ──────────────────────────────────────────────────────────────────────────────
# 10. schema_migrations — 轻量级 schema 版本管理（P0 新增）
# ──────────────────────────────────────────────────────────────────────────────
class SchemaMigration(Base):
    """
    记录已执行的 schema 迁移版本，确保 init_db.py 幂等运行

    每次 init_db.py 运行时：
      1. 读取此表中已应用的版本号
      2. 对所有 version > max_applied 的迁移，顺序执行 DDL
      3. 成功后写入此表
    """

    __tablename__ = "schema_migrations"

    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    description: Mapped[str] = mapped_column(String(256), default="")
    applied_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
