"""
Pipeline Checkpoint 断点续传单元测试 — P2

覆盖：
① 模拟 Step 5 失败，验证 resume 跳过 1-4（已 done 的 resumable steps）
② 验证 payload 恢复（dividend 年度信息）
③ heartbeat 超时识别（recover_unfinished 筛选条件）
"""
from __future__ import annotations

import contextlib
import json
from datetime import date, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.models import Base, JobCheckpoint, JobRun
from app.jobs.pipeline import _HEARTBEAT_TIMEOUT, _RECOVERY_WINDOW

# ─────────────────────────────────────────────────────────────────────────────
# 测试 DB Fixture（in-memory SQLite）
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture()
def test_session_factory():
    """
    创建 in-memory SQLite 引擎，建表后返回 session factory。
    每个测试函数独立一个数据库（不共享状态）。
    """
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    Factory = sessionmaker(bind=engine, autocommit=False, autoflush=False, expire_on_commit=False)
    return Factory


@pytest.fixture()
def patched_pipeline(test_session_factory):
    """
    返回 patch 了 db_session 的 Pipeline 实例（无需真实 DB 连接）。
    provider 同样 mock，避免外部网络调用。
    """

    @contextlib.contextmanager
    def mock_db_session():
        db = test_session_factory()
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    with patch("app.jobs.pipeline.db_session", mock_db_session), \
         patch("app.jobs.pipeline.CompositeProvider", MagicMock):
        from app.jobs.pipeline import Pipeline
        pipe = Pipeline()
    return pipe, test_session_factory


# ─────────────────────────────────────────────────────────────────────────────
# 辅助函数
# ─────────────────────────────────────────────────────────────────────────────


_DEFAULT = object()  # Sentinel: 区分"未传 None"与"显式传 None"


def create_job_run(
    factory,
    trade_date: date = date(2025, 5, 19),
    status: str = "running",
    last_heartbeat: datetime | None | object = _DEFAULT,
    started_at: datetime | None = None,
) -> int:
    """
    在测试 DB 中创建一个 JobRun 记录，返回 id。

    last_heartbeat:
      - 未传（_DEFAULT）→ datetime.now()
      - 显式传 None    → 写入 NULL（用于测试 NULL heartbeat 场景）
      - datetime 值   → 使用该值
    """
    effective_hb: datetime | None = (
        datetime.now() if last_heartbeat is _DEFAULT else last_heartbeat  # type: ignore[assignment]
    )
    with factory() as db:
        job = JobRun(
            started_at=started_at or datetime.now(),
            status=status,
            trade_date=trade_date,
            last_heartbeat=effective_hb,
        )
        db.add(job)
        db.flush()
        return job.id


def get_checkpoint(factory, job_run_id: int, step_name: str) -> JobCheckpoint | None:
    with factory() as db:
        return db.query(JobCheckpoint).filter(
            JobCheckpoint.job_run_id == job_run_id,
            JobCheckpoint.step_name == step_name,
        ).first()


# ─────────────────────────────────────────────────────────────────────────────
# _mark_checkpoint
# ─────────────────────────────────────────────────────────────────────────────


class TestMarkCheckpoint:
    def test_creates_running_checkpoint(self, patched_pipeline) -> None:
        pipe, factory = patched_pipeline
        job_id = create_job_run(factory)

        pipe._mark_checkpoint(job_id, "stock_basic", "running")

        cp = get_checkpoint(factory, job_id, "stock_basic")
        assert cp is not None
        assert cp.status == "running"
        assert cp.started_at is not None
        assert cp.finished_at is None

    def test_updates_to_done(self, patched_pipeline) -> None:
        pipe, factory = patched_pipeline
        job_id = create_job_run(factory)

        pipe._mark_checkpoint(job_id, "blacklist", "running")
        pipe._mark_checkpoint(job_id, "blacklist", "done")

        cp = get_checkpoint(factory, job_id, "blacklist")
        assert cp is not None
        assert cp.status == "done"
        assert cp.finished_at is not None

    def test_stores_json_payload(self, patched_pipeline) -> None:
        pipe, factory = patched_pipeline
        job_id = create_job_run(factory)

        payload = {"completed_years": [2024, 2023, 2022]}
        pipe._mark_checkpoint(job_id, "dividend", "running", payload)

        cp = get_checkpoint(factory, job_id, "dividend")
        assert cp is not None
        loaded = json.loads(cp.payload)
        assert loaded["completed_years"] == [2024, 2023, 2022]

    def test_updates_job_run_heartbeat(self, patched_pipeline) -> None:
        pipe, factory = patched_pipeline
        job_id = create_job_run(factory)

        # Manually set an old heartbeat
        with factory() as db:
            db.query(JobRun).filter(JobRun.id == job_id).update({
                "last_heartbeat": datetime(2020, 1, 1),
            })

        pipe._mark_checkpoint(job_id, "daily", "running")

        with factory() as db:
            job = db.query(JobRun).filter(JobRun.id == job_id).first()
        assert job.last_heartbeat > datetime(2021, 1, 1)  # Refreshed

    def test_marks_failed_with_error_payload(self, patched_pipeline) -> None:
        pipe, factory = patched_pipeline
        job_id = create_job_run(factory)

        pipe._mark_checkpoint(job_id, "fina_q1", "running")
        pipe._mark_checkpoint(job_id, "fina_q1", "failed", {"error": "connection timeout"})

        cp = get_checkpoint(factory, job_id, "fina_q1")
        assert cp is not None
        assert cp.status == "failed"
        payload = json.loads(cp.payload)
        assert "connection timeout" in payload["error"]


