"""
综合评分 — 横截面 z-score 归一化（每个交易日全市场算分位数）

评分逻辑：
1. 取全市场（通过硬性筛选的股票）各因子值
2. 计算 z-score，截尾 ±3σ
3. 线性映射到 [0, 100]
4. 按权重加权得到 composite_score
5. 计算 composite_rank（全市场百分位）
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from app.core.logging import get_logger
from app.domain.models import FactorResult

logger = get_logger("scoring")

# 评分权重（5 个维度各 20%）
SCORE_WEIGHTS = {
    "value": 0.20,
    "growth": 0.20,
    "stability": 0.20,
    "dividend": 0.20,
    "safety": 0.20,
}

# 各因子越高越好 (True) 还是越低越好 (False)
_FACTOR_DIRECTION = {
    "pe_deduct_ttm": False,   # 越低越好
    "growth_rate": True,      # 越高越好
    "volatility": False,       # 越低越好（稳定性）
    "dv_ttm": True,           # 越高越好（股息率）
    "safety_margin": True,    # 越高越好
}


def _zscore_normalize(values: np.ndarray, higher_is_better: bool = True) -> np.ndarray:
    """
    z-score 归一化 → 截尾 ±3 → 映射到 [0, 100]

    Args:
        values:            一列因子值，NaN 已处理
        higher_is_better:  True = 越大越高分，False = 越小越高分

    Returns:
        归一化后的 0-100 分数组（NaN 位置保持 NaN）
    """
    out = np.full_like(values, np.nan, dtype=float)
    valid_mask = ~np.isnan(values)
    if valid_mask.sum() < 5:
        # 有效样本太少，无法计算横截面
        return out

    valid = values[valid_mask]
    mean = np.mean(valid)
    std = np.std(valid, ddof=1)

    if std < 1e-10:
        # 所有值相同，统一给 50 分
        out[valid_mask] = 50.0
        return out

    z = (valid - mean) / std
    # 截尾 ±3σ
    z = np.clip(z, -3, 3)
    # 线性映射到 [0, 100]
    scores = (z + 3) / 6 * 100

    if not higher_is_better:
        scores = 100 - scores

    out[valid_mask] = scores
    return out


def compute_cross_sectional_scores(
    factors: list[FactorResult],
) -> list[FactorResult]:
    """
    对一个交易日的全市场因子列表，计算横截面评分

    Args:
        factors: 所有股票的 FactorResult 列表（原始因子已填充）

    Returns:
        填充了维度得分和 composite_score / composite_rank 的 FactorResult 列表
    """
    if not factors:
        return factors

    n = len(factors)
    logger.info("开始横截面评分", n_stocks=n)

    # 构建 DataFrame 方便向量化操作
    df = pd.DataFrame({
        "idx": range(n),
        "pe_deduct_ttm": [f.pe_deduct_ttm for f in factors],
        "growth_rate": [f.growth_rate for f in factors],
        "volatility": [f.volatility for f in factors],
        "dv_ttm": [f.dv_ttm for f in factors],
        "safety_margin": [f.safety_margin for f in factors],
    })

    # 各列转为 float，无效值设为 NaN
    for col in ["pe_deduct_ttm", "growth_rate", "volatility", "dv_ttm", "safety_margin"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    # 计算各维度得分（显式转换为 np.ndarray 避免 Pylance 类型误报）
    df["value_score"] = _zscore_normalize(
        np.array(df["pe_deduct_ttm"].values, dtype=float), higher_is_better=False
    )
    df["growth_score"] = _zscore_normalize(
        np.array(df["growth_rate"].values, dtype=float), higher_is_better=True
    )
    df["stability_score"] = _zscore_normalize(
        np.array(df["volatility"].values, dtype=float), higher_is_better=False
    )
    df["dividend_score"] = _zscore_normalize(
        np.array(df["dv_ttm"].values, dtype=float), higher_is_better=True
    )
    df["safety_score"] = _zscore_normalize(
        np.array(df["safety_margin"].values, dtype=float), higher_is_better=True
    )

    # 综合评分（缺失维度不参与加权，权重重新归一化）
    score_cols = ["value_score", "growth_score", "stability_score", "dividend_score", "safety_score"]
    weights = np.array([
        SCORE_WEIGHTS["value"],
        SCORE_WEIGHTS["growth"],
        SCORE_WEIGHTS["stability"],
        SCORE_WEIGHTS["dividend"],
        SCORE_WEIGHTS["safety"],
    ])

    composite = []
    for i in range(n):
        row_scores = df[score_cols].iloc[i].values.astype(float)
        valid = ~np.isnan(row_scores)
        if valid.sum() == 0:
            composite.append(np.nan)
        else:
            w = weights[valid]
            w = w / w.sum()  # 重新归一化
            composite.append(float(np.dot(row_scores[valid], w)))

    df["composite_score"] = composite

    # 全市场百分位 (0-100)
    valid_scores = df["composite_score"].dropna()
    if len(valid_scores) > 0:
        df["composite_rank"] = df["composite_score"].rank(pct=True) * 100
    else:
        df["composite_rank"] = np.nan

    # 回写到 FactorResult
    for i, factor in enumerate(factors):
        row = df.iloc[i]
        factor.value_score = _nan_to_none(row["value_score"])
        factor.growth_score = _nan_to_none(row["growth_score"])
        factor.stability_score = _nan_to_none(row["stability_score"])
        factor.dividend_score = _nan_to_none(row["dividend_score"])
        factor.safety_score = _nan_to_none(row["safety_score"])
        factor.composite_score = _nan_to_none(row["composite_score"])
        factor.composite_rank = _nan_to_none(row["composite_rank"])

    logger.info("横截面评分完成", n_stocks=n)
    return factors


def _nan_to_none(v) -> float | None:
    if v is None:
        return None
    try:
        f = float(v)
        return None if np.isnan(f) else round(f, 2)
    except (TypeError, ValueError):
        return None
