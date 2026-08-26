"""공용 로거. 콘솔 + 파일(자정 기준 일자별 순환, 기본 7일 보관) 동시 출력."""
from __future__ import annotations

import logging
import sys
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

from src.config import CONFIG, PROJECT_ROOT

_LOGGERS: dict[str, logging.Logger] = {}


def get_logger(name: str) -> logging.Logger:
    if name in _LOGGERS:
        return _LOGGERS[name]

    logger = logging.getLogger(name)
    logger.setLevel(CONFIG.log_level.upper())
    logger.propagate = False

    if not logger.handlers:
        fmt = logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
        )

        console = logging.StreamHandler(sys.stdout)
        console.setFormatter(fmt)
        logger.addHandler(console)

        log_path = PROJECT_ROOT / CONFIG.log_path
        log_path.parent.mkdir(parents=True, exist_ok=True)
        # 매일 자정(시스템 로컬시각, 통상 KST)에 회전하며 trading_bot.log.YYYY-MM-DD 형태로 보관.
        # backupCount=7 이면 당일 파일 + 지난 7일치 = 최근 8일 로그가 남고 그 이전은 자동 삭제된다.
        file_handler = TimedRotatingFileHandler(
            log_path, when="midnight", backupCount=CONFIG.log_retention_days, encoding="utf-8"
        )
        file_handler.suffix = "%Y-%m-%d"
        file_handler.setFormatter(fmt)
        logger.addHandler(file_handler)

    _LOGGERS[name] = logger
    return logger
