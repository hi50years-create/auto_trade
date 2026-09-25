"""미국 시장(정규장) 시간 판정 유틸. src.utils.time_utils 와 동일한 함수 시그니처로 맞춰서
StockWatcher/TradingEngine 이 국내/미국 아무 모듈이나 주입받아 써도 동작하게 한다.

서머타임(EDT/EST)은 수동 토글 없이 zoneinfo(America/New_York)로 자동 반영된다 - 3월/11월
전환일마다 값을 고쳐줄 필요가 없다. main.py 의 스케줄러도 APScheduler CronTrigger에
timezone="America/New_York" 을 직접 넘겨 같은 방식으로 자동 반영한다.

v1 범위: 정규장(09:30~16:00 ET)만 다룬다. 프리/애프터마켓, 미국 공휴일 자동 판별은
포함하지 않았다 (국내 time_utils.py 도 공휴일 자동판별이 없는 것과 동일한 수준).
"""
from __future__ import annotations

from datetime import date, datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

from src.config import PROJECT_ROOT

NY_TZ = ZoneInfo("America/New_York")

MARKET_OPEN_ET = dtime(9, 30, 0)
MARKET_CLOSE_ET = dtime(16, 0, 0)
# 진입창: 개장 후 90분 (국내의 "09:00~10:30, 개장 후 1.5시간" 관행과 동일한 폭으로 맞춤).
ENTRY_WINDOW_END_ET = dtime(11, 0, 0)

ENTRY_WINDOW_LABEL = "09:30~11:00 (America/New_York)"

# 2026-09-25 실측: 장마감 "도달 그 순간" 매도를 넣었더니 몇 초 차이로 KIS가 이미 장종료
# 처리를 해버려 주문이 계속 거절됐다(40580000, "모의투자 장종료 입니다") - 그런데도 성공
# 여부를 확인 안 해서 실제로는 하나도 안 팔린 포지션들이 "청산 완료"로 잘못 기록/알림됐다.
# 마감 몇 분 전부터 미리 시도해 진짜 체결될 시간을 준다.
CLOSING_LIQUIDATION_BUFFER_MIN = 3

_HOLIDAYS_PATH = PROJECT_ROOT / "config" / "holidays_us.txt"
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
    # 미국 세션은 KST 자정을 걸쳐 열리므로, 기준일은 "그 세션이 속하는 뉴욕 날짜"로 판단한다
    # (뉴욕 현재시각 기준 date - 개장 전 KST 낮에 확인할 때도 오늘밤 열릴 세션의 뉴욕 날짜와 맞음).
    d = d or now().date()
    return d.isoformat() in _load_holidays()


def is_trading_day(d: date | None = None) -> bool:
    d = d or now().date()
    return is_weekday(d) and not is_market_holiday(d)


def now() -> datetime:
    return datetime.now(NY_TZ)


def now_time() -> dtime:
    return now().time()


def is_weekday(d=None) -> bool:
    d = d or now().date()
    return d.weekday() < 5


def is_within_entry_window(t: dtime | None = None) -> bool:
    t = t or now_time()
    return MARKET_OPEN_ET <= t <= ENTRY_WINDOW_END_ET


def is_before_market_close(t: dtime | None = None) -> bool:
    t = t or now_time()
    return t < MARKET_CLOSE_ET


def is_market_close_reached(t: dtime | None = None) -> bool:
    t = t or now_time()
    return t >= MARKET_CLOSE_ET


def is_closing_liquidation_time(t: dtime | None = None) -> bool:
    """장마감 동시청산을 "시도해야 하는" 시간대(마감 몇 분 전 ~ 마감 이후 전부)."""
    t = t or now_time()
    close_dt = datetime.combine(date.today(), MARKET_CLOSE_ET)
    buffer_start = (close_dt - timedelta(minutes=CLOSING_LIQUIDATION_BUFFER_MIN)).time()
    return t >= buffer_start
