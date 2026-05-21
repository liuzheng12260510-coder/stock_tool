"""
交易日历 — 带本地 DB 缓存，避免重复调网络
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from sqlalchemy import text

from app.core.logging import get_logger
from app.db.session import db_session

logger = get_logger("trade_calendar")

# 模块级内存缓存（本次进程生命期内有效）
_calendar_cache: dict[str, list[date]] = {}


def _cache_key(start: str, end: str) -> str:
    return f"{start}_{end}"


def get_trade_dates(start_date: str, end_date: str) -> list[date]:
    """
    获取 [start_date, end_date] 区间内的交易日列表
    优先级：内存缓存 → 数据库 → 网络（Tushare）

    Args:
        start_date: YYYYMMDD 格式
        end_date:   YYYYMMDD 格式
    """
    key = _cache_key(start_date, end_date)
    if key in _calendar_cache:
        return _calendar_cache[key]

    # 尝试从 DB 查
    start_d = datetime.strptime(start_date, "%Y%m%d").date()
    end_d = datetime.strptime(end_date, "%Y%m%d").date()

    try:
        with db_session() as db:
            rows = db.execute(
                text(
                    "SELECT trade_date FROM trade_calendar "
                    "WHERE trade_date >= :s AND trade_date <= :e "
                    "ORDER BY trade_date"
                ),
                {"s": start_d, "e": end_d},
            ).fetchall()
            if rows:
                dates = [r[0] if isinstance(r[0], date) else date.fromisoformat(str(r[0])) for r in rows]
                _calendar_cache[key] = dates
                return dates
    except Exception:
        pass  # 表不存在时正常跳过，走网络

    # 从网络拉取
    from app.data.tushare_provider import TushareProvider  # 延迟导入避免循环

    provider = TushareProvider()
    dates = provider.get_trade_calendar(start_date, end_date)
    if dates:
        _store_to_db(dates)
        _calendar_cache[key] = dates
    return dates


def _store_to_db(dates: list[date]) -> None:
    """将交易日历写入 DB（确保 trade_calendar 表存在）"""
    try:
        with db_session() as db:
            db.execute(
                text(
                    "CREATE TABLE IF NOT EXISTS trade_calendar "
                    "(trade_date DATE PRIMARY KEY)"
                )
            )
            for d in dates:
                db.execute(
                    text(
                        "INSERT OR IGNORE INTO trade_calendar (trade_date) VALUES (:d)"
                    ),
                    {"d": d},
                )
    except Exception as e:
        logger.warning("写入 trade_calendar 失败", error=str(e))


def get_latest_trade_date(reference: date | None = None) -> date:
    """
    获取最近的交易日（不晚于 reference，默认今天）
    """
    ref = reference or date.today()
    start = (ref - timedelta(days=14)).strftime("%Y%m%d")
    end = ref.strftime("%Y%m%d")
    dates = get_trade_dates(start, end)
    if not dates:
        # 兜底：返回上一个工作日
        d = ref
        for _ in range(7):
            d -= timedelta(days=1)
            if d.weekday() < 5:
                return d
        return ref - timedelta(days=1)
    return max(d for d in dates if d <= ref)


def is_trade_date(d: date) -> bool:
    """判断某天是否为交易日"""
    ds = d.strftime("%Y%m%d")
    dates = get_trade_dates(ds, ds)
    return d in dates


def get_prev_trade_date(d: date, offset: int = 1) -> date:
    """获取 d 之前第 offset 个交易日"""
    start = (d - timedelta(days=offset * 3 + 30)).strftime("%Y%m%d")
    end = (d - timedelta(days=1)).strftime("%Y%m%d")
    dates = get_trade_dates(start, end)
    if len(dates) < offset:
        return d - timedelta(days=offset)
    return sorted(dates)[-offset]
