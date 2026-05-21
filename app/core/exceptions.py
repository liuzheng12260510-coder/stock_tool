"""
自定义异常类
"""
from __future__ import annotations


class StockSentryError(Exception):
    """基类"""


class DataProviderError(StockSentryError):
    """数据源拉取失败"""


class RateLimitError(DataProviderError):
    """触发限速（429）"""


class NoDataError(DataProviderError):
    """查询无结果"""


class TTMCalculationError(StockSentryError):
    """TTM 计算无法完成（季度缺口、数据不足等）"""


class AnalyticsError(StockSentryError):
    """分析层通用异常"""


class AIProviderError(StockSentryError):
    """AI 调用失败"""


class AIUnavailableError(AIProviderError):
    """AI 功能未配置或不可用"""


class ExportError(StockSentryError):
    """导出失败"""


class SchedulerError(StockSentryError):
    """调度器异常"""


class ConfigurationError(StockSentryError):
    """配置错误（如必填项缺失）"""


class TushareQuotaError(DataProviderError):
    """Tushare 账号积分不足或接口权限不够（需升级积分等级）"""


class BreakerOpenError(DataProviderError):
    """熔断器处于 OPEN 状态，拒绝请求以保护配额"""
