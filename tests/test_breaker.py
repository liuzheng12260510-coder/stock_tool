"""
熔断器 Circuit Breaker 单元测试
"""
from __future__ import annotations

import time
from datetime import datetime

from app.data.breaker import (
    BreakerState,
    ErrorType,
    _Breaker,
    classify_tushare_error,
    get_breaker,
    reset_all_breakers,
)

# ─────────────────────────────────────────────────────────────────────────────
# 辅助工厂
# ─────────────────────────────────────────────────────────────────────────────

def make_breaker(threshold: int = 3, cooldown: int = 1) -> _Breaker:
    """创建独立的熔断器实例（不入全局注册表）"""
    return _Breaker(
        provider="test_provider",
        endpoint="test_endpoint",
        failure_threshold=threshold,
        cooldown_seconds=cooldown,
    )


# ─────────────────────────────────────────────────────────────────────────────
# 基本状态转换
# ─────────────────────────────────────────────────────────────────────────────

class TestStateTransitions:
    """CLOSED → OPEN → HALF_OPEN → CLOSED 完整状态机"""

    def test_initial_state_is_closed(self):
        b = make_breaker()
        assert b.status().state == BreakerState.CLOSED
        assert b.allow() is True

    def test_closed_to_open_on_threshold(self):
        b = make_breaker(threshold=3)
        for _ in range(3):
            b.record_failure(ErrorType.NETWORK, "error")
        assert b.status().state == BreakerState.OPEN
        assert b.allow() is False

    def test_not_open_before_threshold(self):
        b = make_breaker(threshold=3)
        b.record_failure(ErrorType.NETWORK, "e1")
        b.record_failure(ErrorType.NETWORK, "e2")
        # 仅 2 次，阈值为 3
        assert b.status().state == BreakerState.CLOSED
        assert b.allow() is True

    def test_open_to_half_open_after_cooldown(self):
        b = make_breaker(threshold=1, cooldown=1)  # 1 秒冷却
        b.record_failure(ErrorType.NETWORK, "err")
        assert b.status().state == BreakerState.OPEN

        time.sleep(1.1)  # 等待冷却
        # allow() 触发状态检查
        result = b.allow()
        assert result is True
        assert b.status().state == BreakerState.HALF_OPEN

    def test_half_open_to_closed_on_success(self):
        b = make_breaker(threshold=1, cooldown=1)
        b.record_failure(ErrorType.NETWORK, "err")
        time.sleep(1.1)
        b.allow()  # 触发 HALF_OPEN
        b.record_success()
        assert b.status().state == BreakerState.CLOSED
        assert b.status().failure_count == 0

    def test_half_open_to_open_on_failure(self):
        b = make_breaker(threshold=1, cooldown=1)
        b.record_failure(ErrorType.NETWORK, "err")
        time.sleep(1.1)
        b.allow()  # 触发 HALF_OPEN
        b.record_failure(ErrorType.NETWORK, "probe failed")
        assert b.status().state == BreakerState.OPEN

    def test_success_decrements_failure_count(self):
        b = make_breaker(threshold=5)
        b.record_failure(ErrorType.NETWORK, "e1")
        b.record_failure(ErrorType.NETWORK, "e2")
        assert b.status().failure_count == 2
        b.record_success()
        assert b.status().failure_count == 1

    def test_reset_restores_closed(self):
        b = make_breaker(threshold=1)
        b.record_failure(ErrorType.NETWORK, "e")
        assert b.status().state == BreakerState.OPEN
        b.reset()
        assert b.status().state == BreakerState.CLOSED
        assert b.status().failure_count == 0


# ─────────────────────────────────────────────────────────────────────────────
# QUOTA 错误特殊策略（直接 OPEN 至 EOD）
# ─────────────────────────────────────────────────────────────────────────────

