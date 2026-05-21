"""
筛选过滤器 — 多源民企识别 + 硬性阈值过滤

民企识别改进（修复原代码静默失效问题）：
1. 优先使用 DB 中 act_ent_type 字段（来自 Tushare stock_company）
2. act_ent_type 缺失时回退到 act_name 关键词匹配
3. 仍无法判断时标记为 unknown，日志标记降级

P1 新增：
- apply_hard_filters 支持杜邦/盈余质量/分红连续性 三个新硬性阈值
"""
from __future__ import annotations

from app.core.logging import get_logger

logger = get_logger("filters")

# 国企/央企关键词（act_ent_type 字段值或 act_name 包含以下词则认为是国有）
_SOE_ENT_TYPES = {
    "国有企业", "国有", "country", "central", "央企", "国企",
    "地方国有企业", "中央国有企业",
}

_SOE_NAME_KEYWORDS = [
    "国资委", "人民政府", "财政局", "国有资产", "发展局",
    "建委", "交通局", "住建局", "教育局", "卫健委",
    "中央汇金", "社保基金", "中国证券", "中国人寿",
]

_PRIVATE_ENT_TYPES = {
    "私营企业", "民营", "private", "个人", "自然人",
    "家族控制", "创始人", "自然人持股",
}


def is_private_enterprise(
    act_ent_type: str,
    act_name: str,
    ts_code: str = "",
) -> tuple[bool, str]:
    """
    判断是否为民营企业

    Args:
        act_ent_type: Tushare stock_company.act_ent_type 字段值
        act_name:     实际控制人名称
        ts_code:      股票代码（日志用）

    Returns:
        (is_private, method)
        - is_private: True = 民企，False = 非民企
        - method: 判断依据（'ent_type' / 'name_keyword' / 'unknown'）
    """
    # 优先级 1：act_ent_type 精确匹配
    if act_ent_type:
        normalized = act_ent_type.strip()
        for soe_type in _SOE_ENT_TYPES:
            if soe_type in normalized:
                return False, "ent_type"
        for priv_type in _PRIVATE_ENT_TYPES:
            if priv_type in normalized:
                return True, "ent_type"

    # 优先级 2：act_name 关键词匹配（兜底）
    if act_name:
        for keyword in _SOE_NAME_KEYWORDS:
            if keyword in act_name:
                if ts_code:
                    logger.debug(
                        "民企识别降级：使用 act_name 关键词",
                        ts_code=ts_code,
                        act_name=act_name,
                        keyword=keyword,
                    )
                return False, "name_keyword"

    # 优先级 3：无法判断
    if ts_code and not act_ent_type and not act_name:
        logger.debug(
            "民企识别未知：act_ent_type 和 act_name 均为空",
            ts_code=ts_code,
        )
    return True, "unknown"  # 宽松策略：无法证明是国企则视为民企


def apply_prescreen_filter(
    ts_code: str,
    *,
    exchange: str = "",
    pe_ttm: float | None = None,
    total_mv_yi: float | None = None,
    exclude_bj: bool = True,
    prescreen_pe_ttm_max: float = 20.0,
    prescreen_total_mv_max_yi: float = 100.0,
) -> tuple[bool, str]:
    """
    拉取财务数据前的粗筛（速度优化）

    用 daily_basic 中已有的 pe_ttm 和 total_mv 做快速判断，跳过明显
    不合格的股票，避免为它们发起财务接口请求。

    Args:
        ts_code:                 股票代码（日志用）
        exchange:                交易所（'BSE' = 北交所）
        pe_ttm:                  市盈率TTM（来自 daily_basic）
        total_mv_yi:             总市值（亿元，来自 daily_basic total_mv / 10000）
        exclude_bj:              是否排除北交所
        prescreen_pe_ttm_max:    PE_TTM 上限（0 = 不限）
        prescreen_total_mv_max_yi: 市值上限亿元（0 = 不限）

    Returns:
        (eligible, reason)
        - eligible: True = 通过粗筛，需继续拉财务；False = 直接跳过
        - reason:   跳过原因（eligible=True 时为空字符串）
    """
    # 北交所直接跳过
    if exclude_bj and exchange.upper() == "BSE":
        return False, "北交所股票"

    # 市值 > 上限
    if (
        prescreen_total_mv_max_yi > 0
        and total_mv_yi is not None
        and total_mv_yi > prescreen_total_mv_max_yi
    ):
        return False, f"市值={total_mv_yi:.1f}亿>{prescreen_total_mv_max_yi}亿（预筛）"

    # PE_TTM 超标或亏损
    if prescreen_pe_ttm_max > 0:
        if pe_ttm is None:
            # 无 PE 数据（停牌等）→ 保留继续拉财务
            return True, ""
        if pe_ttm <= 0:
            return False, f"PE_TTM={pe_ttm:.1f}（亏损，预筛）"
        if pe_ttm > prescreen_pe_ttm_max:
            return False, f"PE_TTM={pe_ttm:.1f}>{prescreen_pe_ttm_max}（预筛）"

    return True, ""


