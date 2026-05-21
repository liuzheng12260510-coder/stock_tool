"""
跑批主流程 Pipeline — P2: Checkpoint 断点续传

版本变更（P2）：
  - 引入 job_checkpoints 表跟踪每个 step 的执行状态
  - run() 新增 resume_job_id 参数，支持显式恢复指定 job_run
  - run() 在 non-force 模式下自动发现同日期未完成任务并进入 resume 模式
  - recover_unfinished() 类方法：扫描心跳超时的 job_run 并自动重跑
  - _checkpoint_step() 通用装饰器：记录 pending→running→done/failed
  - dividend step：按年度记录 payload，支持年级断点续传
  - fina_qN：每个报告期独立 checkpoint，可单独跳过
"""
from __future__ import annotations

import contextlib
import json
from collections.abc import Callable
from datetime import date, datetime, timedelta
from typing import Any, TypeVar

import pandas as pd

from app.analytics.blacklist import build_blacklist_from_df, filter_blacklisted_codes
from app.analytics.factors import compute_all_factors
from app.analytics.filters import apply_hard_filters, apply_prescreen_filter, is_private_enterprise
from app.analytics.scoring import compute_cross_sectional_scores
from app.analytics.vectorized import batch_compute_factors, vectorized_prescreen
from app.core.config import settings
from app.core.logging import get_logger
from app.data.composite import CompositeProvider
from app.data.trade_calendar import get_latest_trade_date
from app.db.models import (
    Blacklist,
    DailySnapshot,
    DividendHistory,
    FactorScore,
    FinancialQuarter,
    JobCheckpoint,
    JobRun,
    Stock,
)
from app.db.session import db_session
from app.domain.models import DividendRecord, FactorResult, QuarterRecord

logger = get_logger("pipeline")

T = TypeVar("T")

# ── P3 灰度开关：True = 使用矩阵化算子（向量化预筛 + 批量因子计算）─────────────
#    False = 保留原 iterrows 逐票循环路径（调试用）
USE_VECTORIZED_PIPELINE: bool = True

# 心跳超时：job_run.last_heartbeat 超过此时间视为崩溃
_HEARTBEAT_TIMEOUT = timedelta(hours=1)
# 只恢复最近 24h 内启动的 job_run
_RECOVERY_WINDOW = timedelta(hours=24)

# 可断点跳过的 step（这些 step 的 DB 写入均为 upsert / INSERT OR IGNORE，重复执行安全）
_RESUMABLE_STEPS = frozenset({
    "stock_basic",
    "blacklist",
    "snapshot_save",
    "fina_q1",
    "fina_q2",
    "fina_q3",
    "fina_q4",
    "dividend",
})


