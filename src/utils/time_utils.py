"""장중 시간 판정 유틸. 모든 시각은 한국 표준시(KST, 시스템 로컬 타임존이 KST라고 가정)로 처리한다.
서버가 UTC 로 도는 경우(예: Oracle VM 기본 설치) systemd 유닛 또는 OS 타임존을 Asia/Seoul 로 맞춰야 한다.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta

from src.config import CONFIG, PROJECT_ROOT

# 2026-09-25 실측: "장마감 도달 그 순간" 매도를 넣으면 몇 초 차이로 KIS가 이미 "장종료" 처리를
# 해버려 주문이 거절되는 게 실측 확인됐다(미국 계좌 실측이지만 동일 코드 경로를 쓰는 국내도
# 같은 위험이 있음). 마감 몇 분 전부터 미리 시도해 진짜 체결될 시간을 준다.
CLOSING_LIQUIDATION_BUFFER_MIN = 3

# 2026-09-25 실측: KIS 휴장일조회 API(chk-holiday)는 모의투자 TR을 지원하지 않는다
# (msg_cd=EGW02006 "모의투자 TR 이 아닙니다") - 파일 기반 목록으로 대체한다.
_HOLIDAYS_PATH = PROJECT_ROOT / "config" / "holidays_kr.txt"
_holidays_cache: set[str] | None = None


def _load_holidays() -> set[str]:
    global _holidays_cache
    if _holidays_cache is not None:
        return _holidays_cache
    if not _HOLIDAYS_PATH.exists():
        _holidays_cache = set()
        return _holidays_cache
    _holidays_cache = {
        line.strip() for line in _HOLIDAYS_PATH.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    }
    return _holidays_cache


def is_market_holiday(d: date | None = None) -> bool:
    d = d or date.today()
    return d.isoformat() in _load_holidays()


def is_trading_day(d: date | None = None) -> bool:
    d = d or date.today()
    return is_weekday(d) and not is_market_holiday(d)

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
    """신규 매수 진입 허용 시간(ENTRY_WINDOW_START~ENTRY_WINDOW_END, 기본 09:00~10:30)인지 여부."""
    t = t or now_time()
    return CONFIG.entry_window_start <= t <= CONFIG.entry_window_end


def is_before_market_close(t: time | None = None) -> bool:
    """청산(익절/손절) 감시가 유지되어야 하는 시간(장마감 전)인지 여부."""
    t = t or now_time()
    return t < CONFIG.market_close_time


def is_market_close_reached(t: time | None = None) -> bool:
    t = t or now_time()
    return t >= CONFIG.market_close_time


def is_closing_liquidation_time(t: time | None = None) -> bool:
    """장마감 동시청산을 "시도해야 하는" 시간대(마감 몇 분 전 ~ 마감 이후 전부)."""
    t = t or now_time()
    close_dt = datetime.combine(date.today(), CONFIG.market_close_time)
    buffer_start = (close_dt - timedelta(minutes=CLOSING_LIQUIDATION_BUFFER_MIN)).time()
    return t >= buffer_start


def is_pre_screen_time(t: time | None = None) -> bool:
    t = t or now_time()
    return t >= CONFIG.pre_screen_time
