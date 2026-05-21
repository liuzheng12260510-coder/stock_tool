"""
Excel 导出 API 路由
"""
from __future__ import annotations

from datetime import date
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse

from app.core.exceptions import ExportError
from app.core.logging import get_logger

logger = get_logger("export_router")
router = APIRouter(prefix="/api/export", tags=["export"])


@router.get("/excel")
def export_excel(
    trade_date: Optional[str] = Query(None, description="交易日 YYYY-MM-DD，默认最近"),
) -> FileResponse:
    """
    按需导出筛选结果为 Excel 文件

    生成后直接下载，文件名格式：StockSentry_YYYY-MM-DD.xlsx
    """
    from app.services.export import export_screened_to_excel

    parsed_date = None
    if trade_date:
        try:
            parsed_date = date.fromisoformat(trade_date)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"日期格式错误: {trade_date}")

    try:
        filepath = export_screened_to_excel(trade_date=parsed_date)
        return FileResponse(
            path=str(filepath),
            filename=filepath.name,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
    except ExportError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.exception("Excel 导出失败", error=str(e))
        raise HTTPException(status_code=500, detail="导出失败")