class TestQuotaError:
    """积分/权限不足 → 直接熔断到当日结束，无需达到阈值"""

    def test_single_quota_error_triggers_open(self):
        b = make_breaker(threshold=10)  # 阈值很高
        b.record_failure(ErrorType.QUOTA, "积分不够")
        assert b.status().state == BreakerState.OPEN
        assert b.allow() is False

    def test_quota_open_until_eod(self):
        b = make_breaker(threshold=10)
        b.record_failure(ErrorType.QUOTA, "权限不足")
        status = b.status()
        assert status.open_until is not None
        # open_until 应该是今天 23:59:59
        today = datetime.now().date()
        assert status.open_until.date() == today
        assert status.open_until.hour == 23
        assert status.open_until.minute == 59

    def test_quota_error_does_not_increment_after_threshold(self):
        """QUOTA 直接 OPEN，failure_count 不应参与阈值逻辑"""
        b = make_breaker(threshold=3)
        b.record_failure(ErrorType.QUOTA, "quota")
        # 已经 OPEN，再次 record_failure 不应改变 open_until
        _open_until_1 = b.status().open_until
        b.record_failure(ErrorType.QUOTA, "quota again")
        # 状态仍为 OPEN
        assert b.status().state == BreakerState.OPEN


# ─────────────────────────────────────────────────────────────────────────────
# BUSINESS 错误（不计入 failure）
# ─────────────────────────────────────────────────────────────────────────────

class TestBusinessError:
    """BUSINESS 错误（空数据/无结果）不应触发熔断"""

    def test_business_errors_do_not_count(self):
        b = make_breaker(threshold=2)
        # 远超阈值的 BUSINESS 错误，不应触发 OPEN
        for _ in range(10):
            b.record_failure(ErrorType.BUSINESS, "empty data")
        assert b.status().state == BreakerState.CLOSED
        assert b.status().failure_count == 0


# ─────────────────────────────────────────────────────────────────────────────
# 全局注册表
# ─────────────────────────────────────────────────────────────────────────────

class TestRegistry:
    """get_breaker() 全局注册表行为"""

    def setup_method(self):
        reset_all_breakers()

    def test_same_key_returns_same_instance(self):
        b1 = get_breaker("tushare", "daily_basic")
        b2 = get_breaker("tushare", "daily_basic")
        assert b1 is b2

    def test_different_endpoints_are_isolated(self):
        b1 = get_breaker("tushare", "daily")
        b2 = get_breaker("tushare", "fina_indicator_vip")
        assert b1 is not b2

        # 打开 daily 不影响 fina_indicator_vip
        for _ in range(5):
            b1.record_failure(ErrorType.NETWORK, "err")
        assert b1.status().state == BreakerState.OPEN
        assert b2.status().state == BreakerState.CLOSED

    def test_reset_all_resets_all_instances(self):
        b1 = get_breaker("tushare", "ep_a", failure_threshold=1)
        b2 = get_breaker("tushare", "ep_b", failure_threshold=1)
        b1.record_failure(ErrorType.NETWORK, "e")
        b2.record_failure(ErrorType.NETWORK, "e")
        assert b1.status().state == BreakerState.OPEN
        assert b2.status().state == BreakerState.OPEN

        reset_all_breakers()
        assert b1.status().state == BreakerState.CLOSED
        assert b2.status().state == BreakerState.CLOSED


# ─────────────────────────────────────────────────────────────────────────────
# classify_tushare_error
# ─────────────────────────────────────────────────────────────────────────────

class TestClassifyTushareError:
    """Tushare 原始错误消息 → ErrorType 映射"""

    def test_rate_limit_chinese(self):
        assert classify_tushare_error("抱歉，您每分钟最多访问该接口100次") == ErrorType.RATE_LIMIT

    def test_rate_limit_too_many(self):
        assert classify_tushare_error("Too many requests, rate limit exceeded") == ErrorType.RATE_LIMIT

    def test_quota_jifenbuzu(self):
        assert classify_tushare_error("您的积分不足，无法访问此接口") == ErrorType.QUOTA

    def test_quota_permission(self):
        assert classify_tushare_error("您没有访问该接口的权限") == ErrorType.QUOTA

    def test_quota_insufficient(self):
        assert classify_tushare_error("积分不够，需要2000积分") == ErrorType.QUOTA

    def test_timeout(self):
        assert classify_tushare_error("Connection timeout while fetching data") == ErrorType.TIMEOUT

    def test_network_fallback(self):
        assert classify_tushare_error("Some unknown error occurred") == ErrorType.NETWORK

    def test_empty_string(self):
        assert classify_tushare_error("") == ErrorType.NETWORK