class Pipeline:
    """跑批流水线 — P2 Checkpoint 断点续传版"""

    def __init__(self) -> None:
        self.provider = CompositeProvider()

    # ─────────────────────────────────────────────────────────────────────
    # 公共入口
    # ─────────────────────────────────────────────────────────────────────

    def run(
        self,
        trade_date: date | None = None,
        force: bool = False,
        resume_job_id: int | None = None,
    ) -> int:
        """
        执行完整跑批（支持断点续传）

        Args:
            trade_date:    指定交易日，None = 最近交易日
            force:         True = 忽略已有记录，创建新 job_run 全量重跑
            resume_job_id: 显式指定要恢复的 job_run.id

        Returns:
            job_run 的 id
        """
        started_at = datetime.now()

        # ── 确定本次 job_run 与 done_steps ──────────────────────────
        if resume_job_id is not None:
            # 显式恢复指定 job_run
            with db_session() as db:
                job = db.query(JobRun).filter(JobRun.id == resume_job_id).first()
                if not job:
                    raise ValueError(f"job_run {resume_job_id} 不存在")
                run_date = job.trade_date or trade_date or get_latest_trade_date()

            job_id = resume_job_id
            done_steps, checkpoint_payloads = self._load_checkpoints(job_id)

            # 重置 status 为 running
            with db_session() as db:
                db.query(JobRun).filter(JobRun.id == job_id).update({
                    "status": "running",
                    "last_heartbeat": datetime.now(),
                    "current_step": "resume",
                })
            logger.info(
                "pipeline.resume",
                job_id=job_id,
                trade_date=str(run_date),
                done_steps=sorted(done_steps),
            )

        else:
            run_date = trade_date or get_latest_trade_date()

            if not force:
                # 检查同日期是否有未完成的 job_run → 自动进入 resume 模式
                existing_id = self._find_resumable_job(run_date)
                if existing_id is not None:
                    logger.info(
                        "pipeline.found_unfinished",
                        job_id=existing_id,
                        trade_date=str(run_date),
                    )
                    return self.run(trade_date=run_date, resume_job_id=existing_id)

            # 新建 job_run
            with db_session() as db:
                job = JobRun(
                    started_at=started_at,
                    status="running",
                    trade_date=run_date,
                    last_heartbeat=started_at,
                    current_step="",
                )
                db.add(job)
                db.flush()
                job_id = job.id

            done_steps: set[str] = set()
            checkpoint_payloads: dict[str, dict] = {}
            logger.info("pipeline.start", job_id=job_id, trade_date=str(run_date), force=force)

        # ── 执行 ──────────────────────────────────────────────────────
        try:
            stocks_screened, stocks_passed = self._execute_with_checkpoints(
                run_date, job_id, done_steps, checkpoint_payloads
            )
            duration_ms = int((datetime.now() - started_at).total_seconds() * 1000)

            with db_session() as db:
                db.query(JobRun).filter(JobRun.id == job_id).update({
                    "finished_at": datetime.now(),
                    "status": "success",
                    "stocks_screened": stocks_screened,
                    "stocks_passed": stocks_passed,
                    "duration_ms": duration_ms,
                    "current_step": "done",
                })

            logger.info(
                "pipeline.done",
                job_id=job_id,
                stocks_screened=stocks_screened,
                stocks_passed=stocks_passed,
                duration_ms=duration_ms,
            )

        except Exception as e:
            logger.exception("pipeline.failed", job_id=job_id, error=str(e))
            with db_session() as db:
                db.query(JobRun).filter(JobRun.id == job_id).update({
                    "finished_at": datetime.now(),
                    "status": "failed",
                    "error": str(e)[:2000],
                    "duration_ms": int((datetime.now() - started_at).total_seconds() * 1000),
                })
            raise

        return job_id

    @classmethod
    def recover_unfinished(cls) -> None:
        """
        扫描最近 24h 内 status=running 但心跳超时（>1h）的 job_run，
        自动进入 resume 模式重跑。由 start_scheduler() 在启动时调用。
        """
        heartbeat_cutoff = datetime.now() - _HEARTBEAT_TIMEOUT
        start_cutoff = datetime.now() - _RECOVERY_WINDOW

        with db_session() as db:
            stale_jobs = db.query(JobRun).filter(
                JobRun.status == "running",
                JobRun.started_at >= start_cutoff,
            ).all()
            candidates: list[tuple[int, date | None]] = [
                (job.id, job.trade_date)
                for job in stale_jobs
                if (job.last_heartbeat is None or job.last_heartbeat < heartbeat_cutoff)
            ]

        if not candidates:
            return

        logger.info("pipeline.recover.found_stale", count=len(candidates))
        pipeline = cls()
        for job_id, td in candidates:
            logger.info("pipeline.recover.start", job_id=job_id, trade_date=str(td))
            try:
                pipeline.run(trade_date=td, resume_job_id=job_id)
            except Exception as e:
                logger.exception("pipeline.recover.failed", job_id=job_id, error=str(e))

    # ─────────────────────────────────────────────────────────────────────
    # 主执行逻辑（含 Checkpoint）
    # ─────────────────────────────────────────────────────────────────────

    def _execute_with_checkpoints(
        self,
        trade_date: date,
        job_id: int,
        done_steps: set[str],
        checkpoint_payloads: dict[str, dict],
    ) -> tuple[int, int]:
        """
        带 checkpoint 的主跑批流程。

        Returns:
            (stocks_screened, stocks_passed)
        """
        trade_date_str = trade_date.strftime("%Y%m%d")

        # ── Step 1: stock_basic ───────────────────────────────────────
        logger.info("pipeline.step.stock_basic")
        self._checkpoint_step(
            job_id, "stock_basic", done_steps,
            self._update_stock_basic, skip_if_done=False,  # T1: 每日刷新捕获 ST/退市状态
        )

        # ── Step 2: blacklist ─────────────────────────────────────────
        logger.info("pipeline.step.blacklist")
        self._checkpoint_step(
            job_id, "blacklist", done_steps,
            lambda: self._update_blacklist(trade_date), skip_if_done=False,  # T1
        )

        # ── Step 3: daily (始终拉取以构建内存 DataFrame) ────────────
        logger.info("pipeline.step.daily", trade_date=trade_date_str)
        daily_df: pd.DataFrame | None = self._checkpoint_step(
            job_id, "daily", done_steps,
            lambda: self.provider.get_daily(trade_date_str), skip_if_done=False,
        )  # type: ignore[assignment]

        # ── Step 3.5: daily_basic ──────────────────────────────────────
        logger.info("pipeline.step.daily_basic", trade_date=trade_date_str)
        basic_df: pd.DataFrame | None = self._checkpoint_step(
            job_id, "daily_basic", done_steps,
            lambda: self.provider.get_daily_basic(trade_date_str), skip_if_done=False,
        )  # type: ignore[assignment]

        if daily_df is None or basic_df is None or daily_df.empty or basic_df.empty:
            logger.warning("pipeline.market_data_empty", trade_date=trade_date_str)
            return 0, 0

        # 合并行情 DataFrame
        market_df = daily_df.merge(basic_df, on="ts_code", how="inner", suffixes=("", "_b"))
        logger.info("pipeline.market_data_merged", count=len(market_df))

        # 加载股票基础信息（用于预筛 exchange / is_private）
        stocks = self._load_stocks_dict()

        # 黑名单剔除
        all_codes: set[str] = set(market_df["ts_code"].tolist())
        blacklisted = filter_blacklisted_codes(all_codes, trade_date)
        if blacklisted:
            market_df = market_df[~market_df["ts_code"].isin(blacklisted)].copy()
            logger.info(
                "pipeline.blacklist_filtered",
                count=len(blacklisted),
                remaining=len(market_df),
            )

        # ── Step 4: prescreen (始终执行) ──────────────────────────────
        logger.info("pipeline.step.prescreen")
        prescreen_result: tuple[set[str], pd.DataFrame] | None = self._checkpoint_step(
            job_id, "prescreen", done_steps,
            lambda: self._run_prescreen(market_df, stocks), skip_if_done=False,
        )  # type: ignore[assignment]

        if prescreen_result is None:
            # 不应发生（prescreen 始终运行），保险处理
            logger.warning("pipeline.prescreen_result_none")
            return 0, 0
        eligible_set, market_df = prescreen_result
        eligible_ts_codes: list[str] = market_df["ts_code"].tolist()

        # ── Step 5: snapshot_save ──────────────────────────────────────
        logger.info("pipeline.step.snapshot_save", count=len(market_df))
        self._checkpoint_step(
            job_id, "snapshot_save", done_steps,
            lambda: self._save_daily_snapshots(market_df, trade_date),
            skip_if_done=True,
        )

        # ── Step 6: fina_q1 ~ fina_q4 ─────────────────────────────────
        logger.info("pipeline.step.fina", ts_count=len(eligible_ts_codes))
        periods = _get_recent_report_periods(trade_date, n=4)
        for i, period in enumerate(periods, 1):
            step_name = f"fina_q{i}"
            logger.info("pipeline.step.fina_period",
                        step=step_name, period=period, ts_count=len(eligible_ts_codes))
            self._checkpoint_step(
                job_id, step_name, done_steps,
                lambda p=period: self._step_fina_period(p, eligible_ts_codes),
                skip_if_done=True,
            )

        # ── Step 6.5: dividend (年度断点续传) ─────────────────────────
        logger.info("pipeline.step.dividend", trade_date=trade_date_str)
        self._step_dividend_with_checkpoint(
            job_id, trade_date, eligible_set, done_steps, checkpoint_payloads,
        )

        # ── Step 7: 从 DB 加载因子计算所需输入 ────────────────────────
        logger.info("pipeline.step.load_inputs")
        quarters_by_code, dividends_by_code = self._load_factor_inputs(eligible_ts_codes)

        # ── Step 8: factor_compute (始终执行) ─────────────────────────
        logger.info("pipeline.step.factor_compute", count=len(eligible_ts_codes))
        factor_results: list[FactorResult] | None = self._checkpoint_step(
            job_id, "factor_compute", done_steps,
            lambda: self._compute_factors(
                trade_date, market_df, stocks, quarters_by_code, dividends_by_code
            ),
            skip_if_done=False,
        )  # type: ignore[assignment]

        if not factor_results:
            logger.warning("pipeline.factor_compute_empty")
            return 0, 0
        assert factor_results is not None  # narrow type for Pylance

        # T4 post-process: 回填 close/pct_chg/amount 供硬过滤使用
        _code_mkt: dict = {r["ts_code"]: r for r in market_df.to_dict("records")}
        for _fr in factor_results:
            _mrow = _code_mkt.get(_fr.ts_code, {})
            _fr.close = _to_float(_mrow.get("close"))
            _fr.pct_chg = _to_float(_mrow.get("pct_chg"))
            _fr.amount = _to_float(_mrow.get("amount"))

        # ── Step 9: scoring (始终执行) ─────────────────────────────────
        logger.info("pipeline.step.scoring", count=len(factor_results))
        _fr_for_scoring: list[FactorResult] = factor_results
        scored: list[FactorResult] | None = self._checkpoint_step(
            job_id, "scoring", done_steps,
            lambda: compute_cross_sectional_scores(_fr_for_scoring),
            skip_if_done=False,
        )  # type: ignore[assignment]
        if scored is not None:
            factor_results = scored

        # ── Step 10: 硬性筛选（无 IO，不单独 checkpoint）────────────
        self._apply_hard_filters(factor_results, stocks)

        # ── Step 11: save_scores (始终执行，DELETE + INSERT) ──────────
        logger.info("pipeline.step.save_scores", count=len(factor_results))
        self._checkpoint_step(
            job_id, "save_scores", done_steps,
            lambda: self._save_factor_scores(factor_results, trade_date),
            skip_if_done=False,
        )

        stocks_passed = sum(1 for f in factor_results if f.passed_screening)
        logger.info(
            "pipeline.execute.done",
            total=len(factor_results),
            passed=stocks_passed,
            trade_date=trade_date_str,
        )
        return len(factor_results), stocks_passed

    # ─────────────────────────────────────────────────────────────────────
    # Checkpoint 辅助方法
    # ─────────────────────────────────────────────────────────────────────

    def _checkpoint_step(
        self,
        job_run_id: int,
        step_name: str,
        done_steps: set[str],
        fn: Callable[[], Any],
        *,
        skip_if_done: bool = True,
    ) -> Any:
        """
        通用 step 执行装饰器：前后记录 checkpoint 状态。

        Args:
            skip_if_done: True = 若 step 已 done 则跳过（适合幂等写入步骤）
                          False = 始终执行（适合内存计算/数据拉取步骤）

        Returns:
            fn() 的返回值；跳过时返回 None
        """
        if skip_if_done and step_name in done_steps:
            logger.info("pipeline.step.skip", step=step_name, reason="checkpoint_done")
            return None

        self._mark_checkpoint(job_run_id, step_name, "running")
        try:
            result = fn()
            self._mark_checkpoint(job_run_id, step_name, "done")
            return result
        except Exception as e:
            self._mark_checkpoint(job_run_id, step_name, "failed", {"error": str(e)[:500]})
            raise

    def _mark_checkpoint(
        self,
        job_run_id: int,
        step_name: str,
        status: str,
        payload: dict | None = None,
    ) -> None:
        """
        创建或更新 job_checkpoints 记录，同时刷新 job_run 的心跳时间。

        Args:
            payload: 可选 JSON 中间态（如 dividend 已完成的年度列表）
        """
        now = datetime.now()
        with db_session() as db:
            existing = db.query(JobCheckpoint).filter(
                JobCheckpoint.job_run_id == job_run_id,
                JobCheckpoint.step_name == step_name,
            ).first()

            if existing:
                existing.status = status
                if status == "running" and existing.started_at is None:
                    existing.started_at = now
                if status in ("done", "failed"):
                    existing.finished_at = now
                if payload is not None:
                    existing.payload = json.dumps(payload, ensure_ascii=False)
            else:
                cp = JobCheckpoint(
                    job_run_id=job_run_id,
                    step_name=step_name,
                    status=status,
                    started_at=now if status == "running" else None,
                    finished_at=now if status in ("done", "failed") else None,
                    payload=(
                        json.dumps(payload, ensure_ascii=False)
                        if payload is not None
                        else ""
                    ),
                )
                db.add(cp)

            # 同步刷新 job_run 的心跳与当前 step
            db.query(JobRun).filter(JobRun.id == job_run_id).update({
                "current_step": step_name,
                "last_heartbeat": now,
            })

    def _load_checkpoints(
        self, job_run_id: int
    ) -> tuple[set[str], dict[str, dict]]:
        """
        加载 job_run 的已完成 step 集合和各 step 的 payload。

        Returns:
            (done_steps: set, payload_by_step: dict)
        """
        with db_session() as db:
            checkpoints = db.query(JobCheckpoint).filter(
                JobCheckpoint.job_run_id == job_run_id
            ).all()

        done_steps: set[str] = set()
        payloads: dict[str, dict] = {}
        for cp in checkpoints:
            if cp.status == "done":
                done_steps.add(cp.step_name)
            if cp.payload:
                with contextlib.suppress(Exception):
                    payloads[cp.step_name] = json.loads(cp.payload)
        return done_steps, payloads

    def _find_resumable_job(self, trade_date: date) -> int | None:
        """
        查找同交易日内未完成（status=running）的 job_run id。
        用于 non-force run() 时的自动 resume 检测。
        """
        with db_session() as db:
            job = (
                db.query(JobRun)
                .filter(
                    JobRun.trade_date == trade_date,
                    JobRun.status == "running",
                )
                .order_by(JobRun.id.desc())
                .first()
            )
            return job.id if job else None

    # ─────────────────────────────────────────────────────────────────────
    # 特殊 Step 实现
    # ─────────────────────────────────────────────────────────────────────

    def _step_dividend_with_checkpoint(
        self,
        job_run_id: int,
        trade_date: date,
        eligible_set: set[str],
        done_steps: set[str],
        checkpoint_payloads: dict[str, dict],
    ) -> None:
        """
        Step 6.5 — 分红历史（年度级别断点续传）

        每完成一个年度后立即更新 payload：
            {"completed_years": [2024, 2023, ...]}
        重启时跳过 completed_years 中已有的年度，继续未完成的年度。
        """
        step_name = "dividend"
        if step_name in done_steps:
            logger.info("pipeline.step.skip", step=step_name, reason="checkpoint_done")
            return

        if not eligible_set:
            self._mark_checkpoint(job_run_id, step_name, "done", {"completed_years": []})
            return

        existing_payload = checkpoint_payloads.get(step_name, {})
        completed_years: list[int] = existing_payload.get("completed_years", [])
        completed_set: set[int] = set(completed_years)

        self._mark_checkpoint(
            job_run_id, step_name, "running",
            {"completed_years": sorted(completed_set)},
        )

        saved_total = 0
        try:
            current_year = trade_date.year
            for year_offset in range(5):
                year = current_year - year_offset
                if year in completed_set:
                    logger.info("pipeline.dividend.year.skip", year=year)
                    continue

                end_date_str = f"{year}1231"
                try:
                    df = self.provider.get_dividend_batch(end_date_str)
                except Exception as e:
                    logger.warning(
                        "pipeline.dividend_batch_error",
                        year=year,
                        error=str(e)[:120],
                    )
                    df = pd.DataFrame()

                if not df.empty:
                    if "ts_code" in df.columns:
                        df = df[df["ts_code"].isin(eligible_set)].copy()
                    if "div_proc" in df.columns:
                        df = df[df["div_proc"] == "实施"].copy()
                    if not df.empty:
                        saved_total += self._save_dividend_history(df)

                completed_set.add(year)
                # 每完成一年立即持久化 payload（崩溃可从此处续传）
                self._mark_checkpoint(
                    job_run_id, step_name, "running",
                    {"completed_years": sorted(completed_set)},
                )

            self._mark_checkpoint(
                job_run_id, step_name, "done",
                {"completed_years": sorted(completed_set)},
            )
            logger.info(
                "pipeline.step6_5.saved",
                trade_date=str(trade_date),
                total_saved=saved_total,
            )
        except Exception as e:
            self._mark_checkpoint(
                job_run_id, step_name, "failed",
                {"completed_years": sorted(completed_set), "error": str(e)[:500]},
            )
            raise

    def _step_fina_period(self, period: str, ts_codes: list[str]) -> None:
        """拉取并保存单个报告期的季度财务数据"""
        try:
            df = self.provider.get_financial_quarterly(
                period=period,
                ts_codes=ts_codes,
            )
            if not df.empty:
                self._save_financial_quarters(df)
        except Exception as e:
            logger.warning("pipeline.fina_period_error", period=period, error=str(e))
            raise

    # ─────────────────────────────────────────────────────────────────────
    # 内存计算 Step（这些不 skip，始终执行）
    # ─────────────────────────────────────────────────────────────────────

    def _run_prescreen(
        self, market_df: pd.DataFrame, stocks: dict[str, dict]
    ) -> tuple[set[str], pd.DataFrame]:
        """
        预筛：用 pe_ttm + total_mv 过滤明显不合格的股票，返回 (eligible_set, filtered_df)

        P3：USE_VECTORIZED_PIPELINE=True 时走矩阵化路径（20-50× 提速），
        False 时保留原 iterrows 逐票循环（调试/回退用）。
        """
        if USE_VECTORIZED_PIPELINE:
            return vectorized_prescreen(
                market_df,
                stocks,
                exclude_bj=settings.screen_exclude_bj,
                prescreen_pe_ttm_max=settings.prescreen_pe_ttm_max,
                prescreen_total_mv_max_yi=settings.prescreen_total_mv_max_yi,
            )

        # ── 原 iterrows 路径（灰度开关 OFF 时保留）────────────────────────
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
                logger.debug("prescreen.skip", ts_code=ts_code, reason=skip_reason)

        filtered_df = market_df[market_df["ts_code"].isin(eligible_set)].copy()
        logger.info(
            "pipeline.prescreen.done",
            total=len(market_df),
            eligible=len(eligible_set),
            skipped=prescreen_skipped,
        )
        return eligible_set, filtered_df

    def _compute_factors(
        self,
        trade_date: date,
        market_df: pd.DataFrame,
        stocks: dict[str, dict],
        quarters_by_code: dict[str, list[QuarterRecord]],
        dividends_by_code: dict[str, list[DividendRecord]],
    ) -> list[FactorResult]:
        """
        为每只股票计算因子（含 P1 杜邦/盈余质量/分红连续性）

        P3：USE_VECTORIZED_PIPELINE=True 时走 batch_compute_factors（5-8× 提速），
        False 时保留原 iterrows + compute_all_factors 逐票循环（调试/回退用）。
        """
        if USE_VECTORIZED_PIPELINE:
            return batch_compute_factors(
                trade_date,
                market_df,
                stocks,
                quarters_by_code,
                dividends_by_code,
            )

        # ── 原 iterrows 路径（灰度开关 OFF 时保留）────────────────────────
        factor_results: list[FactorResult] = []

        for _, row in market_df.iterrows():
            ts_code = str(row["ts_code"])
            stock = stocks.get(ts_code)

            if not stock:
                stock = {}

            total_mv = _to_float(row.get("total_mv"))
            pe_ttm = _to_float(row.get("pe_ttm"))
            pb = _to_float(row.get("pb"))
            dv_ttm = _to_float(row.get("dv_ttm"))

            quarters = quarters_by_code.get(ts_code, [])
            dividends = dividends_by_code.get(ts_code, [])

            factor = compute_all_factors(
                ts_code=ts_code,
                trade_date=trade_date,
                quarters=quarters,
                total_mv_wan=total_mv,
                pe_ttm=pe_ttm,
                pb=pb,
                dv_ttm=dv_ttm,
                dividends=dividends,
            )
            factor_results.append(factor)

        logger.info("pipeline.factor_compute.done", total=len(factor_results))
        return factor_results

    def _apply_hard_filters(
        self, factor_results: list[FactorResult], stocks: dict[str, dict]
    ) -> None:
        """硬性筛选（含 P1 新增阈值），in-place 修改 factor_results"""
        for f in factor_results:
            stock = stocks.get(f.ts_code)
            is_private = stock["is_private"] if stock else True
            exchange = stock["exchange"] if stock else ""
            total_mv_yi = f.total_mv / 10000.0 if f.total_mv else None

            passed, reasons = apply_hard_filters(
                ts_code=f.ts_code,
                exchange=exchange,
                is_private=is_private,
                pe_ttm=f.pe_ttm,
                pe_deduct_ttm=f.pe_deduct_ttm,
                dv_ttm=f.dv_ttm,
                total_mv_yi=total_mv_yi,
                high_leverage_flag=f.high_leverage_flag,
                cfo_to_np_ratio=f.cfo_to_np_ratio,
                dividend_continuity=f.dividend_continuity,
                clearance_dividend_flag=f.clearance_dividend_flag,
                exclude_bj=settings.screen_exclude_bj,
                exclude_soe=settings.screen_exclude_soe,
                pe_ttm_max=settings.screen_pe_ttm_max,
                pe_deduct_max=settings.screen_pe_deduct_max,
                dv_ttm_min=settings.screen_dv_ttm_min,
                total_mv_max_yi=settings.screen_total_mv_max_yi,
                max_eqt_multiplier=settings.screen_max_eqt_multiplier,
                min_cfo_to_np=settings.screen_min_cfo_to_np,
                min_div_continuity_years=settings.screen_min_div_continuity_years,
                reject_clearance_div=settings.screen_reject_clearance_div,
            )
            f.passed_screening = passed
            f.fail_reasons = reasons

        # 最低综合分过滤
        for f in factor_results:
            if (
                f.passed_screening
                and f.composite_score is not None
                and f.composite_score < settings.screen_min_composite_score
            ):
                f.passed_screening = False
                f.fail_reasons.append(
                    f"综合评分={f.composite_score:.1f}<{settings.screen_min_composite_score}"
                )

    # ─────────────────────────────────────────────────────────────────────
    # DB 加载辅助
    # ─────────────────────────────────────────────────────────────────────

    def _load_stocks_dict(self) -> dict[str, dict]:
        """从 DB 加载所有股票的基础信息（exchange, is_private, name, industry）"""
        with db_session() as db:
            return {
                s.ts_code: {
                    "exchange": s.exchange or "",
                    "is_private": bool(s.is_private),
                    "name": s.name or "",
                    "industry": s.industry or "",
                }
                for s in db.query(Stock).all()
            }

    def _load_factor_inputs(
        self, eligible_ts_codes: list[str]
    ) -> tuple[dict[str, list[QuarterRecord]], dict[str, list[DividendRecord]]]:
        """从 DB 加载季度财务和分红历史（用于因子计算）"""
        with db_session() as db:
            quarters_raw: list[QuarterRecord] = [
                QuarterRecord(
                    ts_code=q.ts_code,
                    end_date=q.end_date,
                    q_dtprofit=q.q_dtprofit or 0.0,
                    total_cur_assets=q.total_cur_assets or 0.0,
                    total_liab=q.total_liab or 0.0,
                    op_revenue=q.op_revenue or 0.0,
                    source=q.source or "tushare",
                    roe=q.roe,
                    npta=q.npta,
                    debt_to_assets=q.debt_to_assets,
                    assets_turn=q.assets_turn,
                    eqt_multiplier=q.eqt_multiplier,
                    netprofit_yoy=q.netprofit_yoy,
                    op_yoy=q.op_yoy,
                    n_cashflow_act=q.n_cashflow_act,
                    cfo_to_np=q.cfo_to_np,
                )
                for q in db.query(FinancialQuarter).filter(
                    FinancialQuarter.ts_code.in_(eligible_ts_codes)
                ).all()
            ]

        quarters_by_code: dict[str, list[QuarterRecord]] = {}
        for q in quarters_raw:
            quarters_by_code.setdefault(q.ts_code, []).append(q)

        with db_session() as db:
            dividends_by_code: dict[str, list[DividendRecord]] = {}
            for dh in db.query(DividendHistory).filter(
                DividendHistory.ts_code.in_(eligible_ts_codes)
            ).all():
                rec = DividendRecord(
                    ts_code=dh.ts_code,
                    end_date=dh.end_date,
                    div_proc=dh.div_proc or "",
                    stk_div=dh.stk_div,
                    cash_div=dh.cash_div,
                    cash_div_tax=dh.cash_div_tax,
                    ann_date=dh.ann_date,
                    base_date=dh.base_date,
                    pay_date=dh.pay_date,
                    record_date=dh.record_date,
                    ex_date=dh.ex_date,
                )
                dividends_by_code.setdefault(dh.ts_code, []).append(rec)

        return quarters_by_code, dividends_by_code

    # ─────────────────────────────────────────────────────────────────────
    # DB 写入辅助方法（保持向后兼容）
    # ─────────────────────────────────────────────────────────────────────

    def _update_stock_basic(self) -> None:
        """更新股票基础信息（stocks 表，upsert）"""
        df = self.provider.get_stock_basic()
        if df.empty:
            return

        company_df = pd.DataFrame()
        try:
            company_df = self.provider.get_company_info_batch()
        except Exception as e:
            logger.warning("pipeline.company_info_failed", error=str(e))

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
                    existing.list_status = str(row.get("list_status", "L") or "L")  # T1
                else:
                    list_date_raw = row.get("list_date")
                    list_date = None
                    if list_date_raw and str(list_date_raw).strip():
                        with contextlib.suppress(ValueError):
                            list_date = datetime.strptime(str(list_date_raw), "%Y%m%d").date()

                    stock = Stock(
                        ts_code=ts_code,
                        name=str(row.get("name", "")),
                        industry=str(row.get("industry", "") or ""),
                        exchange=str(row.get("exchange", "") or ""),
                        list_date=list_date,
                        act_ent_type=act_ent_type,
                        act_name=act_name,
                        is_private=is_private,
                        list_status=str(row.get("list_status", "L") or "L"),  # T1
                    )
                    db.add(stock)

        logger.info("pipeline.stock_basic.done", count=len(df))

    def _update_blacklist(self, trade_date: date) -> None:
        """更新黑名单表（upsert），同步 stocks.is_blacklisted 标志"""
        with db_session() as db:
            # T1: 增加 list_status，传给 build_blacklist_from_df，激活 D/P 最高优先剔除
            stock_rows = db.query(
                Stock.ts_code, Stock.name, Stock.list_date, Stock.list_status
            ).all()

        if not stock_rows:
            logger.warning("pipeline.blacklist.stocks_empty")
            return

        stock_df = pd.DataFrame([
            {
                "ts_code": r.ts_code,
                "name": r.name or "",
                "list_date": r.list_date,
                "list_status": r.list_status or "L",
            }
            for r in stock_rows
        ])

        bl_records = build_blacklist_from_df(
            stock_df,
            today=trade_date,
            min_list_years=settings.screen_min_list_years,
            exclude_bj=settings.screen_exclude_bj,
        )

        with db_session() as db:
            for rec in bl_records:
                ts_code = rec["ts_code"]
                reason = rec["reason"]
                expires_at = rec.get("expires_at")

                existing_bl = db.query(Blacklist).filter(
                    Blacklist.ts_code == ts_code
                ).first()
                if existing_bl:
                    existing_bl.reason = reason
                    existing_bl.expires_at = expires_at
                else:
                    db.add(Blacklist(ts_code=ts_code, reason=reason, expires_at=expires_at))

            db.query(Stock).update({
                "is_blacklisted": False,
                "is_st": False,
                "is_new_listing": False,
            })
            for rec in bl_records:
                ts_code = rec["ts_code"]
                reason = rec["reason"]
                stock_obj = db.query(Stock).filter(Stock.ts_code == ts_code).first()
                if stock_obj:
                    stock_obj.is_blacklisted = True
                    stock_obj.is_st = reason in ("ST", "*ST", "PT", "delist_risk")
                    stock_obj.is_new_listing = reason == "new_listing"

        logger.info(
            "pipeline.blacklist.done",
            total_blacklisted=len(bl_records),
            trade_date=str(trade_date),
        )

    def _save_daily_snapshots(self, df: pd.DataFrame, trade_date: date) -> None:
        """保存日行情快照（INSERT OR IGNORE）"""
        with db_session() as db:
            for _, row in df.iterrows():
                ts_code = str(row["ts_code"])
                existing = db.query(DailySnapshot).filter(
                    DailySnapshot.ts_code == ts_code,
                    DailySnapshot.trade_date == trade_date,
                ).first()
                if existing:
                    continue

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

        logger.info("pipeline.snapshot.done", trade_date=str(trade_date))

    def _save_financial_quarters(self, df: pd.DataFrame) -> None:
        """
        保存季度财务数据（upsert）
        P1 变更：写入新增的杜邦/盈余质量字段；已存在记录仅更新 None 字段
        """
        seen_in_batch: set[tuple[str, date]] = set()
        saved = 0
        updated = 0

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

                key = (ts_code, end_date)
                if key in seen_in_batch:
                    continue
                seen_in_batch.add(key)

                ann_date = None
                ann_raw = row.get("ann_date")
                if ann_raw and str(ann_raw).strip():
                    with contextlib.suppress(ValueError):
                        ann_date = datetime.strptime(str(ann_raw), "%Y%m%d").date()

                p1_fields = {
                    "roe":            _to_float(row.get("roe")),
                    "npta":           _to_float(row.get("npta")),
                    "debt_to_assets": _to_float(row.get("debt_to_assets")),
                    "assets_turn":    _to_float(row.get("assets_turn")),
                    "eqt_multiplier": _to_float(row.get("eqt_multiplier")),
                    "netprofit_yoy":  _to_float(row.get("netprofit_yoy")),
                    "op_yoy":         _to_float(row.get("op_yoy")),
                    "n_cashflow_act": _to_float(row.get("n_cashflow_act")),
                    "cfo_to_np": (
                        _to_float(row.get("cfo_to_np"))
                        or _to_float(row.get("ocf_to_profit"))
                    ),
                }

                existing = db.query(FinancialQuarter).filter(
                    FinancialQuarter.ts_code == ts_code,
                    FinancialQuarter.end_date == end_date,
                ).first()

                if existing:
                    changed = False
                    for field_name, val in p1_fields.items():
                        if val is not None and getattr(existing, field_name) is None:
                            setattr(existing, field_name, val)
                            changed = True
                    if changed:
                        updated += 1
                    continue

                fq = FinancialQuarter(
                    ts_code=ts_code,
                    end_date=end_date,
                    q_dtprofit=_to_float(row.get("q_dtprofit")),
                    total_cur_assets=_to_float(row.get("total_cur_assets")),
                    total_liab=_to_float(row.get("total_liab")),
                    op_revenue=_to_float(row.get("op_revenue")),
                    ann_date=ann_date,
                    source="tushare",
                    **p1_fields,
                )
                db.add(fq)
                saved += 1

        logger.info("pipeline.fina.saved", saved=saved, updated=updated, total_rows=len(df))

    def _save_dividend_history(self, df: pd.DataFrame) -> int:
        """
        保存分红历史（INSERT OR IGNORE）

        Returns:
            实际写入条数
        """
        saved = 0
        seen_in_batch: set[tuple[str, date]] = set()

        with db_session() as db:
            for _, row in df.iterrows():
                ts_code = str(row.get("ts_code", ""))
                end_date_raw = row.get("end_date")
                if not ts_code or not end_date_raw:
                    continue

                try:
                    end_date = _parse_date(str(end_date_raw))
                except ValueError:
                    continue

                if end_date is None:
                    continue

                key = (ts_code, end_date)
                if key in seen_in_batch:
                    continue
                seen_in_batch.add(key)

                existing = db.query(DividendHistory).filter(
                    DividendHistory.ts_code == ts_code,
                    DividendHistory.end_date == end_date,
                ).first()
                if existing:
                    continue

                dh = DividendHistory(
                    ts_code=ts_code,
                    end_date=end_date,
                    ann_date=_parse_date(str(row.get("ann_date", "") or "")),
                    div_proc=str(row.get("div_proc", "") or ""),
                    stk_div=_to_float(row.get("stk_div")),
                    cash_div=_to_float(row.get("cash_div")),
                    cash_div_tax=_to_float(row.get("cash_div_tax")),
                    base_date=_parse_date(str(row.get("base_date", "") or "")),
                    pay_date=_parse_date(str(row.get("pay_date", "") or "")),
                    record_date=_parse_date(str(row.get("record_date", "") or "")),
                    ex_date=_parse_date(str(row.get("ex_date", "") or "")),
                )
                db.add(dh)
                saved += 1

        return saved

    def _save_factor_scores(self, factors: list[FactorResult], trade_date: date) -> None:
        """保存因子评分（先删当日旧数据，再批量插入；P1：写入新字段）"""
        with db_session() as db:
            db.query(FactorScore).filter(FactorScore.trade_date == trade_date).delete()

            for f in factors:
                score = FactorScore(
                    ts_code=f.ts_code,
                    trade_date=f.trade_date,
                    pe_deduct_ttm=f.pe_deduct_ttm,
                    growth_rate=f.growth_rate,
                    volatility=f.volatility,
                    safety_margin=f.safety_margin,
                    dv_ttm=f.dv_ttm,          # T2: 冗余存储，防 DailySnapshot 缺失
                    value_score=f.value_score,
                    growth_score=f.growth_score,
                    stability_score=f.stability_score,
                    dividend_score=f.dividend_score,
                    safety_score=f.safety_score,
                    composite_score=f.composite_score,
                    composite_rank=f.composite_rank,
                    passed_screening=f.passed_screening,
                    fail_reasons=json.dumps(f.fail_reasons, ensure_ascii=False),
                    dupont_score=f.dupont_score,
                    high_leverage_flag=f.high_leverage_flag,
                    earnings_quality_score=f.earnings_quality_score,
                    dividend_continuity=f.dividend_continuity,
                    dividend_continuity_score=f.dividend_continuity_score,
                )
                db.add(score)

        logger.info("pipeline.scores.saved", count=len(factors))


