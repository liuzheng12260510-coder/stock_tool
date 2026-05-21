"""
AI 服务层 — 按需触发 + DB 缓存（P3 增强）

P3 变更：
- 缓存键加入 composite_score 版本号（_make_prompt_hash），财务因子更新后自动失效旧缓存
- _build_snapshot 同时加载 DuPont / CFO / 分红上下文，传递给 prompt builder
- 支持 financial_red_flags 字段的存储与读取
"""
from __future__ import annotations

import hashlib
from datetime import date

from sqlalchemy import select

from app.ai.openrouter import OpenRouterProvider
from app.core.logging import get_logger
from app.db.models import AIInsight as AIInsightORM
from app.db.models import DailySnapshot, DividendHistory, FactorScore, FinancialQuarter, Stock
from app.db.session import db_session
from app.domain.models import AIInsight, StockSnapshot

logger = get_logger("ai_service")


# ─────────────────────────────────────────────────────────────────────────────
# 公开接口
# ─────────────────────────────────────────────────────────────────────────────


def get_or_create_insight(ts_code: str) -> AIInsight:
    """
    按需触发 AI 解读（命中 DB 缓存则直接返回，否则调用 API）

    缓存失效逻辑（P3）：
    - 缓存键 = hash(ts_code + composite_score + trade_date)
    - 财务因子更新后 composite_score 变化 → hash 不同 → 跳过旧缓存，重新解读

    Args:
        ts_code: 股票代码，e.g. '600519.SH'

    Returns:
        AIInsight（from_cache=True 表示命中缓存）

    Raises:
        AIUnavailableError: AI 功能未配置
        AIProviderError: API 调用失败
    """
    # ── 先构建快照（需要 trade_date / composite_score 来计算 cache key）──
    snapshot, dupont_ctx, cfo_ctx, div_ctx = _build_snapshot_with_context(ts_code)

    # ── 计算当前 prompt hash（含 composite_score 版本）────────────────────
    current_hash = _make_prompt_hash(
        ts_code,
        snapshot.composite_score,
        snapshot.trade_date,
    )

    # ── 查 DB 缓存（status='success' + hash 匹配）────────────────────────
    with db_session() as db:
        cached = db.execute(
            select(AIInsightORM)
            .where(
                AIInsightORM.ts_code == ts_code,
                AIInsightORM.status == "success",
                AIInsightORM.prompt_hash == current_hash,
            )
            .order_by(AIInsightORM.created_at.desc())
        ).scalar_one_or_none()

        if cached:
            logger.info("AI 解读命中缓存", ts_code=ts_code, prompt_hash=current_hash)
            return AIInsight(
                ts_code=ts_code,
                overseas_revenue_pattern=cached.overseas_revenue_pattern,
                localization_level=cached.localization_level,
                core_business_logic=cached.core_business_logic,
                moat_assessment=cached.moat_assessment,
                key_risks=cached.key_risks,
                financial_red_flags=_parse_red_flags(cached.financial_red_flags),
                model=cached.model,
                status="success",
                created_at=str(cached.created_at) if cached.created_at else None,
                from_cache=True,
            )

    # ── 调用 AI ──────────────────────────────────────────────────────────
    provider = OpenRouterProvider()
    insight = provider.analyze_stock(
        snapshot,
        dupont_context=dupont_ctx,
        cfo_context=cfo_ctx,
        dividend_context=div_ctx,
    )

    # ── 持久化到 DB ───────────────────────────────────────────────────────
    _save_insight(ts_code, insight, current_hash)

    return insight


# ─────────────────────────────────────────────────────────────────────────────
# 内部辅助
# ─────────────────────────────────────────────────────────────────────────────


def _build_snapshot_with_context(
    ts_code: str,
) -> tuple[StockSnapshot, str, str, str]:
    """
    从 DB 构建 StockSnapshot（含 P3 新字段），
    同时生成 DuPont / CFO / 分红 的文字上下文供 Prompt 使用。

    Returns:
        (snapshot, dupont_context, cfo_context, dividend_context)
    """
    with db_session() as db:
        stock = db.execute(
            select(Stock).where(Stock.ts_code == ts_code)
        ).scalar_one_or_none()
        name = stock.name if stock else ts_code
        industry = stock.industry if stock else ""

        # 最近日行情
        snap = db.execute(
            select(DailySnapshot)
            .where(DailySnapshot.ts_code == ts_code)
            .order_by(DailySnapshot.trade_date.desc())
        ).scalars().first()

        # 最近因子评分
        score = db.execute(
            select(FactorScore)
            .where(FactorScore.ts_code == ts_code)
            .order_by(FactorScore.trade_date.desc())
        ).scalars().first()

        # 近 4 季财务数据（杜邦 / CFO）
        fq_rows = db.execute(
            select(FinancialQuarter)
            .where(FinancialQuarter.ts_code == ts_code)
            .order_by(FinancialQuarter.end_date.desc())
            .limit(4)
        ).scalars().all()

        # 近 5 年分红
        dh_rows = db.execute(
            select(DividendHistory)
            .where(
                DividendHistory.ts_code == ts_code,
                DividendHistory.div_proc == "实施",
            )
            .order_by(DividendHistory.end_date.desc())
            .limit(5)
        ).scalars().all()

    total_mv_yi = None
    if snap and snap.total_mv:
        total_mv_yi = snap.total_mv / 10_000.0

    snapshot = StockSnapshot(
        ts_code=ts_code,
        name=name,
        trade_date=snap.trade_date if snap else date.today(),
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
        # P3 新字段
        dupont_score=score.dupont_score if score else None,
        dividend_continuity=score.dividend_continuity if score else None,
        high_leverage_flag=bool(score.high_leverage_flag) if score else False,
        earnings_quality_score=score.earnings_quality_score if score else None,
    )

    # ── 构建文字上下文 ────────────────────────────────────────────────────
    dupont_ctx = _build_dupont_context(fq_rows)
    cfo_ctx = _build_cfo_context(fq_rows)
    div_ctx = _build_dividend_context(dh_rows)

    return snapshot, dupont_ctx, cfo_ctx, div_ctx


