"""
杜邦分解分析模块

职责：
- 从最新季度数据提取杜邦三要素（净利率 / 资产周转率 / 权益乘数）
- 识别高杠杆粉饰 ROE 红旗
- 输出 DupontResult + 综合得分（0-100，绝对分非横截面）

高杠杆红旗规则：
  eqt_multiplier > 3.0  OR  debt_to_assets > 70%
"""
from __future__ import annotations

from dataclasses import dataclass

from app.core.logging import get_logger
from app.domain.models import QuarterRecord

logger = get_logger("dupont")


@dataclass
class DupontResult:
    """杜邦分解结果"""

    roe: float | None                # ROE %（净资产收益率）
    npta: float | None               # 总资产净利率 %
    assets_turn: float | None        # 总资产周转率（次/年）
    eqt_multiplier: float | None     # 权益乘数
    debt_to_assets: float | None     # 资产负债率 %
    leverage_warning: bool           # 高杠杆粉饰 ROE 红旗


def compute_dupont(quarter: QuarterRecord) -> DupontResult:
    """
    从单季财务数据计算杜邦分解

    Args:
        quarter: 最新季度财务记录（含杜邦字段）

    Returns:
        DupontResult，含高杠杆红旗标识
    """
    roe = quarter.roe
    npta = quarter.npta
    assets_turn = quarter.assets_turn
    eqt_multiplier = quarter.eqt_multiplier
    debt_to_assets = quarter.debt_to_assets

    # 高杠杆红旗：权益乘数 > 3.0 或资产负债率 > 70%
    leverage_warning = False
    if eqt_multiplier is not None and eqt_multiplier > 3.0:
        leverage_warning = True
        logger.debug(
            "dupont.high_leverage_eqt_multiplier",
            ts_code=quarter.ts_code,
            eqt_multiplier=eqt_multiplier,
        )
    if debt_to_assets is not None and debt_to_assets > 70.0:
        leverage_warning = True
        logger.debug(
            "dupont.high_leverage_debt_ratio",
            ts_code=quarter.ts_code,
            debt_to_assets=debt_to_assets,
        )

    return DupontResult(
        roe=roe,
        npta=npta,
        assets_turn=assets_turn,
        eqt_multiplier=eqt_multiplier,
        debt_to_assets=debt_to_assets,
        leverage_warning=leverage_warning,
    )


def score_dupont(result: DupontResult) -> float | None:
    """
    将杜邦结果转换为 0-100 综合分（绝对阈值，非横截面）

    评分逻辑：
    - 基础分来自 ROE（0-20% ROE 线性映射到 0-100 分）
    - ROE >= 20% 给满分 100
    - ROE <= 0 给 0 分
    - 高杠杆红旗：扣 30 分惩罚（下限 0）

    Args:
        result: DupontResult

    Returns:
        0-100 分，ROE 缺失返回 None
    """
    if result.roe is None:
        return None

    roe = result.roe
    if roe <= 0:
        base_score = 0.0
    elif roe >= 20.0:
        base_score = 100.0
    else:
        base_score = roe / 20.0 * 100.0

    # 高杠杆惩罚
    if result.leverage_warning:
        base_score = max(0.0, base_score - 30.0)

    return round(base_score, 2)
