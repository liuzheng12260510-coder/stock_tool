"""
令牌桶限速器 — 替换原代码中的 np.random < 0.05 玄学限流
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field


@dataclass
class TokenBucket:
    """
    线程安全令牌桶限速器

    Args:
        rate: 每分钟允许请求数
        burst: 最大积累令牌数（默认等于 rate）
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
        now = time.monotonic()
        elapsed = now - self._last
        self._last = now
        # 每分钟 rate 个令牌 → 每秒 rate/60 个
        self._tokens = min(self.burst, self._tokens + elapsed * (self.rate / 60.0))

    def acquire(self, tokens: float = 1.0, timeout: float = 60.0) -> bool:
        """
        阻塞直到获取令牌，或超时返回 False

        Args:
            tokens: 需要的令牌数（默认 1）
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
                wait = (tokens - self._tokens) / (self.rate / 60.0)

            if time.monotonic() + wait > deadline:
                return False
            time.sleep(min(wait, 1.0))

    def try_acquire(self, tokens: float = 1.0) -> bool:
        """非阻塞尝试获取令牌"""
        with self._lock:
            self._refill()
            if self._tokens >= tokens:
                self._tokens -= tokens
                return True
            return False


# ── 全局 Tushare 限速器（模块级单例）─────────────────────────────────────
# 在 config 加载前先用默认值，config 加载后通过 init_tushare_limiter() 重置
_tushare_limiter: TokenBucket | None = None
_limiter_lock = threading.Lock()


def get_tushare_limiter(rate: int = 180) -> TokenBucket:
    """获取 Tushare 全局限速器单例"""
    global _tushare_limiter
    with _limiter_lock:
        if _tushare_limiter is None:
            _tushare_limiter = TokenBucket(rate=rate, burst=min(rate, 20))
    return _tushare_limiter


def init_tushare_limiter(rate: int) -> None:
    """用配置值初始化/重置全局限速器"""
    global _tushare_limiter
    with _limiter_lock:
        _tushare_limiter = TokenBucket(rate=rate, burst=min(rate, 20))