def _build_dupont_context(fq_rows: list) -> str:
    """生成杜邦三因子文字摘要"""
    if not fq_rows:
        return ""
    latest = fq_rows[0]
    parts = []
    if latest.roe is not None:
        parts.append(f"ROE={latest.roe:.1f}%")
    if latest.npta is not None:
        parts.append(f"总资产净利率={latest.npta:.1f}%")
    if latest.assets_turn is not None:
        parts.append(f"资产周转率={latest.assets_turn:.2f}次")
    if latest.eqt_multiplier is not None:
        parts.append(f"权益乘数={latest.eqt_multiplier:.2f}×")
    if latest.debt_to_assets is not None:
        parts.append(f"资产负债率={latest.debt_to_assets:.1f}%")
    if not parts:
        return ""
    period = latest.end_date.strftime("%Y-%m-%d") if hasattr(latest.end_date, "strftime") else str(latest.end_date)
    return f"最新报告期（{period}）：{', '.join(parts)}"


def _build_cfo_context(fq_rows: list) -> str:
    """生成 CFO/NP 近 4 季趋势文字摘要"""
    ratios = [fq.cfo_to_np for fq in fq_rows if fq.cfo_to_np is not None]
    if not ratios:
        return ""
    avg = sum(ratios) / len(ratios)
    recent_str = ", ".join(f"{r:.2f}" for r in reversed(ratios))
    quality = "优秀" if avg >= 0.8 else ("良好" if avg >= 0.5 else "偏低")
    return f"近{len(ratios)}季 CFO/NP 均值={avg:.2f}（{quality}），各季值：{recent_str}"


def _build_dividend_context(dh_rows: list) -> str:
    """生成分红历史文字摘要"""
    valid = [dh for dh in dh_rows if dh.cash_div and dh.cash_div > 0]
    if not valid:
        return "无现金分红记录"
    entries = [
        f"{dh.end_date.year}年派现{dh.cash_div:.2f}元/股"
        for dh in reversed(valid)
    ]
    return f"近{len(valid)}年：{', '.join(entries)}"


def _make_prompt_hash(
    ts_code: str,
    composite_score: float | None,
    trade_date: date,
) -> str:
    """
    生成 prompt 缓存键（P3：含 composite_score 版本）

    逻辑：当 composite_score 或 trade_date 变化时，hash 随之改变，
    旧缓存不再被命中，自动触发重新解读。
    """
    score_str = f"{composite_score:.2f}" if composite_score is not None else "none"
    raw = f"{ts_code}|{score_str}|{trade_date}"
    return hashlib.md5(raw.encode()).hexdigest()[:16]  # noqa: S324


def _save_insight(ts_code: str, insight: AIInsight, prompt_hash: str) -> None:
    """保存 AI 解读到 DB，使用 prompt_hash 作为幂等键"""
    import json

    red_flags_json = json.dumps(
        insight.financial_red_flags, ensure_ascii=False
    ) if insight.financial_red_flags else "[]"

    with db_session() as db:
        record = AIInsightORM(
            ts_code=ts_code,
            overseas_revenue_pattern=insight.overseas_revenue_pattern,
            localization_level=insight.localization_level,
            core_business_logic=insight.core_business_logic,
            moat_assessment=insight.moat_assessment,
            key_risks=insight.key_risks,
            financial_red_flags=red_flags_json,
            model=insight.model,
            prompt_hash=prompt_hash,
            status="success",
        )
        db.add(record)
    logger.info("AI 解读已持久化", ts_code=ts_code, prompt_hash=prompt_hash)


def _parse_red_flags(raw: str | None) -> list[str]:
    """从 DB Text 字段解析 financial_red_flags JSON 数组"""
    import json

    if not raw:
        return []
    try:
        result = json.loads(raw)
        return result if isinstance(result, list) else []
    except (json.JSONDecodeError, TypeError):
        return []
