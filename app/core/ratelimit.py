"""
令牌桶限速器 — P2 升级版

变更（P2）：
- 新增命名桶注册表 (_BUCKETS)，支持多数据源各自独立限速
- 新增 `register_bucket` / `get_bucket` / `rate_limit` 装饰器
- `TokenBucketSync` 别名，标识同步同类，与异步场景语义区分
- 新增 `TUSHARE_BURST_CAPACITY` 配置支持
"""
from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import wraps
from typing import TYPE_CHECKING, TypeVar

if TYPE_CHECKING:
    pass

F = TypeVar("F", bound=Callable)


@dataclass
class TokenBucket:
    """
    线程安全令牌桶限速器（同步实现，适合同步 Pipeline 调用）

    Args:
        rate:  每分钟允许请求数（tokens/min）
        burst: 最大积累令牌数（突发容量，默认等于 rate）

    Example::

        bucket = TokenBucket(rate=180, burst=20)
        bucket.acquire()          # 阻塞直到获得令牌
        bucket.try_acquire()      # 非阻塞尝试
    """

    rate: float  # tokens per minute
    burst: float = 0.0
    _tokens: float = field(default=0.0, init=False, repr=False)
    _last: float = field(default_factory=time.monotonic, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.burst <= 0:
            self.burst = self.rate
        self._tokens = self.burst
        self._last = time.monotonic()

    def _refill(self) -> None:
        """懒惰式补充：在每次 acquire 前调用，计算自上次以来应补充的令牌"""
        now = time.monotonic()
        elapsed = now - self._last
        self._last = now
        # 每分钟 rate 个令牌 → 每秒 rate/60 个
        self._tokens = min(self.burst, self._tokens + elapsed * (self.rate / 60.0))

    def acquire(self, tokens: float = 1.0, timeout: float = 60.0) -> bool:
        """
        阻塞直到获取令牌，或超时返回 False

        Args:
            tokens:  需要的令牌数（默认 1）
            timeout: 最长等待秒数

        Returns:
            True = 成功获取，False = 超时
        """
        deadline = time.monotonic() + timeout
        while True:
            with self._lock:
                self._refill()
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return True
                # 计算还需等待多久
                wait = (tokens - self._tokens) / (self.rate / 60.0)

            if time.monotonic() + wait > deadline:
                return False
            time.sleep(min(wait, 1.0))

    def try_acquire(self, tokens: float = 1.0) -> bool:
        """非阻塞尝试获取令牌，立即返回"""
        with self._lock:
            self._refill()
            if self._tokens >= tokens:
                self._tokens -= tokens
                return True
            return False

    @property
    def available_tokens(self) -> float:
        """当前可用令牌数（近似值，仅供调试）"""
        with self._lock:
            self._refill()
            return self._tokens


# 为语义清晰提供同步别名（类比 asyncio 版本）
TokenBucketSync = TokenBucket


# ── 命名桶注册表 ──────────────────────────────────────────────────────────────
_BUCKETS: dict[str, TokenBucket] = {}
_REGISTRY_LOCK = threading.Lock()


def register_bucket(name: str, bucket: TokenBucket) -> None:
    """注册一个命名令牌桶（如已存在则覆盖）"""
    with _REGISTRY_LOCK:
        _BUCKETS[name] = bucket


def get_bucket(name: str) -> TokenBucket:
    """
    获取命名令牌桶。若不存在则按名称创建默认桶（rate=60/min）。

    Args:
        name: 桶名称（如 "tushare"、"akshare"）

    Returns:
        对应的 TokenBucket
    """
    with _REGISTRY_LOCK:
        if name not in _BUCKETS:
            # 默认：60 req/min，宽松兜底
            _BUCKETS[name] = TokenBucket(rate=60.0, burst=10.0)
        return _BUCKETS[name]


def rate_limit(
    bucket_name: str = "tushare",
    tokens: float = 1.0,
    timeout: float = 60.0,
) -> Callable[[F], F]:
    """
    限速装饰器工厂 — 在调用前从命名令牌桶获取令牌

    Args:
        bucket_name: 令牌桶名称（默认 "tushare"）
        tokens:      每次调用消耗的令牌数（默认 1）
        timeout:     等待令牌的超时秒数（默认 60s）

    Raises:
        TimeoutError: 等待超时仍未获得令牌

    Usage::

        @rate_limit(bucket_name="tushare", tokens=1)
        def fetch_daily(trade_date: str) -> pd.DataFrame:
            ...
    """

    def decorator(fn: F) -> F:
        @wraps(fn)
        def wrapper(*args, **kwargs):  # type: ignore[no-untyped-def]
            bucket = get_bucket(bucket_name)
            if not bucket.acquire(tokens=tokens, timeout=timeout):
                raise TimeoutError(
                    f"rate_limit '{bucket_name}': 等待 {timeout}s 后仍未获得令牌，"
                    f"当前 rate={bucket.rate}/min"
                )
            return fn(*args, **kwargs)

        return wrapper  # type: ignore[return-value]

    return decorator  # type: ignore[return-value]


# ── 全局 Tushare 限速器（模块级单例）────────────────────────────────────────
# 在 config 加载前先用默认值，config 加载后通过 init_tushare_limiter() 重置
_tushare_limiter: TokenBucket | None = None
_limiter_lock = threading.Lock()


def get_tushare_limiter(rate: int = 180, burst: int = 0) -> TokenBucket:
    """
    获取 Tushare 全局限速器单例

    Args:
        rate:  每分钟请求数上限
        burst: 突发容量（0 = 等于 rate）
    """
    global _tushare_limiter
    with _limiter_lock:
        if _tushare_limiter is None:
            effective_burst = float(burst if burst > 0 else min(rate, 20))
            _tushare_limiter = TokenBucket(rate=float(rate), burst=effective_burst)
            # 同步到命名桶注册表，使 @rate_limit(bucket_name="tushare") 可用
            _BUCKETS["tushare"] = _tushare_limiter
    return _tushare_limiter


def init_tushare_limiter(rate: int, burst: int = 0) -> None:
    """
    用配置值初始化/重置全局 Tushare 限速器

    Args:
        rate:  tushare_rate_limit（req/min）
        burst: tushare_burst_capacity（0 = 默认 min(rate,20)）
    """
    global _tushare_limiter
    effective_burst = float(burst if burst > 0 else min(rate, 20))
    limiter = TokenBucket(rate=float(rate), burst=effective_burst)
    with _limiter_lock:
        _tushare_limiter = limiter
        _BUCKETS["tushare"] = limiter
