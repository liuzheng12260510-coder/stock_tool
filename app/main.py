"""
StockSentry — FastAPI 应用工厂 + 启动入口
"""
from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.api.routes import export, insight, jobs, stocks
from app.core.config import settings
from app.core.logging import configure_logging, get_logger
from app.db.models import Base
from app.db.session import engine

# 日志配置（最先初始化）
configure_logging(settings.logs_dir, debug=settings.web_debug)
logger = get_logger("main")

# 确保数据库表存在
Base.metadata.create_all(bind=engine)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """应用生命周期管理"""
    logger.info(
        "StockSentry 启动",
        host=settings.web_host,
        port=settings.web_port,
        database=settings.database_url,
    )

    # 启动调度器
    from app.jobs.scheduler import start_scheduler
    start_scheduler()

    yield  # 应用运行中

    # 停止调度器
    from app.jobs.scheduler import stop_scheduler
    stop_scheduler()

    logger.info("StockSentry 已停止")


def create_app() -> FastAPI:
    """FastAPI 应用工厂"""
    app = FastAPI(
        title="StockSentry 个股哨兵",
        description="A股民营企业价值筛选系统",
        version="1.0.0",
        lifespan=lifespan,
        docs_url="/api/docs",
        redoc_url="/api/redoc",
    )

    # ── 静态文件 ─────────────────────────────────────────────────────────
    from pathlib import Path

    static_dir = Path(__file__).parent / "web" / "static"
    static_dir.mkdir(parents=True, exist_ok=True)
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    # ── API 路由 ─────────────────────────────────────────────────────────
    app.include_router(stocks.router)
    app.include_router(insight.router)
    app.include_router(jobs.router)
    app.include_router(export.router)

    # ── 健康检查 ─────────────────────────────────────────────────────────
    @app.get("/api/health")
    def health_check() -> dict:
        return {
            "status": "ok",
            "app": "StockSentry",
            "version": "1.0.0",
            "ai_enabled": settings.ai_available,
            "scheduler_enabled": settings.scheduler_enabled,
        }

    # ── Web 页面路由 ──────────────────────────────────────────────────────
    from app.web.routes import web_router
    app.include_router(web_router)

    return app


# 模块级实例（供 uvicorn 直接引用）
app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=settings.web_host,
        port=settings.web_port,
        reload=settings.web_debug,
        log_level="debug" if settings.web_debug else "info",
    )
