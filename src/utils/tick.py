"""KRX 호가단위(tick size) 계산. 코스피/코스닥 공통 일반 규정 기준(2025년 기준 통상 구간).
※ 거래소가 호가단위 구간을 개정하는 경우가 있으므로 실거래 전 최신 규정을 재확인할 것.
"""
from __future__ import annotations

_TICK_TABLE = [
    (2_000, 1),
    (5_000, 5),
    (20_000, 10),
    (50_000, 50),
    (200_000, 100),
    (500_000, 500),
    (float("inf"), 1_000),
]


def get_tick_size(price: float) -> int:
    for upper, tick in _TICK_TABLE:
        if price < upper:
            return tick
    return 1_000


def round_up_ticks(price: float, n_ticks: int = 2) -> int:
    """슬리피지 방지를 위해 기준가 대비 n_ticks 만큼 높은 지정가를 계산한다 (3.6.1 지정가 초과 주문 규칙)."""
    p = float(price)
    for _ in range(n_ticks):
        tick = get_tick_size(p)
        p = p + tick
    # 최종가를 해당 구간 tick 배수로 정렬
    tick = get_tick_size(p)
    return int(round(p / tick) * tick)
