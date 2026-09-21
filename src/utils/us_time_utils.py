"""미국 시장(정규장) 시간 판정 유틸. src.utils.time_utils 와 동일한 함수 시그니처로 맞춰서
StockWatcher/TradingEngine 이 국내/미국 아무 모듈이나 주입받아 써도 동작하게 한다.

서머타임(EDT/EST)은 수동 토글 없이 zoneinfo(America/New_York)로 자동 반영된다 - 3월/11월
전환일마다 값을 고쳐줄 필요가 없다. main.py 의 스케줄러도 APScheduler CronTrigger에
timezone="America/New_York" 을 직접 넘겨 같은 방식으로 자동 반영한다.

v1 범위: 정규장(09:30~16:00 ET)만 다룬다. 프리/애프터마켓, 미국 공휴일 자동 판별은
포함하지 않았다 (국내 time_utils.py 도 공휴일 자동판별이 없는 것과 동일한 수준).
"""
from __future__ import annotations

from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

NY_TZ = ZoneInfo("America/New_York")

MARKET_OPEN_ET = dtime(9, 30, 0)
MARKET_CLOSE_ET = dtime(16, 0, 0)
# 진입창: 개장 후 90분 (국내의 "09:00~10:30, 개장 후 1.5시간" 관행과 동일한 폭으로 맞춤).
ENTRY_WINDOW_END_ET = dtime(11, 0, 0)

ENTRY_WINDOW_LABEL = "09:30~11:00 (America/New_York)"


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
