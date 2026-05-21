"""
领域枚举 — 无框架依赖
"""
from __future__ import annotations

from enum import StrEnum


class ProviderType(StrEnum):
    TUSHARE = "tushare"
    AKSHARE = "akshare"
    COMPOSITE = "composite"


class AIStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    CACHED = "cached"


class JobStatus(StrEnum):
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    PARTIAL = "partial"  # 部分成功


class Exchange(StrEnum):
    SSE = "SSE"    # 上交所
    SZSE = "SZSE"  # 深交所
    BSE = "BSE"    # 北交所


class EntType(StrEnum):
    PRIVATE = "私营企业"
    SOE = "国有企业"
    CENTRAL = "央企"
    MIXED = "混合所有制"
    FOREIGN = "外资"
    UNKNOWN = "未知"


class ReportPeriod(StrEnum):
    Q1 = "03-31"   # 一季报
    SEMI = "06-30" # 半年报
    Q3 = "09-30"   # 三季报
    ANNUAL = "12-31" # 年报
