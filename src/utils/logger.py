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


class _SecretRedactFilter(logging.Filter):
    """python-telegram-bot 내부 httpx 클라이언트가 'HTTP Request: POST https://api.telegram.org/bot<TOKEN>/...'
    형태로 URL에 토큰이 박힌 요청 로그를 INFO 레벨로 남기는 것이 실측 확인됐다 (2026-09-07, 루트 로거에
    핸들러를 붙이면서 이 로그도 함께 파일/콘솔에 찍히기 시작함). httpx 로거는 별도로 WARNING 이상만
    남기도록 낮춰뒀지만, 다른 라이브러리가 비슷하게 URL/쿼리에 비밀값을 남길 가능성에 대비해 알려진
    비밀값 문자열을 로그에서 무조건 마스킹하는 필터를 루트 로거에 추가로 걸어둔다 (다중 방어)."""

    def __init__(self, secrets: list[str]):
        super().__init__()
        self._secrets = [s for s in secrets if s]

    def filter(self, record: logging.LogRecord) -> bool:
        if not self._secrets:
            return True
        try:
            msg = record.getMessage()
        except Exception:
            return True
        redacted = msg
        for s in self._secrets:
            redacted = redacted.replace(s, "***REDACTED***")
        if redacted != msg:
            record.msg = redacted
            record.args = ()
        return True


def _configure_root() -> None:
    global _root_configured
    if _root_configured:
        return

    root = logging.getLogger()
    root.setLevel(CONFIG.log_level.upper())

    # 주의: 이 필터는 root Logger가 아니라 각 핸들러에 걸어야 한다. Logger.addFilter()는 그 로거
    # 자신에게 직접 로그를 남길 때만 적용되고, 자식 로거(main/kis_client/...)가 올려보낸(propagate)
    # 레코드는 callHandlers()가 핸들러를 바로 호출하므로 root의 Logger 필터를 건너뛴다 - 처음에
    # root.addFilter()로 걸었다가 실제로는 전혀 마스킹이 안 되는 걸 실측으로 확인하고 고쳤다.
    redact_filter = _SecretRedactFilter([
        CONFIG.telegram_bot_token,
        CONFIG.kis_app_key,
        CONFIG.kis_app_secret,
        CONFIG.naver_client_secret,
        CONFIG.gemini_api_key,
        CONFIG.web_secret_key,
    ])

    # httpx(python-telegram-bot 내부 HTTP 클라이언트)는 요청 URL을 통째로 INFO 로그에 남기는데,
    # 텔레그램 Bot API는 URL 자체에 봇 토큰이 박혀 있는 방식(.../bot<TOKEN>/...)이라 그대로 두면
    # 매 요청마다 토큰이 로그에 평문으로 남는다. 요청 자체는 굳이 안 남아도 되므로 WARNING으로 낮춘다.
    logging.getLogger("httpx").setLevel(logging.WARNING)

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    console.addFilter(redact_filter)
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
    file_handler.addFilter(redact_filter)
    root.addHandler(file_handler)

    _root_configured = True


def get_logger(name: str) -> logging.Logger:
    _configure_root()
    logger = logging.getLogger(name)
    logger.setLevel(CONFIG.log_level.upper())
    return logger
