"""
OpenRouter AI Provider — JSON Schema 强制结构化输出
修复原代码缺陷：不再解析 Markdown，改用 response_format JSON Schema
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from app.ai.prompts import INSIGHT_JSON_SCHEMA, SYSTEM_PROMPT, build_stock_analysis_prompt
from app.core.config import settings
from app.core.exceptions import AIProviderError, AIUnavailableError
from app.core.logging import get_logger
from app.domain.models import AIInsight, StockSnapshot

logger = get_logger("openrouter")


class OpenRouterProvider:
    """
    OpenRouter Claude Provider

    关键改进（vs 原代码）：
    - 使用 response_format={"type":"json_schema"} 强制结构化输出
    - pydantic 直接 parse_obj()，不再解析 Markdown 代码块
    - httpx 异步改同步（避免 FastAPI → asyncio 嵌套问题）
    """

    def __init__(self) -> None:
        if not settings.ai_available:
            raise AIUnavailableError(
                "AI 功能不可用：请在 .env 中设置 OPENROUTER_API_KEY"
            )
        self._headers = {
            "Authorization": f"Bearer {settings.openrouter_api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/stocksentry",
            "X-Title": "StockSentry",
        }
        self._base_url = settings.ai_base_url.rstrip("/")
        logger.info("OpenRouterProvider 初始化完成", model=settings.ai_model)

    @retry(
        stop=stop_after_attempt(2),
        wait=wait_exponential(multiplier=1, min=5, max=30),
        reraise=True,
    )
    def analyze_stock(
        self,
        snapshot: StockSnapshot,
        dupont_context: str = "",
        cfo_context: str = "",
        dividend_context: str = "",
    ) -> AIInsight:
        """
        分析单只股票，返回 5 维度 AIInsight（P3：含财务上下文）

        Args:
            snapshot:         股票快照（含估值数据）
            dupont_context:   杜邦三因子文字摘要（P3 新增）
            cfo_context:      CFO/NP 近4季趋势摘要（P3 新增）
            dividend_context: 分红历史文字摘要（P3 新增）

        Returns:
            AIInsight（status='success'）

        Raises:
            AIProviderError: 调用失败
        """
        user_prompt = build_stock_analysis_prompt(
            snapshot,
            dupont_context=dupont_context,
            cfo_context=cfo_context,
            dividend_context=dividend_context,
        )
        _ = hashlib.md5(
            (snapshot.ts_code + user_prompt).encode()
        ).hexdigest()[:16]

        payload: dict[str, Any] = {
            "model": settings.ai_model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "stock_insight",
                    "strict": True,
                    "schema": INSIGHT_JSON_SCHEMA,
                },
            },
            "max_tokens": 2000,
            "temperature": 0.3,
        }

        logger.info("调用 AI 分析", ts_code=snapshot.ts_code, model=settings.ai_model)

        try:
            proxies = {}
            if settings.http_proxy:
                proxies["http://"] = settings.http_proxy
            if settings.https_proxy:
                proxies["https://"] = settings.https_proxy

            with httpx.Client(
                timeout=settings.ai_timeout_seconds,
                proxies=proxies if proxies else None,  # type: ignore[arg-type]
            ) as client:
                resp = client.post(
                    f"{self._base_url}/chat/completions",
                    headers=self._headers,
                    json=payload,
                )

            if resp.status_code != 200:
                raise AIProviderError(
                    f"OpenRouter 返回 {resp.status_code}: {resp.text[:500]}"
                )

            data = resp.json()
            content = data["choices"][0]["message"]["content"]

            # JSON Schema 模式下，content 直接是合法 JSON 字符串
            parsed = json.loads(content)

            # 解析 financial_red_flags（P3 新增字段，兼容旧版 AI 未返回时）
            raw_flags = parsed.get("financial_red_flags", [])
            flags = raw_flags if isinstance(raw_flags, list) else []

            insight = AIInsight(
                ts_code=snapshot.ts_code,
                overseas_revenue_pattern=parsed.get("overseas_revenue_pattern", ""),
                localization_level=parsed.get("localization_level", ""),
                core_business_logic=parsed.get("core_business_logic", ""),
                moat_assessment=parsed.get("moat_assessment", ""),
                key_risks=parsed.get("key_risks", ""),
                financial_red_flags=flags,
                model=settings.ai_model,
                status="success",
                from_cache=False,
            )

            logger.info("AI 分析成功", ts_code=snapshot.ts_code)
            return insight

        except AIProviderError:
            raise
        except json.JSONDecodeError as e:
            raise AIProviderError(f"AI 输出 JSON 解析失败: {e}") from e
        except Exception as e:
            raise AIProviderError(f"OpenRouter 调用异常: {e}") from e
