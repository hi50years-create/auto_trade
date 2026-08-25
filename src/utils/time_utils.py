"""장중 시간 판정 유틸. 모든 시각은 한국 표준시(KST, 시스템 로컬 타임존이 KST라고 가정)로 처리한다.
서버가 UTC 로 도는 경우(예: Oracle VM 기본 설치) systemd 유닛 또는 OS 타임존을 Asia/Seoul 로 맞춰야 한다.
"""
from __future__ import annotations

from datetime import date, datetime, time

from src.config import CONFIG

KOREA_HOLIDAYS_NOTE = (
    "공휴일/임시휴장일 자동 판별은 포함되어 있지 않습니다. "
    "필요 시 한국거래소(KRX) 개장일 API 또는 별도 캘린더 파일을 연동하세요."
)


def now() -> datetime:
    return datetime.now()


def now_time() -> time:
    return now().time()


def is_weekday(d: date | None = None) -> bool:
    d = d or date.today()
    return d.weekday() < 5  # 0=Mon ... 4=Fri


def is_within_entry_window(t: time | None = None) -> bool:
    """신규 매수 진입 허용 시간(기본 09:00:00~09:30:00)인지 여부."""
    t = t or now_time()
    return CONFIG.entry_window_start <= t <= CONFIG.entry_window_end


def is_before_market_close(t: time | None = None) -> bool:
    """청산(익절/손절) 감시가 유지되어야 하는 시간(장마감 전)인지 여부."""
    t = t or now_time()
    return t < CONFIG.market_close_time


def is_market_close_reached(t: time | None = None) -> bool:
    t = t or now_time()
    return t >= CONFIG.market_close_time


def is_pre_screen_time(t: time | None = None) -> bool:
    t = t or now_time()
    return t >= CONFIG.pre_screen_time
