"""
内存级 Circuit Breaker — 三态熔断器（CLOSED / OPEN / HALF_OPEN）

进程重启后状态清零（重置为 CLOSED），这是预期行为：
重启本身相当于给每个接口一次"重新尝试"的机会。

架构约束：
- 仅依赖 core / domain，不导入 db / api / jobs
- 线程安全：每个 BreakerState 拥有独立的 threading.Lock
- 错误分类由调用方传入 error_type 参数（枚举），不做字符串解析

错误类型分类与策略：
  RATE_LIMIT  → 计入 failure_count，达阈值后 OPEN，cooldown 后转 HALF_OPEN
  QUOTA       → 直接 OPEN 至当日 23:59:59（积分问题短期必定重现）
  TIMEOUT     → 计入 failure_count，等同 RATE_LIMIT
  NETWORK     → 计入 failure_count
  BUSINESS    → 不计入（空数据 / 无结果是正常业务情况）
"""
from __future__ import annotations

import threading
from datetime import date, datetime, time
from enum import Enum, auto
from typing import NamedTuple

from app.core.logging import get_logger

logger = get_logger("breaker")

# ──────────────────────────────────────────────────────────────────────────────
# 公共类型
# ──────────────────────────────────────────────────────────────────────────────


class ErrorType(Enum):
    """调用方传入的错误分类"""

    RATE_LIMIT = auto()  # 429 / 流量限制
    QUOTA = auto()       # 积分不足 / 权限不够（今日 OPEN）
    TIMEOUT = auto()     # 网络超时
    NETWORK = auto()     # 其他网络错误
    BUSINESS = auto()    # 业务空数据（不计入 failure）


class BreakerState(Enum):
    CLOSED = "closed"       # 正常放行
    OPEN = "open"           # 熔断中，拒绝请求
    HALF_OPEN = "half_open" # 探针放行一次


class BreakerStatus(NamedTuple):
    """熔断器当前快照（供监控/日志用）"""

    provider: str
    endpoint: str
    state: BreakerState
    failure_count: int
    open_until: datetime | None
    last_error: str


# ──────────────────────────────────────────────────────────────────────────────
# 单个 (provider, endpoint) 熔断器实例
# ──────────────────────────────────────────────────────────────────────────────


class _Breaker:
    """
    单 endpoint 熔断器内部实现

    Args:
        provider:           数据源名称（'tushare' / 'akshare'）
        endpoint:           接口名称（如 'fina_indicator_vip'）
        failure_threshold:  连续失败多少次触发 OPEN
        cooldown_seconds:   OPEN → HALF_OPEN 的冷却秒数
    """

    def __init__(
        self,
        provider: str,
        endpoint: str,
        failure_threshold: int = 5,
        cooldown_seconds: int = 300,
    ) -> None:
        self.provider = provider
        self.endpoint = endpoint
        self.failure_threshold = failure_threshold
        self.cooldown_seconds = cooldown_seconds

        self._state = BreakerState.CLOSED
        self._failure_count = 0
        self._open_until: datetime | None = None
        self._last_error: str = ""
        self._lock = threading.Lock()

    # ── 对外接口 ──────────────────────────────────────────────────────────

    def allow(self) -> bool:
        """
        判断当前是否允许发起请求。

        Returns:
            True = 可以发起请求（CLOSED 或 HALF_OPEN）
            False = 熔断中，应走 fallback（OPEN）
        """
        with self._lock:
            if self._state == BreakerState.CLOSED:
                return True

            if self._state == BreakerState.OPEN:
                # 检查冷却是否到期
                if self._open_until is not None and datetime.now() >= self._open_until:
                    self._state = BreakerState.HALF_OPEN
                    logger.info(
                        "breaker.half_open",
                        provider=self.provider,
                        endpoint=self.endpoint,
                    )
                    return True  # HALF_OPEN 放行此次探针
                return False

            if self._state == BreakerState.HALF_OPEN:
                # HALF_OPEN 只允许 1 次探针，后续如未回调则视为仍在探测中
                return True

            return True  # 默认放行（防御性兜底）

    def record_success(self) -> None:
        """记录一次成功调用（HALF_OPEN → CLOSED）"""
        with self._lock:
            if self._state == BreakerState.HALF_OPEN:
                self._state = BreakerState.CLOSED
                self._failure_count = 0
                self._open_until = None
                logger.info(
                    "breaker.closed",
                    provider=self.provider,
                    endpoint=self.endpoint,
                    reason="probe_success",
                )
            elif self._state == BreakerState.CLOSED:
                # 连续成功时逐渐衰减 failure_count
                if self._failure_count > 0:
                    self._failure_count = max(0, self._failure_count - 1)

    def record_failure(self, error_type: ErrorType, error_msg: str = "") -> None:
        """
        记录一次失败调用

        Args:
            error_type: 错误类型（决定熔断策略）
            error_msg:  原始错误信息（截断后用于日志/状态快照）
        """
        with self._lock:
            self._last_error = error_msg[:200] if error_msg else ""

            # BUSINESS 错误不计入 failure
            if error_type == ErrorType.BUSINESS:
                return

            # QUOTA 错误 → 直接 OPEN 至当日 23:59:59
            if error_type == ErrorType.QUOTA:
                self._open_quota_until_eod()
                return

            # 其他错误（RATE_LIMIT / TIMEOUT / NETWORK）→ 累计计数
            if self._state == BreakerState.HALF_OPEN:
                # 探针失败 → 重新 OPEN，延长冷却
                self._failure_count += 1
                self._open_with_cooldown()
                logger.warning(
                    "breaker.probe_failed",
                    provider=self.provider,
                    endpoint=self.endpoint,
                    error_type=error_type.name,
                )
                return

            if self._state == BreakerState.CLOSED:
                self._failure_count += 1
                if self._failure_count >= self.failure_threshold:
                    self._open_with_cooldown()

    def status(self) -> BreakerStatus:
        """返回当前状态快照"""
        with self._lock:
            return BreakerStatus(
                provider=self.provider,
                endpoint=self.endpoint,
                state=self._state,
                failure_count=self._failure_count,
                open_until=self._open_until,
                last_error=self._last_error,
            )

    def reset(self) -> None:
        """手动重置到 CLOSED 状态（测试或运维用）"""
        with self._lock:
            self._state = BreakerState.CLOSED
            self._failure_count = 0
            self._open_until = None
            self._last_error = ""
        logger.info(
            "breaker.reset",
            provider=self.provider,
            endpoint=self.endpoint,
        )

    # ── 内部辅助 ──────────────────────────────────────────────────────────

    def _open_with_cooldown(self) -> None:
        """进入 OPEN 状态，cooldown_seconds 后允许探针"""
        from datetime import timedelta  # noqa: PLC0415

        self._state = BreakerState.OPEN
        self._open_until = datetime.now() + timedelta(seconds=self.cooldown_seconds)
        logger.warning(
            "breaker.open",
            provider=self.provider,
            endpoint=self.endpoint,
            failure_count=self._failure_count,
            open_until=self._open_until.isoformat(),
            cooldown_seconds=self.cooldown_seconds,
        )

    def _open_quota_until_eod(self) -> None:
        """进入 OPEN 状态直到今天 23:59:59（积分/权限问题）"""
        today = date.today()
        eod = datetime.combine(today, time(23, 59, 59))
        self._state = BreakerState.OPEN
        self._open_until = eod
        logger.warning(
            "breaker.open_quota",
            provider=self.provider,
            endpoint=self.endpoint,
            open_until=eod.isoformat(),
            reason="quota_or_permission_insufficient",
        )


