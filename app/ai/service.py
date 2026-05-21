"""
AI 服务层 — 按需触发 + DB 缓存
"""
from __future__ import annotations

from datetime import datetime

from app.ai.openrouter import OpenRouterProvider
from app.core.exceptions import AIUnavailableError
from app.core.logging import get_logger
from app.db.models import AIInsight as AIInsightORM
from app.db.models import DailySnapshot, FactorScore, Stock
from app.db.session import db_session
from app.domain.models import AIInsight, StockSnapshot

logger = get_logger("ai_service")


def get_or_create_insight(ts_code: str) -> AIInsight:
    """
    按需触发 AI 解读（命中 DB 缓存则直接返回，否则调用 API）

    Args:
        ts_code: 股票代码，e.g. '600519.SH'

    Returns:
        AIInsight（from_cache=True 表示命中缓存）

    Raises:
        AIUnavailableError: AI 功能未配置
        AIProviderError: API 调用失败
    """
    # ── 查 DB 缓存（status='success' 的最新记录）────────────────────────
    with db_session() as db:
        cached = (
            db.query(AIInsightORM)
            .filter(
                AIInsightORM.ts_code == ts_code,
                AIInsightORM.status == "success",
            )
            .order_by(AIInsightORM.created_at.desc())
            .first()
        )
        if cached:
            logger.info("AI 解读命中缓存", ts_code=ts_code)
            return AIInsight(
                ts_code=ts_code,
                overseas_revenue_pattern=cached.overseas_revenue_pattern,
                localization_level=cached.localization_level,
                core_business_logic=cached.core_business_logic,
                moat_assessment=cached.moat_assessment,
                key_risks=cached.key_risks,
                model=cached.model,
                status="success",
                created_at=str(cached.created_at) if cached.created_at else None,
                from_cache=True,
            )

    # ── 构建快照 ─────────────────────────────────────────────────────────
    snapshot = _build_snapshot(ts_code)

    # ── 调用 AI ──────────────────────────────────────────────────────────
    provider = OpenRouterProvider()
    insight = provider.analyze_stock(snapshot)

    # ── 持久化到 DB ───────────────────────────────────────────────────────
    _save_insight(ts_code, insight)

    return insight


def _build_snapshot(ts_code: str) -> StockSnapshot:
    """从 DB 构建 StockSnapshot（最近一日数据）"""
    with db_session() as db:
        stock = db.query(Stock).filter(Stock.ts_code == ts_code).first()
        name = stock.name if stock else ts_code
        industry = stock.industry if stock else ""

        # 最近日行情
        snap = (
            db.query(DailySnapshot)
            .filter(DailySnapshot.ts_code == ts_code)
            .order_by(DailySnapshot.trade_date.desc())
            .first()
        )

        # 最近因子评分
        score = (
            db.query(FactorScore)
            .filter(FactorScore.ts_code == ts_code)
            .order_by(FactorScore.trade_date.desc())
            .first()
        )

    total_mv_yi = None
    if snap and snap.total_mv:
        total_mv_yi = snap.total_mv / 10000.0

    return StockSnapshot(
        ts_code=ts_code,
        name=name,
        trade_date=snap.trade_date if snap else __import__("datetime").date.today(),
        close=snap.close if snap else None,
        pct_chg=snap.pct_chg if snap else None,
        pe_ttm=snap.pe_ttm if snap else None,
        pe_deduct_ttm=score.pe_deduct_ttm if score else None,
        pb=snap.pb if snap else None,
        dv_ttm=snap.dv_ttm if snap else None,
        total_mv_yi=total_mv_yi,
        composite_score=score.composite_score if score else None,
        composite_rank=score.composite_rank if score else None,
        passed_screening=score.passed_screening if score else False,
        industry=industry,
    )


def _save_insight(ts_code: str, insight: AIInsight) -> None:
    """保存 AI 解读到 DB"""
    with db_session() as db:
        record = AIInsightORM(
            ts_code=ts_code,
            overseas_revenue_pattern=insight.overseas_revenue_pattern,
            localization_level=insight.localization_level,
            core_business_logic=insight.core_business_logic,
            moat_assessment=insight.moat_assessment,
            key_risks=insight.key_risks,
            model=insight.model,
            prompt_hash="",  # 可选
            status="success",
        )
        db.add(record)
    logger.info("AI 解读已持久化", ts_code=ts_code)