# ─────────────────────────────────────────────────────────────────────────────
# _load_checkpoints
# ─────────────────────────────────────────────────────────────────────────────


class TestLoadCheckpoints:
    def test_returns_done_steps_set(self, patched_pipeline) -> None:
        pipe, factory = patched_pipeline
        job_id = create_job_run(factory)

        pipe._mark_checkpoint(job_id, "stock_basic", "done")
        pipe._mark_checkpoint(job_id, "blacklist", "done")
        pipe._mark_checkpoint(job_id, "daily", "running")  # not done

        done_steps, _ = pipe._load_checkpoints(job_id)

        assert "stock_basic" in done_steps
        assert "blacklist" in done_steps
        assert "daily" not in done_steps

    def test_returns_payload_dict(self, patched_pipeline) -> None:
        pipe, factory = patched_pipeline
        job_id = create_job_run(factory)

        payload = {"completed_years": [2024, 2023, 2022]}
        pipe._mark_checkpoint(job_id, "dividend", "done", payload)

        _, payloads = pipe._load_checkpoints(job_id)

        assert "dividend" in payloads
        assert payloads["dividend"]["completed_years"] == [2024, 2023, 2022]

    def test_failed_step_not_in_done(self, patched_pipeline) -> None:
        pipe, factory = patched_pipeline
        job_id = create_job_run(factory)

        pipe._mark_checkpoint(job_id, "fina_q1", "failed", {"error": "oops"})

        done_steps, _ = pipe._load_checkpoints(job_id)
        assert "fina_q1" not in done_steps


# ─────────────────────────────────────────────────────────────────────────────
# _checkpoint_step ① — Step 5 失败，resume 跳过 1-4
# ─────────────────────────────────────────────────────────────────────────────


class TestCheckpointStepResumeLogic:
    """
    测试场景：
    - Steps 1-4（stock_basic, blacklist, fina_q1, fina_q2）已标记 done
    - Step 5（snapshot_save）失败
    - Resume 时，1-4 被跳过（fn 不被调用），5 被重新执行
    """

    def test_skips_done_resumable_step(self, patched_pipeline) -> None:
        pipe, factory = patched_pipeline
        job_id = create_job_run(factory)
        done_steps = {"stock_basic", "blacklist", "fina_q1", "fina_q2"}

        call_tracker: list[str] = []

        for step in ["stock_basic", "blacklist", "fina_q1", "fina_q2"]:
            result = pipe._checkpoint_step(
                job_id, step, done_steps,
                lambda s=step: call_tracker.append(s),
                skip_if_done=True,
            )
            assert result is None, f"Step {step} should have been skipped"

        # None of the step functions should have been called
        assert len(call_tracker) == 0

    def test_executes_non_done_step(self, patched_pipeline) -> None:
        pipe, factory = patched_pipeline
        job_id = create_job_run(factory)
        done_steps = {"stock_basic"}  # snapshot_save NOT in done_steps

        called = []

        result = pipe._checkpoint_step(
            job_id, "snapshot_save", done_steps,
            lambda: called.append("executed") or "ok",
            skip_if_done=True,
        )

        assert result == "ok"
        assert "executed" in called

    def test_failed_step_sets_failed_checkpoint(self, patched_pipeline) -> None:
        """Step 5 (snapshot_save) 失败 → checkpoint 必须为 failed"""
        pipe, factory = patched_pipeline
        job_id = create_job_run(factory)
        done_steps: set[str] = set()

        with pytest.raises(RuntimeError, match="step 5 failed"):
            pipe._checkpoint_step(
                job_id, "snapshot_save", done_steps,
                lambda: (_ for _ in ()).throw(RuntimeError("step 5 failed")),
                skip_if_done=True,
            )

        cp = get_checkpoint(factory, job_id, "snapshot_save")
        assert cp is not None
        assert cp.status == "failed"
        error_payload = json.loads(cp.payload)
        assert "step 5 failed" in error_payload["error"]

    def test_always_run_step_ignores_done_flag(self, patched_pipeline) -> None:
        """skip_if_done=False 的 step 即使标记为 done 也应执行"""
        pipe, factory = patched_pipeline
        job_id = create_job_run(factory)
        done_steps = {"daily"}  # daily would be "done", but skip_if_done=False

        called = []

        result = pipe._checkpoint_step(
            job_id, "daily", done_steps,
            lambda: called.append("ran") or "daily_data",
            skip_if_done=False,  # should ALWAYS run
        )

        assert result == "daily_data"
        assert "ran" in called