def apply_hard_filters(
    ts_code: str,
    *,
    exchange: str = "",
    is_private: bool = True,
    pe_ttm: float | None = None,
    pe_deduct_ttm: float | None = None,
    dv_ttm: float | None = None,
    total_mv_yi: float | None = None,     # 亿元
    high_leverage_flag: bool = False,      # P1：杜邦高杠杆红旗
    cfo_to_np_ratio: float | None = None,  # P1：CFO/净利润比值
    dividend_continuity: int | None = None,# P1：连续分红年数
    clearance_dividend_flag: bool = False, # P1：清仓式分红红旗
    # 阈值
    exclude_bj: bool = True,
    exclude_soe: bool = True,
    pe_ttm_max: float = 30.0,
    pe_deduct_max: float = 20.0,
    dv_ttm_min: float = 2.0,
    total_mv_max_yi: float = 100.0,
    # P1 新增阈值
    max_eqt_multiplier: float = 3.0,       # 0 = 不限
    min_cfo_to_np: float = 0.5,            # 0 = 不限
    min_div_continuity_years: int = 3,     # 0 = 不限
    reject_clearance_div: bool = True,
) -> tuple[bool, list[str]]:
    """
    应用硬性筛选条件（P1 增加三个新过滤维度）

    Args:
        ts_code:                股票代码（日志用）
        exchange:               交易所
        is_private:             是否民营企业
        pe_ttm:                 市盈率 TTM
        pe_deduct_ttm:          扣非 PE TTM
        dv_ttm:                 股息率 %
        total_mv_yi:            总市值（亿元）
        high_leverage_flag:     P1 杜邦高杠杆红旗
        cfo_to_np_ratio:        P1 CFO/净利润比值
        dividend_continuity:    P1 连续分红年数
        clearance_dividend_flag: P1 清仓式分红红旗
        exclude_bj:             是否排除北交所
        exclude_soe:            是否排除国企
        pe_ttm_max:             PE TTM 上限
        pe_deduct_max:          扣非 PE 上限
        dv_ttm_min:             股息率下限
        total_mv_max_yi:        市值上限
        max_eqt_multiplier:     权益乘数上限（0=不限）
        min_cfo_to_np:          CFO/NP 下限（0=不限）
        min_div_continuity_years: 最低连续分红年数（0=不限）
        reject_clearance_div:   是否剔除清仓式分红

    Returns:
        (passed, fail_reasons)
    """
    fail_reasons: list[str] = []

    # 北交所排除
    if exclude_bj and exchange.upper() == "BSE":
        fail_reasons.append("北交所股票")

    # 国企排除
    if exclude_soe and not is_private:
        fail_reasons.append("非民营企业")

    # PE TTM 上限
    if pe_ttm is not None:
        if pe_ttm <= 0:
            fail_reasons.append(f"PE_TTM={pe_ttm:.1f}（亏损）")
        elif pe_ttm > pe_ttm_max:
            fail_reasons.append(f"PE_TTM={pe_ttm:.1f}>{pe_ttm_max}")

    # 扣非 PE TTM 上限
    if pe_deduct_ttm is not None:
        if pe_deduct_ttm <= 0:
            fail_reasons.append(f"扣非PE_TTM={pe_deduct_ttm:.1f}（扣非亏损）")
        elif pe_deduct_ttm > pe_deduct_max:
            fail_reasons.append(f"扣非PE_TTM={pe_deduct_ttm:.1f}>{pe_deduct_max}")

    # 股息率下限
    if dv_ttm is not None and dv_ttm < dv_ttm_min:
        fail_reasons.append(f"股息率={dv_ttm:.2f}%<{dv_ttm_min}%")

    # 市值上限
    if total_mv_max_yi > 0 and total_mv_yi is not None and total_mv_yi > total_mv_max_yi:
        fail_reasons.append(f"总市值={total_mv_yi:.1f}亿>{total_mv_max_yi}亿")

    # ── P1：杜邦高杠杆过滤 ─────────────────────────────────────────────
    if max_eqt_multiplier > 0 and high_leverage_flag:
        fail_reasons.append(f"高杠杆粉饰ROE（权益乘数>{max_eqt_multiplier}或资产负债率>70%）")
        logger.debug(
            "filters.reject_high_leverage",
            ts_code=ts_code,
        )

    # ── P1：盈余质量过滤 ────────────────────────────────────────────────
    if min_cfo_to_np > 0 and cfo_to_np_ratio is not None and cfo_to_np_ratio < min_cfo_to_np:
        fail_reasons.append(
            f"盈余质量不足(CFO/NP={cfo_to_np_ratio:.2f}<{min_cfo_to_np})"
        )

    # ── P1：分红连续性过滤 ──────────────────────────────────────────────
    if (
        min_div_continuity_years > 0
        and dividend_continuity is not None
        and dividend_continuity < min_div_continuity_years
    ):
        fail_reasons.append(
            f"连续分红年数={dividend_continuity}<{min_div_continuity_years}年"
        )

    # ── P1：清仓式分红过滤 ──────────────────────────────────────────────
    if reject_clearance_div and clearance_dividend_flag:
        fail_reasons.append("疑似清仓式分红（单年派现占近5年总额>70%）")
        logger.warning(
            "filters.reject_clearance_dividend",
            ts_code=ts_code,
        )

    return len(fail_reasons) == 0, fail_reasons