# ─────────────────────────────────────────────────────────────────────────────
# 工具函数
# ─────────────────────────────────────────────────────────────────────────────

def _to_float(v: Any) -> float | None:
    """安全转换为 float，失败返回 None"""
    if v is None:
        return None
    try:
        import math  # noqa: PLC0415
        f = float(v)
        return None if math.isnan(f) or math.isinf(f) else f
    except (TypeError, ValueError):
        return None


def _parse_date(s: str) -> date | None:
    """安全解析 YYYYMMDD 或 ISO 格式日期字符串，失败返回 None"""
    if not s or s.strip() in ("", "None", "nan", "NaT"):
        return None
    s = s.strip()
    if len(s) == 8 and s.isdigit():
        try:
            return datetime.strptime(s, "%Y%m%d").date()
        except ValueError:
            return None
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        return None


def _get_recent_report_periods(trade_date: date, n: int = 4) -> list[str]:
    """
    获取 trade_date 之前最近 n 个季度报告期（YYYYMMDD 格式）

    季度报告期：03-31, 06-30, 09-30, 12-31
    """
    all_periods: list[str] = []
    year = trade_date.year

    for y in range(year, year - 3, -1):
        for month_day in [("12", "31"), ("09", "30"), ("06", "30"), ("03", "31")]:
            period_str = f"{y}{month_day[0]}{month_day[1]}"
            try:
                period_date = date(y, int(month_day[0]), int(month_day[1]))
                if period_date <= trade_date:
                    all_periods.append(period_str)
            except ValueError:
                pass

        if len(all_periods) >= n:
            break

    return all_periods[:n]
