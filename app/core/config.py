"""
配置中心 — pydantic-settings 读取 .env，提供全局单例 settings

v1: 初始版本
v2 (P0): 黑名单/次新股/熔断器/fina_loop_rescue 配置
v3 (P1): 杜邦/盈余质量/分红连续性 筛选阈值
v4 (P4): T1~T4 全量改进：市值下限/价格下限/异动剔除/停牌剔除/行业剔除
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

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
    http_proxy: str | None = None
    https_proxy: str | None = None

    # ── 调度 ────────────────────────────────────────────────────────────
    scheduler_enabled: bool = True
    scheduler_cron_hour: int = 15
    scheduler_cron_minute: int = 30

    # ── 筛选阈值 ────────────────────────────────────────────────────────
    screen_pe_ttm_max: float = 30.0
    screen_pe_deduct_max: float = 20.0
    screen_dv_ttm_min: float = 2.0
    screen_total_mv_max_yi: float = 100.0   # 亿元，0 = 不限（上限）
    screen_exclude_bj: bool = True           # 排除北交所
    screen_exclude_soe: bool = True          # 排除国企/央企
    screen_min_composite_score: float = 60.0

    # ── 预筛阈值（拉财务数据前的速度优化粗筛）────────────────────────────
    prescreen_pe_ttm_max: float = 20.0       # PE_TTM 超过此值直接跳过，0 = 不限
    prescreen_total_mv_max_yi: float = 100.0 # 市值（亿）超过此值直接跳过，0 = 不限

    # ── 黑名单 & 次新股 ──────────────────────────────────────────────────
    screen_min_list_years: float = 1.0

    # ── T4 新增：硬过滤强化（市值下限/价格下限/异动/停牌/行业 nan）──────────
    # 总市值下限（亿元），0 = 不限；剔除微市值壳股（如退市风险股）
    screen_min_total_mv_yi: float = 30.0
    # 股价下限（元/股），0 = 不限；剔除仙股
    screen_min_close_price: float = 2.0
    # 当日 |涨跌幅%| 上限，0 = 不限；涨跌停股在该截面价格失真，排除
    screen_max_abs_pct_chg: float = 7.0
    # 行业字段为空（nan/None/""）时直接剔除
    screen_exclude_industry_nan: bool = True
    # 当日无成交额（上报为 0 或 None）视为停牌，临时排除
    screen_exclude_no_volume: bool = True

    # ── 熔断器（Circuit Breaker）配置 ───────────────────────────────────
    breaker_failure_threshold: int = 5
    breaker_cooldown_seconds: int = 300

    # ── 安全阀：是否启用逐只兜底拉取 fina_indicator ─────────────────────
    enable_fina_loop_rescue: bool = False

    # ── P1 新增：杜邦高杠杆排雷 ─────────────────────────────────────────
    screen_max_eqt_multiplier: float = 3.0

    # ── P1 新增：盈余质量阈值 ─────────────────────────────────────────────
    screen_min_cfo_to_np: float = 0.5

    # ── P1 新增：分红连续性阈值 ──────────────────────────────────────────
    screen_min_div_continuity_years: int = 3

    # 是否剔除"清仓式分红"标的（单年派现占近5年总额>70%）
    screen_reject_clearance_div: bool = True

    # ── 数据库 ──────────────────────────────────────────────────────────
    database_url: str = "sqlite:///./data/stocksentry.db"

    # ── Web ─────────────────────────────────────────────────────────────
    web_host: str = "127.0.0.1"
    web_port: int = 8000
    web_debug: bool = False

    # ── Tushare 限速 ────────────────────────────────────────────────────
    tushare_rate_limit: int = 180  # req/min
    tushare_burst_capacity: int = 0

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