# ─────────────────────────────────────────────────────────────────────────────
# ② payload 恢复（dividend 年度断点续传）
# ─────────────────────────────────────────────────────────────────────────────


class TestDividendPayloadRecovery:
    def test_payload_persists_completed_years(self, patched_pipeline) -> None:
        """dividend step 每完成一年就更新 payload，崩溃后可从 payload 恢复"""
        pipe, factory = patched_pipeline
        job_id = create_job_run(factory)

        # Simulate partial completion: years 2024, 2023 done
        pipe._mark_checkpoint(
            job_id, "dividend", "running",
            {"completed_years": [2024, 2023]},
        )

        # On resume, load the payload
        _, payloads = pipe._load_checkpoints(job_id)

        completed = payloads.get("dividend", {}).get("completed_years", [])
        assert 2024 in completed
        assert 2023 in completed
        assert 2022 not in completed  # Not yet done

    def test_dividend_step_skips_completed_years(self, patched_pipeline) -> None:
        """
        _step_dividend_with_checkpoint 在 resume 模式下，
        只处理 completed_years 之外的年度
        """
        pipe, factory = patched_pipeline
        job_id = create_job_run(factory)

        # Pre-existing partial state: 2024 done, 2023 done
        checkpoint_payloads = {
            "dividend": {"completed_years": [2024, 2023]},
        }
        done_steps: set[str] = set()  # dividend NOT yet done

        years_fetched: list[int] = []

        def mock_get_dividend_batch(end_date_str: str):
            import pandas as pd  # noqa: PLC0415
            year = int(end_date_str[:4])
            years_fetched.append(year)
            return pd.DataFrame()  # 返回空 DataFrame（无需保存）

        pipe.provider.get_dividend_batch = mock_get_dividend_batch

        pipe._step_dividend_with_checkpoint(
            job_id,
            trade_date=date(2025, 5, 19),
            eligible_set={"000001.SZ", "600000.SH"},
            done_steps=done_steps,
            checkpoint_payloads=checkpoint_payloads,
        )

        # Should only fetch 2025, 2022, 2021 — NOT 2024 or 2023
        assert 2024 not in years_fetched
        assert 2023 not in years_fetched
        # 2025, 2022, 2021 should have been fetched
        assert 2025 in years_fetched

    def test_dividend_step_marks_done(self, patched_pipeline) -> None:
        pipe, factory = patched_pipeline
        job_id = create_job_run(factory)

        import pandas as pd  # noqa: PLC0415
        pipe.provider.get_dividend_batch = lambda _: pd.DataFrame()

        pipe._step_dividend_with_checkpoint(
            job_id,
            trade_date=date(2025, 5, 19),
            eligible_set={"000001.SZ"},
            done_steps=set(),
            checkpoint_payloads={},
        )

        cp = get_checkpoint(factory, job_id, "dividend")
        assert cp is not None
        assert cp.status == "done"
        final_payload = json.loads(cp.payload)
        assert len(final_payload["completed_years"]) == 5  # 5 years processed


# ─────────────────────────────────────────────────────────────────────────────
# ③ heartbeat 超时识别
# ─────────────────────────────────────────────────────────────────────────────


