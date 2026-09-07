"""공용 로거. 콘솔 + 파일(자정 기준 일자별 순환, 기본 7일 보관) 동시 출력.

파일/콘솔 핸들러는 루트 로거에 딱 한 번만 붙이고, 이름별 로거(main/kis_client/state_machine/...)는
자체 핸들러 없이 propagate=True로 루트까지 올려보낸다. 예전엔 로거 이름마다 같은 경로를 가리키는
별도의 TimedRotatingFileHandler 인스턴스를 만들었는데, 자정 롤오버 시 여러 핸들러가 동시에 같은
파일을 rename하려고 경합해 대부분의 로거가 롤오버 이후 사라진(rename된) 파일 핸들에 계속 쓰게
되는 문제가 실측 확인됐다 (2026-09-07: main 로거만 살아남고 kis_client/screener/state_machine
로거는 자정 이후 trading_bot.log에 전혀 안 남고 콘솔에만 찍힘). 핸들러를 하나만 두면 이 경합
자체가 발생하지 않는다.
"""
from __future__ import annotations

import logging
import sys
from logging.handlers import TimedRotatingFileHandler

from src.config import CONFIG, PROJECT_ROOT

_root_configured = False


def _configure_root() -> None:
    global _root_configured
    if _root_configured:
        return

    root = logging.getLogger()
    root.setLevel(CONFIG.log_level.upper())

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    root.addHandler(console)

    log_path = PROJECT_ROOT / CONFIG.log_path
    log_path.parent.mkdir(parents=True, exist_ok=True)
    # 매일 자정(시스템 로컬시각, 통상 KST)에 회전하며 trading_bot.log.YYYY-MM-DD 형태로 보관.
    # backupCount=7 이면 당일 파일 + 지난 7일치 = 최근 8일 로그가 남고 그 이전은 자동 삭제된다.
    file_handler = TimedRotatingFileHandler(
        log_path, when="midnight", backupCount=CONFIG.log_retention_days, encoding="utf-8"
    )
    file_handler.suffix = "%Y-%m-%d"
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)

    _root_configured = True


def get_logger(name: str) -> logging.Logger:
    _configure_root()
    logger = logging.getLogger(name)
    logger.setLevel(CONFIG.log_level.upper())
    return logger
