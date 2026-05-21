"""
跑批主流程 Pipeline

执行顺序：
1. 确定交易日
2. 检查当日是否已跑过（幂等性）
3. 拉取全市场行情（daily + daily_basic，各一次请求）
4. 加载股票基础信息（用于预筛判断 exchange）
5. 【预筛】用 pe_ttm + total_mv 过滤明显不合格的股票，缩小宇宙
6. 对预筛后子集保存日行情快照
7. 只针对预筛通过的股票拉取/更新季度财务
8. 计算单股因子
9. 横截面评分
10. 应用硬性筛选
11. 入库（upsert）
12. 更新任务状态
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from typing import Optional

import pandas as pd

from app.analytics.factors import compute_all_factors
from app.analytics.filters import apply_hard_filters, apply_prescreen_filter, is_private_enterprise
from app.analytics.scoring import compute_cross_sectional_scores
from app.core.config import settings
from app.core.logging import get_logger
from app.data.composite import CompositeProvider
from app.data.trade_calendar import get_latest_trade_date
from app.db.models import (
    DailySnapshot,
    FactorScore,
    FinancialQuarter,
    JobRun,
    Stock,
)
from app.db.session import db_session
from app.domain.models import FactorResult, QuarterRecord

logger = get_logger("pipeline")


class Pipeline:
    """跑批流水线"""

    def __init__(self) -> None:
        self.provider = CompositeProvider()

    # ─────────────────────────────────────────────────────────────────────
    # 公共入口
    # ─────────────────────────────────────────────────────────────────────

    def run(self, trade_date: Optional[date] = None, force: bool = False) -> int:
        """
        执行完整跑批

        Args:
            trade_date: 指定交易日，None = 最近交易日
            force:      强制重跑（即使今日已跑过）

        Returns:
            job_run 的 id
        """
        started_at = datetime.now()

        # 建 job_run 记录
        with db_session() as db:
            run_date = trade_date or get_latest_trade_date()
            job = JobRun(
                started_at=started_at,
                status="running",
                trade_date=run_date,
            )
            db.add(job)
            db.flush()
            job_id = job.id

        logger.info("跑批开始", job_id=job_id, trade_date=str(run_date))

        try:
            stocks_screened, stocks_passed = self._execute(run_date, force=force)
            duration_ms = int((datetime.now() - started_at).total_seconds() * 1000)

            with db_session() as db:
                db.query(JobRun).filter(JobRun.id == job_id).update({
                    "finished_at": datetime.now(),
                    "status": "success",
                    "stocks_screened": stocks_screened,
                    "stocks_passed": stocks_passed,
                    "duration_ms": duration_ms,
                })

            logger.info(
                "跑批完成",
                job_id=job_id,
                stocks_screened=stocks_screened,
                stocks_passed=stocks_passed,
                duration_ms=duration_ms,
            )

        except Exception as e:
            logger.exception("跑批失败", job_id=job_id, error=str(e))
            with db_session() as db:
                db.query(JobRun).filter(JobRun.id == job_id).update({
                    "finished_at": datetime.now(),
                    "status": "failed",
                    "error": str(e)[:2000],
                    "duration_ms": int((datetime.now() - started_at).total_seconds() * 1000),
                })
            raise

        return job_id

    # ─────────────────────────────────────────────────────────────────────
    # 内部执行逻辑
    # ─────────────────────────────────────────────────────────────────────

    def _execute(self, trade_date: date, force: bool = False) -> tuple[int, int]:
        """
        实际跑批逻辑

        Returns:
            (stocks_screened, stocks_passed)
        """
        trade_date_str = trade_date.strftime("%Y%m%d")

        # ── 幂等检查 ─────────────────────────────────────────────────────
        if not force:
            with db_session() as db:
                existing = db.query(FactorScore).filter(
                    FactorScore.trade_date == trade_date
                ).first()
                if existing:
                    logger.info("当日数据已存在，跳过（使用 force=True 强制重跑）", trade_date=trade_date_str)
                    count = db.query(FactorScore).filter(FactorScore.trade_date == trade_date).count()
                    passed = db.query(FactorScore).filter(
                        FactorScore.trade_date == trade_date,
                        FactorScore.passed_screening == True,
                    ).count()
                    return count, passed

        # ── Step 1: 拉取股票基础信息（更新 stocks 表）────────────────────
        logger.info("Step 1: 更新股票基础信息")
        self._update_stock_basic()

        # ── Step 2: 拉取当日行情（全市场，一次请求）─────────────────────
        logger.info("Step 2: 拉取当日行情", trade_date=trade_date_str)
        daily_df = self.provider.get_daily(trade_date_str)
        basic_df = self.provider.get_daily_basic(trade_date_str)

        if daily_df.empty or basic_df.empty:
            logger.warning("当日行情为空，可能是非交易日", trade_date=trade_date_str)
            return 0, 0

        # 合并行情数据
        market_df = daily_df.merge(basic_df, on="ts_code", how="inner", suffixes=("", "_b"))
        logger.info("行情数据合并完成", count=len(market_df))

        # ── Step 3: 提前加载股票基础信息（用于预筛 exchange 字段）────────
        logger.info("Step 3: 加载股票基础信息（用于预筛）")
        with db_session() as db:
            stocks: dict[str, dict] = {
                s.ts_code: {
                    "exchange": s.exchange or "",
                    "is_private": bool(s.is_private),
                    "name": s.name or "",
                    "industry": s.industry or "",
                }
                for s in db.query(Stock).all()
            }

        # ── Step 4: 预筛——用 pe_ttm + total_mv 缩小数据请求宇宙 ─────────
        logger.info("Step 4: 预筛过滤（市值/PE 粗筛，跳过明显不合格股票）")
        eligible_set: set[str] = set()
        prescreen_skipped = 0

        for _, row in market_df.iterrows():
            ts_code = str(row["ts_code"])
            stock = stocks.get(ts_code)
            exchange = stock["exchange"] if stock else ""

            total_mv = _to_float(row.get("total_mv"))
            pe_ttm = _to_float(row.get("pe_ttm"))
            total_mv_yi = total_mv / 10000.0 if total_mv is not None else None

            eligible, skip_reason = apply_prescreen_filter(
                ts_code=ts_code,
                exchange=exchange,
                pe_ttm=pe_ttm,
                total_mv_yi=total_mv_yi,
                exclude_bj=settings.screen_exclude_bj,
                prescreen_pe_ttm_max=settings.prescreen_pe_ttm_max,
                prescreen_total_mv_max_yi=settings.prescreen_total_mv_max_yi,
            )

            if eligible:
                eligible_set.add(ts_code)
            else:
                prescreen_skipped += 1
                logger.debug("预筛跳过", ts_code=ts_code, reason=skip_reason)

        logger.info(
            "预筛完成",
            total=len(market_df),
            eligible=len(eligible_set),
            skipped=prescreen_skipped,
        )

        # 把 market_df 收窄到预筛通过的子集，后续所有步骤都用它
        market_df = market_df[market_df["ts_code"].isin(eligible_set)].copy()
        eligible_ts_codes: list[str] = market_df["ts_code"].tolist()

        # ── Step 5: 保存快照（仅预筛通过子集）───────────────────────────
        logger.info("Step 5: 保存日行情快照（预筛后子集）", count=len(market_df))
        self._save_daily_snapshots(market_df, trade_date)

        # ── Step 6: 只对预筛通过的股票拉取季度财务 ───────────────────────
        logger.info("Step 6: 更新季度财务数据", ts_count=len(eligible_ts_codes))
        self._update_financial_quarters(trade_date, eligible_ts_codes)

        # ── Step 7: 从 DB 读取季度财务 ───────────────────────────────────
        logger.info("Step 7: 加载季度财务记录")
        with db_session() as db:
            quarters_raw_list: list[QuarterRecord] = [
                QuarterRecord(
                    ts_code=q.ts_code,
                    end_date=q.end_date,
                    q_dtprofit=q.q_dtprofit or 0.0,
                    total_cur_assets=q.total_cur_assets or 0.0,
                    total_liab=q.total_liab or 0.0,
                    op_revenue=q.op_revenue or 0.0,
                    source=q.source or "tushare",
                )
                for q in db.query(FinancialQuarter).filter(
                    FinancialQuarter.ts_code.in_(eligible_ts_codes)
                ).all()
            ]

        # 按股票分组季度数据
        quarters_by_code: dict[str, list[QuarterRecord]] = {}
        for q in quarters_raw_list:
            quarters_by_code.setdefault(q.ts_code, []).append(q)

        # ── Step 8: 计算因子 ──────────────────────────────────────────────
        logger.info("Step 8: 计算因子")
        factor_results: list[FactorResult] = []

        for _, row in market_df.iterrows():
            ts_code = str(row["ts_code"])
            stock = stocks.get(ts_code)

            if stock:
                is_private = stock["is_private"]
                exchange = stock["exchange"]
            else:
                is_private = True  # 宽松
                exchange = ""

            total_mv = _to_float(row.get("total_mv"))
            pe_ttm = _to_float(row.get("pe_ttm"))
            pb = _to_float(row.get("pb"))
            dv_ttm = _to_float(row.get("dv_ttm"))

            quarters = quarters_by_code.get(ts_code, [])

            factor = compute_all_factors(
                ts_code=ts_code,
                trade_date=trade_date,
                quarters=quarters,
                total_mv_wan=total_mv,
                pe_ttm=pe_ttm,
                pb=pb,
                dv_ttm=dv_ttm,
            )

            # 应用硬性筛选
            total_mv_yi = total_mv / 10000.0 if total_mv else None
            passed, reasons = apply_hard_filters(
                ts_code=ts_code,
                exchange=exchange,
                is_private=is_private,
                pe_ttm=pe_ttm,
                pe_deduct_ttm=factor.pe_deduct_ttm,
                dv_ttm=dv_ttm,
                total_mv_yi=total_mv_yi,
                exclude_bj=settings.screen_exclude_bj,
                exclude_soe=settings.screen_exclude_soe,
                pe_ttm_max=settings.screen_pe_ttm_max,
                pe_deduct_max=settings.screen_pe_deduct_max,
                dv_ttm_min=settings.screen_dv_ttm_min,
                total_mv_max_yi=settings.screen_total_mv_max_yi,
            )
            factor.passed_screening = passed
            factor.fail_reasons = reasons
            factor_results.append(factor)

        logger.info("因子计算完成", total=len(factor_results))

        # ── Step 9: 横截面评分 ────────────────────────────────────────────
        logger.info("Step 9: 横截面评分")
        factor_results = compute_cross_sectional_scores(factor_results)

        # 按综合分应用最低分过滤
        for f in factor_results:
            if f.passed_screening and f.composite_score is not None:
                if f.composite_score < settings.screen_min_composite_score:
                    f.passed_screening = False
                    f.fail_reasons.append(f"综合评分={f.composite_score:.1f}<{settings.screen_min_composite_score}")

        # ── Step 10: 入库 ─────────────────────────────────────────────────
        logger.info("Step 10: 保存因子评分到数据库")
        self._save_factor_scores(factor_results, trade_date)

        stocks_passed = sum(1 for f in factor_results if f.passed_screening)
        logger.info(
            "跑批流程完成",
            total=len(factor_results),
            passed=stocks_passed,
            trade_date=trade_date_str,
        )
        return len(factor_results), stocks_passed

    # ─────────────────────────────────────────────────────────────────────
    # 辅助方法
    # ─────────────────────────────────────────────────────────────────────

    def _update_stock_basic(self) -> None:
        """更新股票基础信息（stocks 表）"""
        df = self.provider.get_stock_basic()
        if df.empty:
            return

        # 拉取公司信息（民企字段）
        company_df = pd.DataFrame()
        try:
            company_df = self.provider.get_company_info_batch()
        except Exception as e:
            logger.warning("批量公司信息拉取失败，将跳过民企更新", error=str(e))

        if not company_df.empty:
            df = df.merge(company_df, on="ts_code", how="left")

        with db_session() as db:
            for _, row in df.iterrows():
                ts_code = str(row["ts_code"])
                act_ent_type = str(row.get("act_ent_type", "") or "")
                act_name = str(row.get("act_name", "") or "")
                is_private, _ = is_private_enterprise(act_ent_type, act_name, ts_code)

                existing = db.query(Stock).filter(Stock.ts_code == ts_code).first()
                if existing:
                    existing.name = str(row.get("name", ""))
                    existing.industry = str(row.get("industry", "") or "")
                    existing.exchange = str(row.get("exchange", "") or "")
                    existing.act_ent_type = act_ent_type
                    existing.act_name = act_name
                    existing.is_private = is_private
                else:
                    list_date_raw = row.get("list_date")
                    list_date = None
                    if list_date_raw and str(list_date_raw).strip():
                        try:
                            list_date = datetime.strptime(str(list_date_raw), "%Y%m%d").date()
                        except ValueError:
                            pass

                    stock = Stock(
                        ts_code=ts_code,
                        name=str(row.get("name", "")),
                        industry=str(row.get("industry", "") or ""),
                        exchange=str(row.get("exchange", "") or ""),
                        list_date=list_date,
                        act_ent_type=act_ent_type,
                        act_name=act_name,
                        is_private=is_private,
                    )
                    db.add(stock)

        logger.info("股票基础信息更新完成", count=len(df))

    def _save_daily_snapshots(self, df: pd.DataFrame, trade_date: date) -> None:
        """保存日行情快照（upsert）"""
        with db_session() as db:
            for _, row in df.iterrows():
                ts_code = str(row["ts_code"])
                # 检查是否已存在
                existing = db.query(DailySnapshot).filter(
                    DailySnapshot.ts_code == ts_code,
                    DailySnapshot.trade_date == trade_date,
                ).first()
                if existing:
                    continue  # 已有数据不覆盖

                snap = DailySnapshot(
                    ts_code=ts_code,
                    trade_date=trade_date,
                    open=_to_float(row.get("open")),
                    high=_to_float(row.get("high")),
                    low=_to_float(row.get("low")),
                    close=_to_float(row.get("close")),
                    pct_chg=_to_float(row.get("pct_chg")),
                    vol=_to_float(row.get("vol")),
                    amount=_to_float(row.get("amount")),
                    pe_ttm=_to_float(row.get("pe_ttm")),
                    pb=_to_float(row.get("pb")),
                    dv_ttm=_to_float(row.get("dv_ttm")),
                    total_mv=_to_float(row.get("total_mv")),
                    circ_mv=_to_float(row.get("circ_mv")),
                )
                db.add(snap)

        logger.info("日行情快照保存完成", trade_date=str(trade_date))

    def _update_financial_quarters(
        self,
        trade_date: date,
        ts_codes: list[str],
    ) -> None:
        """
        增量更新季度财务数据

        策略：
        - 获取最近 4 个报告期（最多）
        - 只针对传入的 ts_codes 列表拉取（预筛后子集）
        - 优先使用 fina_indicator_vip 全市场一次拉取
        - vip 不可用时，用 ts_codes 列表逐只兜底
        - 已存在的 (ts_code, end_date) 不重复写入

        Args:
            trade_date: 当前交易日
            ts_codes:   需要拉取财务数据的股票列表（预筛后子集）
        """
        if not ts_codes:
            logger.info("预筛后无需拉取财务数据的股票")
            return

        # 最近报告期列表（往前推 1 年）
        periods = _get_recent_report_periods(trade_date, n=4)

        for period in periods:
            logger.info("拉取季度财务", period=period, ts_count=len(ts_codes))
            try:
                df = self.provider.get_financial_quarterly(
                    period=period,
                    ts_codes=ts_codes,
                )
                if df.empty:
                    continue
                self._save_financial_quarters(df)
            except Exception as e:
                logger.warning("季度财务拉取失败", period=period, error=str(e))

    def _save_financial_quarters(self, df: pd.DataFrame) -> None:
        """保存季度财务数据（insert-or-skip）"""
        # 批内去重：逐只拼合的 DataFrame 可能含有相同 (ts_code, end_date) 的多条披露记录
        seen_in_batch: set[tuple[str, date]] = set()
        saved = 0
        with db_session() as db:
            for _, row in df.iterrows():
                ts_code = str(row.get("ts_code", ""))
                end_date_raw = row.get("end_date")
                if not ts_code or not end_date_raw:
                    continue
                try:
                    end_date = datetime.strptime(str(end_date_raw), "%Y%m%d").date()
                except ValueError:
                    try:
                        end_date = date.fromisoformat(str(end_date_raw))
                    except ValueError:
                        continue

                # 批内重复直接跳过，避免 UNIQUE constraint 冲突
                key = (ts_code, end_date)
                if key in seen_in_batch:
                    continue
                seen_in_batch.add(key)

                # 数据库层面去重（已有则跳过）
                existing = db.query(FinancialQuarter).filter(
                    FinancialQuarter.ts_code == ts_code,
                    FinancialQuarter.end_date == end_date,
                ).first()
                if existing:
                    continue

                ann_date = None
                ann_raw = row.get("ann_date")
                if ann_raw and str(ann_raw).strip():
                    try:
                        ann_date = datetime.strptime(str(ann_raw), "%Y%m%d").date()
                    except ValueError:
                        pass

                fq = FinancialQuarter(
                    ts_code=ts_code,
                    end_date=end_date,
                    q_dtprofit=_to_float(row.get("q_dtprofit")),
                    total_cur_assets=_to_float(row.get("total_cur_assets")),
                    total_liab=_to_float(row.get("total_liab")),
                    op_revenue=_to_float(row.get("op_revenue")),
                    ann_date=ann_date,
                    source="tushare",
                )
                db.add(fq)
                saved += 1

        logger.info("季度财务保存完成", saved=saved, total_rows=len(df))

    def _save_factor_scores(self, factors: list[FactorResult], trade_date: date) -> None:
        """保存因子评分（先删当日旧数据，再批量插入）"""
        with db_session() as db:
            # 删除当日旧数据（全量重算）
            db.query(FactorScore).filter(FactorScore.trade_date == trade_date).delete()

            for f in factors:
                score = FactorScore(
                    ts_code=f.ts_code,
                    trade_date=f.trade_date,
                    pe_deduct_ttm=f.pe_deduct_ttm,
                    growth_rate=f.growth_rate,
                    volatility=f.volatility,
                    safety_margin=f.safety_margin,
                    value_score=f.value_score,
                    growth_score=f.growth_score,
                    stability_score=f.stability_score,
                    dividend_score=f.dividend_score,
                    safety_score=f.safety_score,
                    composite_score=f.composite_score,
                    composite_rank=f.composite_rank,
                    passed_screening=f.passed_screening,
                    fail_reasons=json.dumps(f.fail_reasons, ensure_ascii=False),
                )
                db.add(score)

        logger.info("因子评分保存完成", count=len(factors))


# ─────────────────────────────────────────────────────────────────────────────
# 工具函数
# ─────────────────────────────────────────────────────────────────────────────

def _to_float(v) -> Optional[float]:
    """安全转换为 float，失败返回 None"""
    if v is None:
        return None
    try:
        f = float(v)
        import math
        return None if math.isnan(f) or math.isinf(f) else f
    except (TypeError, ValueError):
        return None


def _get_recent_report_periods(trade_date: date, n: int = 4) -> list[str]:
    """
    获取 trade_date 之前最近 n 个季度报告期（YYYYMMDD 格式）

    季度报告期：03-31, 06-30, 09-30, 12-31
    """
    all_periods = []
    year = trade_date.year

    # 往前推 2 年，找最近的 n 个已到期的报告期
    for y in range(year, year - 3, -1):
        for month_day in [("12", "31"), ("09", "30"), ("06", "30"), ("03", "31")]:
            period_str = f"{y}{month_day[0]}{month_day[1]}"
            try:
                period_date = date(y, int(month_day[0]), int(month_day[1]))
                # 报告期需要已过去（财报通常在 4 个月后披露，但我们用当日判断）
                if period_date <= trade_date:
                    all_periods.append(period_str)
            except ValueError:
                pass

        if len(all_periods) >= n:
            break

    return all_periods[:n]