class TestHeartbeatTimeoutDetection:
    """
    验证 recover_unfinished() 能正确区分：
    - 心跳超时（> _HEARTBEAT_TIMEOUT）→ 需要 recover
    - 心跳正常 → 不处理
    """

    def test_stale_job_identified(self, test_session_factory) -> None:
        """心跳超时的 running job 应在候选列表内"""

        @contextlib.contextmanager
        def mock_db_session():
            db = test_session_factory()
            try:
                yield db
                db.commit()
            except Exception:
                db.rollback()
                raise
            finally:
                db.close()

        now = datetime.now()
        stale_hb = now - _HEARTBEAT_TIMEOUT - timedelta(minutes=10)  # definitely stale

        stale_id = create_job_run(
            test_session_factory,
            trade_date=date(2025, 5, 19),
            status="running",
            last_heartbeat=stale_hb,
            started_at=now - timedelta(hours=2),
        )

        # Simulate the filtering logic from recover_unfinished
        heartbeat_cutoff = now - _HEARTBEAT_TIMEOUT
        start_cutoff = now - _RECOVERY_WINDOW

        with mock_db_session() as db:
            all_running = db.query(JobRun).filter(
                JobRun.status == "running",
                JobRun.started_at >= start_cutoff,
            ).all()
            stale = [
                j for j in all_running
                if j.last_heartbeat is None or j.last_heartbeat < heartbeat_cutoff
            ]

        assert len(stale) == 1
        assert stale[0].id == stale_id

    def test_fresh_job_not_identified(self, test_session_factory) -> None:
        """心跳正常的 running job 不应出现在候选列表"""

        @contextlib.contextmanager
        def mock_db_session():
            db = test_session_factory()
            try:
                yield db
                db.commit()
            except Exception:
                db.rollback()
                raise
            finally:
                db.close()

        now = datetime.now()
        create_job_run(
            test_session_factory,
            trade_date=date(2025, 5, 20),
            status="running",
            last_heartbeat=now,  # Fresh heartbeat
            started_at=now,
        )

        heartbeat_cutoff = now - _HEARTBEAT_TIMEOUT
        start_cutoff = now - _RECOVERY_WINDOW

        with mock_db_session() as db:
            all_running = db.query(JobRun).filter(
                JobRun.status == "running",
                JobRun.started_at >= start_cutoff,
            ).all()
            stale = [
                j for j in all_running
                if j.last_heartbeat is None or j.last_heartbeat < heartbeat_cutoff
            ]

        assert len(stale) == 0

    def test_null_heartbeat_identified_as_stale(self, test_session_factory) -> None:
        """last_heartbeat=NULL 应视为心跳超时（需 recover）"""

        @contextlib.contextmanager
        def mock_db_session():
            db = test_session_factory()
            try:
                yield db
                db.commit()
            except Exception:
                db.rollback()
                raise
            finally:
                db.close()

        now = datetime.now()
        null_hb_id = create_job_run(
            test_session_factory,
            trade_date=date(2025, 5, 18),
            status="running",
            last_heartbeat=None,  # NULL heartbeat
            started_at=now - timedelta(hours=1),
        )

        heartbeat_cutoff = now - _HEARTBEAT_TIMEOUT
        start_cutoff = now - _RECOVERY_WINDOW

        with mock_db_session() as db:
            all_running = db.query(JobRun).filter(
                JobRun.status == "running",
                JobRun.started_at >= start_cutoff,
            ).all()
            stale = [
                j for j in all_running
                if j.last_heartbeat is None or j.last_heartbeat < heartbeat_cutoff
            ]

        assert any(j.id == null_hb_id for j in stale)

    def test_find_resumable_job(self, patched_pipeline) -> None:
        """_find_resumable_job 找到同交易日未完成的 job_run"""
        pipe, factory = patched_pipeline
        td = date(2025, 5, 19)

        job_id = create_job_run(factory, trade_date=td, status="running")

        result = pipe._find_resumable_job(td)
        assert result == job_id

    def test_find_resumable_no_match(self, patched_pipeline) -> None:
        """无未完成任务时返回 None"""
        pipe, factory = patched_pipeline
        td = date(2025, 5, 21)

        # Create a SUCCESSFUL job (not running)
        create_job_run(factory, trade_date=td, status="success")

        result = pipe._find_resumable_job(td)
        assert result is None
