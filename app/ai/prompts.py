"""
AI 分析 Prompt 模板 — 5 维度分析
"""
from __future__ import annotations

from app.domain.models import StockSnapshot

SYSTEM_PROMPT = """你是一位专注于A股民营上市公司价值投资的研究员。
你的分析风格：保守、客观、基于财务数据，避免过度乐观。
回答必须严格按照指定的 JSON Schema 格式输出，不得添加额外字段或 Markdown 格式。"""


def build_stock_analysis_prompt(snapshot: StockSnapshot, extra_context: str = "") -> str:
    """构建单股 5 维分析 Prompt"""
    mv_yi = f"{snapshot.total_mv_yi:.1f}亿" if snapshot.total_mv_yi else "未知"
    pe_str = f"{snapshot.pe_deduct_ttm:.1f}" if snapshot.pe_deduct_ttm else f"{snapshot.pe_ttm:.1f}" if snapshot.pe_ttm else "未知"
    dv_str = f"{snapshot.dv_ttm:.2f}%" if snapshot.dv_ttm else "未知"
    score_str = f"{snapshot.composite_score:.1f}" if snapshot.composite_score else "未知"

    prompt = f"""请对以下A股民营上市公司进行5维度分析：

## 股票基本信息
- 股票代码：{snapshot.ts_code}
- 公司名称：{snapshot.name}
- 所属行业：{snapshot.industry}
- 总市值：{mv_yi}
- 扣非PE TTM：{pe_str}
- 股息率：{dv_str}
- 综合评分：{score_str}/100

{extra_context}

## 分析要求
请从以下5个维度进行分析，每个维度200字以内，客观务实：

1. **出海营收模式**：分析该公司是否有海外营收，出海策略和占比，主要市场布局
2. **本地化程度**：评估其在目标市场的本地化运营深度，包括团队、供应链、品牌
3. **核心商业逻辑**：解释该公司的核心盈利模式和竞争优势来源
4. **护城河评估**：分析可持续竞争优势的宽度和深度（品牌/专利/网络效应/成本优势等）
5. **关键风险**：列出最值得关注的3-5个风险因素

请严格按照 JSON Schema 格式输出，字段名必须完全匹配。"""
    return prompt


# JSON Schema — 强制结构化输出
INSIGHT_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "overseas_revenue_pattern": {
            "type": "string",
            "description": "出海营收模式分析（200字以内）"
        },
        "localization_level": {
            "type": "string",
            "description": "本地化程度评估（200字以内）"
        },
        "core_business_logic": {
            "type": "string",
            "description": "核心商业逻辑分析（200字以内）"
        },
        "moat_assessment": {
            "type": "string",
            "description": "护城河评估（200字以内）"
        },
        "key_risks": {
            "type": "string",
            "description": "关键风险列表（200字以内）"
        }
    },
    "required": [
        "overseas_revenue_pattern",
        "localization_level",
        "core_business_logic",
        "moat_assessment",
        "key_risks"
    ],
    "additionalProperties": False
}
