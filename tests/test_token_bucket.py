"""
令牌桶限速器单元测试 — P2

覆盖：
- TokenBucket 速率、突发容量、超时、并发
- TokenBucketSync 别名
- 命名桶注册表（register_bucket / get_bucket）
- @rate_limit 装饰器
"""
from __future__ import annotations

import threading
import time

import pytest

from app.core.ratelimit import (
    TokenBucket,
    TokenBucketSync,
    get_bucket,
    rate_limit,
    register_bucket,
)

# ─────────────────────────────────────────────────────────────────────────────
# 基础行为
# ─────────────────────────────────────────────────────────────────────────────


class TestTokenBucketBasics:
    def test_initial_tokens_equal_burst(self) -> None:
        bucket = TokenBucket(rate=60.0, burst=5.0)
        assert bucket.available_tokens == pytest.approx(5.0, abs=0.05)

    def test_burst_defaults_to_rate(self) -> None:
        bucket = TokenBucket(rate=60.0)  # burst 未指定 → 等于 rate
        assert bucket.burst == 60.0

    def test_try_acquire_drains_tokens(self) -> None:
        bucket = TokenBucket(rate=60.0, burst=3.0)
        assert bucket.try_acquire(1.0) is True
        assert bucket.try_acquire(1.0) is True
        assert bucket.try_acquire(1.0) is True
        assert bucket.try_acquire(1.0) is False  # 桶已空

    def test_try_acquire_multi_tokens(self) -> None:
        bucket = TokenBucket(rate=60.0, burst=5.0)
        assert bucket.try_acquire(5.0) is True   # 全取
        assert bucket.try_acquire(1.0) is False  # 空桶

    def test_refill_over_time(self) -> None:
        """速率 600/min = 10/s，等待 0.2s 应补充 ~2 个令牌"""
        bucket = TokenBucket(rate=600.0, burst=5.0)
        # 先取空
        bucket.try_acquire(5.0)
        assert bucket.available_tokens == pytest.approx(0.0, abs=0.1)

        time.sleep(0.25)  # 等待 ~2.5 个令牌补充
        assert bucket.available_tokens >= 2.0

    def test_tokens_capped_at_burst(self) -> None:
        bucket = TokenBucket(rate=600.0, burst=3.0)
        # 等待远超 burst 的时间，令牌不应超过 burst
        time.sleep(0.2)
        assert bucket.available_tokens <= 3.0 + 0.1  # 允许极小误差


# ─────────────────────────────────────────────────────────────────────────────
# acquire 超时行为
# ─────────────────────────────────────────────────────────────────────────────


class TestAcquireTimeout:
    def test_acquire_returns_true_when_tokens_available(self) -> None:
        bucket = TokenBucket(rate=60.0, burst=5.0)
        result = bucket.acquire(tokens=1.0, timeout=1.0)
        assert result is True

    def test_acquire_blocks_and_returns_true(self) -> None:
        """空桶 + 高速率：acquire 应短暂等待后成功"""
        bucket = TokenBucket(rate=600.0, burst=1.0)  # 10 tokens/s
        bucket._tokens = 0.0  # 手动清空

        start = time.monotonic()
        result = bucket.acquire(tokens=1.0, timeout=2.0)
        elapsed = time.monotonic() - start

        assert result is True
        # 速率 10/s → 等待约 0.1s，允许 [0.05, 0.5] 范围
        assert 0.04 <= elapsed <= 0.5

    def test_acquire_timeout_returns_false(self) -> None:
        """空桶 + 极慢速率：应在 timeout 内返回 False"""
        bucket = TokenBucket(rate=1.0, burst=1.0)  # 1 token/min
        bucket._tokens = 0.0

        result = bucket.acquire(tokens=1.0, timeout=0.05)
        assert result is False

    def test_acquire_with_zero_timeout(self) -> None:
        """超短 timeout 空桶应立即返回 False"""
        bucket = TokenBucket(rate=60.0, burst=2.0)
        bucket._tokens = 0.0
        result = bucket.acquire(tokens=1.0, timeout=0.001)
        assert result is False


# ─────────────────────────────────────────────────────────────────────────────
# 并发安全
# ─────────────────────────────────────────────────────────────────────────────