# ──────────────────────────────────────────────────────────────────────────────
# 全局熔断器注册表（模块级单例）
# ──────────────────────────────────────────────────────────────────────────────

_registry: dict[tuple[str, str], _Breaker] = {}
_registry_lock = threading.Lock()


def get_breaker(
    provider: str,
    endpoint: str,
    failure_threshold: int = 5,
    cooldown_seconds: int = 300,
) -> _Breaker:
    """
    获取或创建指定 (provider, endpoint) 的熔断器实例

    Args:
        provider:          数据源名称
        endpoint:          接口名称
        failure_threshold: 连续失败触发阈值（首次创建时生效）
        cooldown_seconds:  冷却秒数（首次创建时生效）

    Returns:
        对应的熔断器实例（已存在则直接返回，参数不覆盖）
    """
    key = (provider, endpoint)
    with _registry_lock:
        if key not in _registry:
            _registry[key] = _Breaker(
                provider=provider,
                endpoint=endpoint,
                failure_threshold=failure_threshold,
                cooldown_seconds=cooldown_seconds,
            )
        return _registry[key]


def reset_all_breakers() -> None:
    """重置所有熔断器（测试 / 运维恢复用）"""
    with _registry_lock:
        for breaker in _registry.values():
            breaker.reset()
    logger.info("breaker.reset_all")


def get_all_statuses() -> list[BreakerStatus]:
    """获取所有熔断器的状态快照（用于监控 API）"""
    with _registry_lock:
        keys = list(_registry.keys())
    return [_registry[k].status() for k in keys]


# ──────────────────────────────────────────────────────────────────────────────
# 便捷装饰器（同步版，供 TushareProvider 内部使用）
# ──────────────────────────────────────────────────────────────────────────────

def classify_tushare_error(error_msg: str) -> ErrorType:
    """
    将 Tushare 原始错误信息映射到 ErrorType

    Args:
        error_msg: str(exception) 的内容

    Returns:
        ErrorType 枚举值
    """
    lower = error_msg.lower()

    # 积分/权限错误（需要升级账号）
    quota_keywords = (
        "积分不够", "积分不足", "权限", "permission", "insufficient",
        "您的积分", "需要", "2000", "5000", "vip", "需要升级",
        "您没有访问", "没有权限", "无权限",
    )
    for kw in quota_keywords:
        if kw in lower:
            return ErrorType.QUOTA

    # 限流
    rate_keywords = (
        "每分钟最多", "访问频率", "rate limit", "too many", "429",
        "抱歉，您每分钟", "请求过频",
    )
    for kw in rate_keywords:
        if kw in lower:
            return ErrorType.RATE_LIMIT

    # 超时
    timeout_keywords = ("timeout", "timed out", "超时", "connection")
    for kw in timeout_keywords:
        if kw in lower:
            return ErrorType.TIMEOUT

    return ErrorType.NETWORK
