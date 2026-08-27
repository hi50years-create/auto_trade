"""
전역 설정 로더. .env 파일을 읽어 검증된 설정값을 노출한다.
모든 모듈은 os.environ 을 직접 읽지 말고 이 모듈의 CONFIG 를 통해서만 값을 참조한다.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import time as dtime
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = PROJECT_ROOT / ".env"

if ENV_PATH.exists():
    load_dotenv(ENV_PATH)
else:
    # 배포 환경에서는 systemd EnvironmentFile 이나 OS 환경변수로 주입될 수 있으므로
    # .env 파일이 없다고 즉시 죽이지는 않는다. 다만 필수값 검증에서 걸러진다.
    load_dotenv()


def _get_str(key: str, default: str | None = None, required: bool = False) -> str:
    val = os.environ.get(key, default)
    if required and (val is None or val == ""):
        raise RuntimeError(f"[config] 필수 환경변수 누락: {key} (.env 파일 확인)")
    return val


def _get_float(key: str, default: float) -> float:
    val = os.environ.get(key)
    return float(val) if val not in (None, "") else default


def _get_int(key: str, default: int) -> int:
    val = os.environ.get(key)
    return int(val) if val not in (None, "") else default


def _parse_hhmmss(val: str) -> dtime:
    h, m, s = (int(x) for x in val.split(":"))
    return dtime(h, m, s)


@dataclass(frozen=True)
class Config:
    # 운영 모드
    trading_mode: str = field(default_factory=lambda: _get_str("TRADING_MODE", "paper"))

    # KIS
    kis_app_key: str = field(default_factory=lambda: _get_str("KIS_APP_KEY", required=True))
    kis_app_secret: str = field(default_factory=lambda: _get_str("KIS_APP_SECRET", required=True))
    kis_cano: str = field(default_factory=lambda: _get_str("KIS_CANO", required=True))
    kis_acnt_prdt_cd: str = field(default_factory=lambda: _get_str("KIS_ACNT_PRDT_CD", "01"))

    # Naver
    naver_client_id: str = field(default_factory=lambda: _get_str("NAVER_CLIENT_ID", ""))
    naver_client_secret: str = field(default_factory=lambda: _get_str("NAVER_CLIENT_SECRET", ""))

    # Gemini
    gemini_api_key: str = field(default_factory=lambda: _get_str("GEMINI_API_KEY", ""))
    gemini_model: str = field(default_factory=lambda: _get_str("GEMINI_MODEL", "gemini-3.6-flash"))
    gemini_throttle_seconds: float = field(default_factory=lambda: _get_float("GEMINI_THROTTLE_SECONDS", 4.5))

    # Telegram
    # telegram_chat_id: 알림이 발송되는 그룹(또는 개인) 채팅방. 필수.
    # telegram_user_chat_id: 개인 DM chat_id. 비워두면 그룹 채팅에서의 명령만 허용된다.
    telegram_bot_token: str = field(default_factory=lambda: _get_str("TELEGRAM_BOT_TOKEN", required=True))
    telegram_chat_id: str = field(default_factory=lambda: _get_str("TELEGRAM_CHAT_ID", required=True))
    telegram_user_chat_id: str = field(default_factory=lambda: _get_str("TELEGRAM_USER_CHAT_ID", ""))

    @property
    def telegram_allowed_chat_ids(self) -> tuple[int, ...]:
        ids = {int(self.telegram_chat_id)}
        if self.telegram_user_chat_id:
            ids.add(int(self.telegram_user_chat_id))
        return tuple(ids)

    # 리스크 관리
    max_slots: int = field(default_factory=lambda: _get_int("MAX_SLOTS", 3))
    slot_allocation_pct: float = field(default_factory=lambda: _get_float("SLOT_ALLOCATION_PCT", 0.30))
    target_profit_pct: float = field(default_factory=lambda: _get_float("TARGET_PROFIT_PCT", 0.05))
    stop_loss_pct: float = field(default_factory=lambda: _get_float("STOP_LOSS_PCT", -0.03))
    slippage_pct: float = field(default_factory=lambda: _get_float("SLIPPAGE_PCT", 0.002))
    index_crash_threshold_pct: float = field(default_factory=lambda: _get_float("INDEX_CRASH_THRESHOLD_PCT", -1.2))
    gap_up_max_pct: float = field(default_factory=lambda: _get_float("GAP_UP_MAX_PCT", 5.0))
    min_prev_trade_amount: float = field(default_factory=lambda: _get_float("MIN_PREV_TRADE_AMOUNT", 25_000_000_000))

    # 시간 필터
    entry_window_start: dtime = field(default_factory=lambda: _parse_hhmmss(_get_str("ENTRY_WINDOW_START", "09:00:00")))
    entry_window_end: dtime = field(default_factory=lambda: _parse_hhmmss(_get_str("ENTRY_WINDOW_END", "09:30:00")))
    market_close_time: dtime = field(default_factory=lambda: _parse_hhmmss(_get_str("MARKET_CLOSE_TIME", "15:30:00")))
    pre_screen_time: dtime = field(default_factory=lambda: _parse_hhmmss(_get_str("PRE_SCREEN_TIME", "08:50:00")))
    # 장마감 직후 스냅샷 계산 시각. 15:30 종가 확정에 약간의 지연 버퍼(5분)를 둔다.
    eod_snapshot_time: dtime = field(default_factory=lambda: _parse_hhmmss(_get_str("EOD_SNAPSHOT_TIME", "15:35:00")))

    # DB / 로그
    db_path: str = field(default_factory=lambda: _get_str("DB_PATH", "data/trading_bot.db"))
    log_level: str = field(default_factory=lambda: _get_str("LOG_LEVEL", "INFO"))
    log_path: str = field(default_factory=lambda: _get_str("LOG_PATH", "logs/trading_bot.log"))
    log_retention_days: int = field(default_factory=lambda: _get_int("LOG_RETENTION_DAYS", 7))

    # 웹 대시보드 (텔레그램 일회용 코드 로그인)
    web_enabled: bool = field(default_factory=lambda: _get_str("WEB_ENABLED", "true").lower() == "true")
    web_host: str = field(default_factory=lambda: _get_str("WEB_HOST", "127.0.0.1"))
    web_port: int = field(default_factory=lambda: _get_int("WEB_PORT", 8080))
    # 세션 쿠키 서명용 비밀키. 비워두면 프로세스 시작 시 임의 생성(재시작하면 로그인 풀림).
    # VM 등에서 재시작해도 세션을 유지하고 싶으면 고정값을 넣어주세요 (openssl rand -hex 32 로 생성 권장).
    web_secret_key: str = field(default_factory=lambda: _get_str("WEB_SECRET_KEY", ""))

    def __post_init__(self):
        if self.trading_mode not in ("paper", "live"):
            raise RuntimeError("[config] TRADING_MODE 은 'paper' 또는 'live' 만 허용됩니다.")
        if self.max_slots * self.slot_allocation_pct > 1.0001:
            raise RuntimeError(
                "[config] MAX_SLOTS * SLOT_ALLOCATION_PCT 가 100% 를 초과합니다. "
                f"(slots={self.max_slots}, pct={self.slot_allocation_pct})"
            )

    @property
    def is_live(self) -> bool:
        return self.trading_mode == "live"


CONFIG = Config()
