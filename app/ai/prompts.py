"""
AI 分析 Prompt 模板 — 5 维度分析（P3 增强）

P3 变更：
- build_stock_analysis_prompt 增加杜邦/CFO/分红财务上下文参数
- INSIGHT_JSON_SCHEMA 新增 financial_red_flags 字段（数组）
"""
from __future__ import annotations

from app.domain.models import StockSnapshot

SYSTEM_PROMPT = """你是一位专注于A股民营上市公司价值投资的研究员。
你的分析风格：保守、客观、基于财务数据，避免过度乐观。
回答必须严格按照指定的 JSON Schema 格式输出，不得添加额外字段或 Markdown 格式。"""


def build_stock_analysis_prompt(
    snapshot: StockSnapshot,
    extra_context: str = "",
    dupont_context: str = "",
    cfo_context: str = "",
    dividend_context: str = "",
) -> str:
    """
    构建单股 5 维分析 Prompt（P3：补充杜邦/CFO/分红财务上下文）

    Args:
        snapshot:         股票快照（基本行情 + 评分）
        extra_context:    额外自由文本上下文（旧版兼容）
        dupont_context:   杜邦三因子最新值 + 同业百分位描述
        cfo_context:      CFO/NP 近 4 季趋势描述
        dividend_context: 连续分红年数 + 派息额走势描述
    """
    mv_yi = f"{snapshot.total_mv_yi:.1f}亿" if snapshot.total_mv_yi else "未知"
    pe_str = (
        f"{snapshot.pe_deduct_ttm:.1f}"
        if snapshot.pe_deduct_ttm
        else (f"{snapshot.pe_ttm:.1f}" if snapshot.pe_ttm else "未知")
    )
    dv_str = f"{snapshot.dv_ttm:.2f}%" if snapshot.dv_ttm else "未知"
    score_str = f"{snapshot.composite_score:.1f}" if snapshot.composite_score else "未知"

    # 财务健康上下文（P3 新增）
    financial_section = ""
    if dupont_context or cfo_context or dividend_context:
        parts = []
        if dupont_context:
            parts.append(f"**杜邦分析**：{dupont_context}")
        if cfo_context:
            parts.append(f"**盈余质量（CFO/NP）**：{cfo_context}")
        if dividend_context:
            parts.append(f"**分红历史**：{dividend_context}")
        financial_section = "\n## 财务健康数据\n" + "\n".join(parts) + "\n"

    # P3 新增：杜邦/盈余质量/分红红旗快速摘要（从 snapshot 字段读取）
    flag_lines = []
    if snapshot.high_leverage_flag:
        flag_lines.append("⚠️ 高杠杆红旗：权益乘数>3 或资产负债率>70%，ROE 可能含水分")
    if snapshot.earnings_quality_score is not None and snapshot.earnings_quality_score < 30:
        flag_lines.append(
            f"⚠️ 低盈余质量：CFO/NP 评分={snapshot.earnings_quality_score:.0f}/100"
        )
    if snapshot.dividend_continuity is not None and snapshot.dividend_continuity < 3:
        flag_lines.append(
            f"⚠️ 分红连续性弱：仅连续 {snapshot.dividend_continuity} 年"
        )
    flags_section = ""
    if flag_lines:
        flags_section = "\n## 已识别财务红旗\n" + "\n".join(flag_lines) + "\n"

    prompt = f"""请对以下A股民营上市公司进行5维度分析：

## 股票基本信息
- 股票代码：{snapshot.ts_code}
- 公司名称：{snapshot.name}
- 所属行业：{snapshot.industry}
- 总市值：{mv_yi}
- 扣非PE TTM：{pe_str}
- 股息率：{dv_str}
- 综合评分：{score_str}/100
- 杜邦综合分：{f"{snapshot.dupont_score:.1f}" if snapshot.dupont_score else "未知"}/100
- 连续分红年数：{snapshot.dividend_continuity if snapshot.dividend_continuity is not None else "未知"}年
{financial_section}{flags_section}{extra_context}
## 分析要求
请从以下5个维度进行分析，每个维度200字以内，客观务实：

1. **出海营收模式**：分析该公司是否有海外营收，出海策略和占比，主要市场布局
2. **本地化程度**：评估其在目标市场的本地化运营深度，包括团队、供应链、品牌
3. **核心商业逻辑**：解释该公司的核心盈利模式和竞争优势来源
4. **护城河评估**：分析可持续竞争优势的宽度和深度（品牌/专利/网络效应/成本优势等）
5. **关键风险**：列出最值得关注的3-5个风险因素

同时请综合以上所有财务数据，在 financial_red_flags 字段中列出你识别到的所有财务异常。

请严格按照 JSON Schema 格式输出，字段名必须完全匹配。"""
    return prompt


# JSON Schema — 强制结构化输出（P3：新增 financial_red_flags 数组）
INSIGHT_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "overseas_revenue_pattern": {
            "type": "string",
            "description": "出海营收模式分析（200字以内）",
        },
        "localization_level": {
            "type": "string",
            "description": "本地化程度评估（200字以内）",
        },
        "core_business_logic": {
            "type": "string",
            "description": "核心商业逻辑分析（200字以内）",
        },
        "moat_assessment": {
            "type": "string",
            "description": "护城河评估（200字以内）",
        },
        "key_risks": {
            "type": "string",
            "description": "关键风险列表（200字以内）",
        },
        "financial_red_flags": {
            "type": "array",
            "items": {"type": "string"},
            "description": "AI 综合识别的财务异常列表（如高杠杆、低盈余质量、清仓式分红等），无异常则返回空数组",
        },
    },
    "required": [
        "overseas_revenue_pattern",
        "localization_level",
        "core_business_logic",
        "moat_assessment",
        "key_risks",
        "financial_red_flags",
    ],
    "additionalProperties": False,
}
