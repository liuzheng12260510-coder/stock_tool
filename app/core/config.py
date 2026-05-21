"""
配置中心 — pydantic-settings 读取 .env，提供全局单例 settings
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Optional

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── 数据源 ─────────────────────────────────────────────────────────
    tushare_token: str  # 必填，无默认值

    # ── AI ─────────────────────────────────────────────────────────────
    openrouter_api_key: str = ""  # 可选，不填则 AI 功能不可用
    ai_model: str = "anthropic/claude-sonnet-4-5"
    ai_timeout_seconds: int = 120
    ai_base_url: str = "https://openrouter.ai/api/v1"

    # ── 代理 ────────────────────────────────────────────────────────────
    http_proxy: Optional[str] = None
    https_proxy: Optional[str] = None

    # ── 调度 ────────────────────────────────────────────────────────────
    scheduler_enabled: bool = True
    scheduler_cron_hour: int = 15
    scheduler_cron_minute: int = 30

    # ── 筛选阈值 ────────────────────────────────────────────────────────
    screen_pe_ttm_max: float = 30.0
    screen_pe_deduct_max: float = 20.0
    screen_dv_ttm_min: float = 2.0
    screen_total_mv_max_yi: float = 100.0   # 亿元，0 = 不限
    screen_exclude_bj: bool = True           # 排除北交所
    screen_exclude_soe: bool = True          # 排除国企/央企
    screen_min_composite_score: float = 60.0

    # ── 预筛阈值（拉财务数据前的速度优化粗筛）────────────────────────────
    # 用 daily_basic 的 pe_ttm 作为扣非PE的代理（pe_deduct_ttm >= pe_ttm），
    # 命中任一条件即在拉财务、存快照前直接跳过，大幅缩小数据请求量。
    prescreen_pe_ttm_max: float = 20.0       # PE_TTM 超过此值直接跳过，0 = 不限
    prescreen_total_mv_max_yi: float = 100.0 # 市值（亿）超过此值直接跳过，0 = 不限

    # ── 数据库 ──────────────────────────────────────────────────────────
    database_url: str = "sqlite:///./data/stocksentry.db"

    # ── Web ─────────────────────────────────────────────────────────────
    web_host: str = "127.0.0.1"
    web_port: int = 8000
    web_debug: bool = False

    # ── Tushare 限速 ────────────────────────────────────────────────────
    tushare_rate_limit: int = 180  # req/min

    # ── 路径 ─────────────────────────────────────────────────────────────
    base_dir: Path = Path(__file__).parent.parent.parent
    data_dir: Path = Path(__file__).parent.parent.parent / "data"
    exports_dir: Path = Path(__file__).parent.parent.parent / "exports"
    logs_dir: Path = Path(__file__).parent.parent.parent / "logs"

    @field_validator("tushare_token")
    @classmethod
    def token_not_empty(cls, v: str) -> str:
        if not v or v == "your_tushare_token_here":
            raise ValueError(
                "❌ TUSHARE_TOKEN 未设置！请在 .env 文件中配置真实的 Tushare token。"
            )
        return v

    def ensure_dirs(self) -> None:
        """确保运行时目录存在"""
        for d in (self.data_dir, self.exports_dir, self.logs_dir):
            d.mkdir(parents=True, exist_ok=True)

    @property
    def ai_available(self) -> bool:
        return bool(self.openrouter_api_key and self.openrouter_api_key != "sk-or-your_key_here")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    s = Settings()  # type: ignore[call-arg]
    s.ensure_dirs()
    return s


# 模块级别快捷访问
settings = get_settings()
