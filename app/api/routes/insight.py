"""
AI 解读 API 路由
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from app.core.exceptions import AIProviderError, AIUnavailableError
from app.core.logging import get_logger

logger = get_logger("insight_router")
router = APIRouter(prefix="/api/stocks", tags=["insight"])


@router.post("/{ts_code}/insight")
def get_insight(ts_code: str) -> JSONResponse:
    """
    按需触发 AI 解读（命中缓存直接返回，否则调用 API）

    - 第一次调用：调用 OpenRouter API，约 30-60 秒
    - 第二次及以后：命中 DB 缓存，即时返回
    """
    from app.ai.service import get_or_create_insight

    try:
        insight = get_or_create_insight(ts_code)
        return JSONResponse({
            "ts_code": ts_code,
            "from_cache": insight.from_cache,
            "model": insight.model,
            "created_at": insight.created_at,
            "status": insight.status,
            "overseas_revenue_pattern": insight.overseas_revenue_pattern,
            "localization_level": insight.localization_level,
            "core_business_logic": insight.core_business_logic,
            "moat_assessment": insight.moat_assessment,
            "key_risks": insight.key_risks,
        })
    except AIUnavailableError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except AIProviderError as e:
        raise HTTPException(status_code=502, detail=f"AI 调用失败: {str(e)}")
    except Exception as e:
        logger.exception("AI 解读异常", ts_code=ts_code, error=str(e))
        raise HTTPException(status_code=500, detail="内部错误")