class TestConcurrentAcquire:
    def test_concurrent_try_acquire(self) -> None:
        """并发 20 个线程竞争同一桶（burst=20），每个取 1 个令牌"""
        bucket = TokenBucket(rate=6000.0, burst=20.0)
        results: list[bool] = []
        lock = threading.Lock()

        def acquire_once() -> None:
            r = bucket.try_acquire(1.0)
            with lock:
                results.append(r)

        threads = [threading.Thread(target=acquire_once) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # 恰好 20 个成功（burst=20），无超发
        assert sum(results) == 20

    def test_concurrent_no_over_grant(self) -> None:
        """高并发下令牌不能超发"""
        burst = 10
        bucket = TokenBucket(rate=600.0, burst=float(burst))
        success_count = 0
        lock = threading.Lock()

        def try_many() -> None:
            nonlocal success_count
            for _ in range(5):
                if bucket.try_acquire(1.0):
                    with lock:
                        success_count += 1

        threads = [threading.Thread(target=try_many) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # 成功数量 ≤ burst（不超发）
        assert success_count <= burst + 1  # +1 允许极小的时间补充误差


# ─────────────────────────────────────────────────────────────────────────────
# TokenBucketSync 别名
# ─────────────────────────────────────────────────────────────────────────────


def test_token_bucket_sync_is_alias() -> None:
    """TokenBucketSync 应是 TokenBucket 的别名"""
    assert TokenBucketSync is TokenBucket


def test_token_bucket_sync_behavior() -> None:
    bucket = TokenBucketSync(rate=60.0, burst=3.0)
    assert bucket.try_acquire(3.0) is True
    assert bucket.try_acquire(1.0) is False


# ─────────────────────────────────────────────────────────────────────────────
# 命名桶注册表
# ─────────────────────────────────────────────────────────────────────────────


class TestBucketRegistry:
    def test_register_and_get_bucket(self) -> None:
        bucket = TokenBucket(rate=120.0, burst=10.0)
        register_bucket("test_reg_bucket", bucket)
        retrieved = get_bucket("test_reg_bucket")
        assert retrieved is bucket

    def test_get_bucket_creates_default_if_missing(self) -> None:
        name = "test_auto_bucket_xyz"
        bucket = get_bucket(name)  # Should auto-create
        assert isinstance(bucket, TokenBucket)
        assert bucket.rate > 0

    def test_register_overwrites_existing(self) -> None:
        old_bucket = TokenBucket(rate=10.0, burst=1.0)
        new_bucket = TokenBucket(rate=999.0, burst=100.0)
        register_bucket("test_overwrite_bucket", old_bucket)
        register_bucket("test_overwrite_bucket", new_bucket)
        assert get_bucket("test_overwrite_bucket") is new_bucket


# ─────────────────────────────────────────────────────────────────────────────
# @rate_limit 装饰器
# ─────────────────────────────────────────────────────────────────────────────


class TestRateLimitDecorator:
    def test_decorated_function_returns_value(self) -> None:
        register_bucket("test_deco_full", TokenBucket(rate=6000.0, burst=100.0))

        @rate_limit(bucket_name="test_deco_full", tokens=1)
        def my_func(x: int) -> int:
            return x * 2

        assert my_func(5) == 10

    def test_decorator_consumes_tokens(self) -> None:
        bucket = TokenBucket(rate=600.0, burst=2.0)
        register_bucket("test_deco_consume", bucket)

        @rate_limit(bucket_name="test_deco_consume", tokens=1)
        def my_func() -> None:
            pass

        my_func()  # consumes 1
        my_func()  # consumes 1 → bucket now empty (burst=2)

        assert bucket.available_tokens < 1.0

    def test_decorator_timeout_raises(self) -> None:
        bucket = TokenBucket(rate=1.0, burst=1.0)
        bucket._tokens = 0.0  # drain
        register_bucket("test_deco_timeout", bucket)

        @rate_limit(bucket_name="test_deco_timeout", tokens=1, timeout=0.05)
        def my_func() -> None:
            pass

        with pytest.raises(TimeoutError, match="rate_limit"):
            my_func()

    def test_decorator_preserves_function_metadata(self) -> None:
        register_bucket("test_deco_meta", TokenBucket(rate=60.0, burst=5.0))

        @rate_limit(bucket_name="test_deco_meta")
        def documented_func() -> int:
            """My docstring"""
            return 42

        assert documented_func.__name__ == "documented_func"
        assert documented_func.__doc__ == "My docstring"

    def test_default_bucket_name_is_tushare(self) -> None:
        """@rate_limit 默认 bucket_name='tushare'，应从 tushare 桶扣令牌"""
        # Ensure tushare bucket is initialized with plenty of tokens
        register_bucket("tushare", TokenBucket(rate=180.0, burst=50.0))

        @rate_limit(tokens=1)  # 默认 bucket_name="tushare"
        def api_call() -> str:
            return "ok"

        assert api_call() == "ok"
