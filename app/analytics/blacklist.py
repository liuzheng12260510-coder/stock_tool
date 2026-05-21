"""
全局黑名单识别 — 纯函数，无外部 IO

识别维度：
1. ST / *ST / 退市风险 / PT：股票名称含特殊前缀
2. 北交所：ts_code 后缀 .BJ
3. 次新股：list_date 距今不足 min_list_years 年

设计原则：
- 全部使用纯函数 + 正则，不依赖数据库或网络
- 返回值为黑名单原因字符串（None 表示不在黑名单）
- 过期机制（expires_at）由调用方计算并写入 blacklist 表
"""
from __future__ import annotations

import re
from datetime import date, timedelta

from app.core.logging import get_logger

logger = get_logger("blacklist")

# 股票名称黑名单正则（前缀/全词匹配，大小写不敏感）
# Tushare 中 ST 股名称格式为 "ST 某某" 或 "*ST某某"
_BLACKLIST_NAME_PATTERN = re.compile(
    r"^(\*ST|ST\s|ST(?=[^\s])|退市|PT\s|PT(?=[^\s])|\*)",
    re.IGNORECASE,
)

# 更宽松的包含匹配（有些名称如"ST银行"无空格）
_ST_CONTAINS_PATTERN = re.compile(r"[*＊]?ST", re.IGNORECASE)

# 北交所后缀
_BSE_SUFFIX = ".BJ"

# 退市相关关键词
_DELIST_KEYWORDS = ("退市", "退", "PT")


def detect_blacklist_reason(
    name: str,
    ts_code: str,
    list_date: date | None,
    today: date,
    min_list_years: float = 1.0,
    exclude_bj: bool = True,
) -> tuple[str | None, date | None]:
    """
    检测股票是否应当加入黑名单

    Args:
        name:            股票名称（Tushare stock_basic.name）
        ts_code:         股票代码，如 "600519.SH"
        list_date:       上市日期（None 时跳过次新股判断）
        today:           当前日期（交易日）
        min_list_years:  次新股门槛（年），默认 1 年
        exclude_bj:      是否排除北交所

    Returns:
        (reason, expires_at)
        - reason:     黑名单原因字符串，None 表示不在黑名单
        - expires_at: 该黑名单记录的过期时间（None 表示永久）
                      次新股满足年限后自动过期
    """
    name_stripped = (name or "").strip()
    code_upper = (ts_code or "").upper()

    # ── 1. ST / *ST / PT / 退市风险 ─────────────────────────────────────
    if name_stripped:
        # 名称以 *ST、ST、退市、PT 开头
        if _BLACKLIST_NAME_PATTERN.match(name_stripped):
            return _classify_st_reason(name_stripped), None
        # 名称中含有 ST（防止某些无空格写法漏检）
        if _ST_CONTAINS_PATTERN.search(name_stripped):
            return "ST", None
        # 名称中含"退市"
        if "退市" in name_stripped:
            return "delist_risk", None

    # ── 2. 北交所 ────────────────────────────────────────────────────────
    if exclude_bj and code_upper.endswith(_BSE_SUFFIX):
        return "bse", None

    # ── 3. 次新股（上市不足 min_list_years 年）──────────────────────────
    if list_date is not None:
        min_days = int(min_list_years * 365)
        days_listed = (today - list_date).days
        if days_listed < min_days:
            # 过期时间 = list_date + min_days（满足年限后自动出列）
            expires_at = list_date + timedelta(days=min_days)
            return "new_listing", expires_at

    return None, None


def _classify_st_reason(name: str) -> str:
    """细分 ST 类别以便日志可读"""
    upper = name.upper()
    if upper.startswith("*ST") or upper.startswith("＊ST"):
        return "*ST"
    if "退市" in name:
        return "delist_risk"
    if upper.startswith("PT"):
        return "PT"
    return "ST"


def build_blacklist_from_df(
    stock_df,  # pd.DataFrame with columns: ts_code, name, list_date
    today: date,
    min_list_years: float = 1.0,
    exclude_bj: bool = True,
) -> list[dict]:
    """
    批量从 stock_df 生成黑名单记录列表

    Args:
        stock_df:         包含 ts_code / name / list_date 列的 DataFrame
        today:            当前交易日
        min_list_years:   次新股门槛（年）
        exclude_bj:       是否排除北交所

    Returns:
        黑名单记录 list[dict]，每个 dict 含 ts_code / reason / expires_at
    """
    import pandas as pd  # noqa: PLC0415

    records: list[dict] = []

    for _, row in stock_df.iterrows():
        ts_code = str(row.get("ts_code", ""))
        name = str(row.get("name", "") or "")

        # 解析上市日期
        list_date: date | None = None
        raw_ld = row.get("list_date")
        if raw_ld is not None and not pd.isna(raw_ld):
            if isinstance(raw_ld, date):
                list_date = raw_ld
            else:
                try:
                    from datetime import datetime  # noqa: PLC0415
                    list_date = datetime.strptime(str(raw_ld), "%Y%m%d").date()
                except (ValueError, TypeError):
                    pass

        reason, expires_at = detect_blacklist_reason(
            name=name,
            ts_code=ts_code,
            list_date=list_date,
            today=today,
            min_list_years=min_list_years,
            exclude_bj=exclude_bj,
        )

        if reason is not None:
            records.append(
                {
                    "ts_code": ts_code,
                    "reason": reason,
                    "expires_at": expires_at,
                }
            )
            logger.debug(
                "黑名单命中",
                ts_code=ts_code,
                name=name,
                reason=reason,
                expires_at=str(expires_at) if expires_at else "永久",
            )

    return records


def filter_blacklisted_codes(
    ts_codes: set[str],
    today: date,
) -> set[str]:
    """
    从数据库中加载未过期的黑名单，返回应当排除的 ts_code 集合。

    Args:
        ts_codes: 待过滤的 ts_code 集合
        today:    当前日期

    Returns:
        应当排除的 ts_code 子集
    """
    from sqlalchemy import select  # noqa: PLC0415

    from app.db.models import Blacklist  # noqa: PLC0415
    from app.db.session import db_session  # noqa: PLC0415

    if not ts_codes:
        return set()

    excluded: set[str] = set()
    with db_session() as db:
        # 加载该 ts_codes 中在黑名单里且未到期的记录
        stmt = select(Blacklist.ts_code, Blacklist.expires_at).where(
            Blacklist.ts_code.in_(ts_codes)
        )
        rows = db.execute(stmt).fetchall()
        for row in rows:
            bl_code, expires_at = row
            # expires_at 为 None（永久）或未到期
            if expires_at is None or expires_at > today:
                excluded.add(bl_code)

    return excluded
