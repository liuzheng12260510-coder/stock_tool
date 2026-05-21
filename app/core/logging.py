"""
结构化日志配置 — structlog + 彩色控制台 + 文件轮转
"""
from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path

import structlog


def configure_logging(log_dir: Path, debug: bool = False) -> None:
    """初始化日志系统，应在应用启动时调用一次"""
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "stocksentry.log"

    level = logging.DEBUG if debug else logging.INFO

    # ── stdlib handler ──────────────────────────────────────────────────
    handlers: list[logging.Handler] = [
        logging.StreamHandler(sys.stdout),
        logging.handlers.RotatingFileHandler(
            log_file,
            maxBytes=10 * 1024 * 1024,  # 10 MB
            backupCount=5,
            encoding="utf-8",
        ),
    ]

    logging.basicConfig(
        format="%(message)s",
        level=level,
        handlers=handlers,
    )

    # ── structlog 配置 ───────────────────────────────────────────────────
    shared_processors: list = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=False),
        structlog.processors.StackInfoRenderer(),
    ]

    structlog.configure(
        processors=shared_processors
        + [
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        processor=structlog.dev.ConsoleRenderer(colors=True),
        foreign_pre_chain=shared_processors,
    )

    for h in handlers:
        h.setFormatter(formatter)
        h.setLevel(level)


def get_logger(name: str = "stocksentry") -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)
