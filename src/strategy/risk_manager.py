"""v10 실전 안전 필터: 슬롯 기반 자금관리, 종합주가지수 필터, 진입 제한 시간."""
from __future__ import annotations

import threading
from datetime import time

from src.config import CONFIG
from src.utils import time_utils
from src.utils.logger import get_logger

log = get_logger("risk_manager")


class SlotManager:
    """최대 max_slots 개 동시 보유 제한 + 종목당 slot_allocation_pct 비율 배분."""

    def __init__(self, max_slots: int = None, allocation_pct: float = None):
        self.max_slots = max_slots or CONFIG.max_slots
        self.allocation_pct = allocation_pct or CONFIG.slot_allocation_pct
        self._slots: dict[str, dict] = {}
        self._lock = threading.Lock()

    @property
    def active_slots_count(self) -> int:
        with self._lock:
            return len(self._slots)

    def has_room(self) -> bool:
        with self._lock:
            return len(self._slots) < self.max_slots

    def acquire(self, code: str, name: str, buy_price: float, qty: int) -> bool:
        with self._lock:
            if len(self._slots) >= self.max_slots:
                return False
            self._slots[code] = {"name": name, "buy_price": buy_price, "qty": qty}
            return True

    def release(self, code: str):
        with self._lock:
            self._slots.pop(code, None)

    def snapshot(self) -> dict[str, dict]:
        with self._lock:
            return dict(self._slots)

    def allocation_amount(self, total_cash: float) -> float:
        """1슬롯당 투자 가능 금액 (총 가용자금 * 슬롯 배분 비율)."""
        return total_cash * self.allocation_pct


def check_market_index(kospi_change_pct: float, kosdaq_change_pct: float) -> tuple[bool, str]:
    """코스피/코스닥 중 하나라도 임계치(기본 -1.2%) 이하로 폭락 시 당일 거래 전면 중단."""
    threshold = CONFIG.index_crash_threshold_pct
    if kospi_change_pct <= threshold or kosdaq_change_pct <= threshold:
        return False, f"종합주가지수 폭락 감지 (KOSPI: {kospi_change_pct:.2f}%, KOSDAQ: {kosdaq_change_pct:.2f}%, 기준: {threshold}%)"
    return True, "지수 정상 범위"


def check_gap_up(day_open_price: float, prev_close_price: float) -> tuple[bool, float]:
    """시가 갭 상승률이 GAP_UP_MAX_PCT 이하인지 검증. 반환: (통과여부, 갭비율)"""
    gap_pct = ((day_open_price - prev_close_price) / prev_close_price) * 100
    return gap_pct <= CONFIG.gap_up_max_pct, gap_pct


def is_entry_allowed_now() -> bool:
    """신규 매수 진입 허용 시간(09:00:00~09:30:00)인지 여부."""
    return time_utils.is_within_entry_window()


def is_exit_monitoring_active() -> bool:
    """청산 감시는 장마감(15:30)까지 계속되어야 한다."""
    return time_utils.is_before_market_close()
