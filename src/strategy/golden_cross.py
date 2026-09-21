"""이동평균 골든크로스 매수 전략. 체결강도/호가잔량비 같은 국내 전용 실시간 수급 데이터 없이도
동작하도록 설계했다 - 미국주식 등 그런 데이터를 못 받는 시장의 기본 전략으로 쓴다.

판정: 단기이동평균이 장기이동평균을 아래에서 위로 교차(골든크로스)하는 봉의 종가에 매수.
청산은 전략 고유 조건 없이 공통 목표익절/고정손절/장마감청산에 맡긴다 (v1 단순화 - 데드크로스
청산 등을 추가하고 싶으면 on_tick_holding 을 오버라이드하되, 그러려면 보유 중에도 봉 데이터를
계속 공급해줄 별도 배관이 필요하다).
"""
from __future__ import annotations

from src.strategy.plugin_base import EntrySignal, Strategy


class GoldenCrossStrategy(Strategy):
    requires_dip_below_open = False

    def __init__(self, short_window: int = 5, long_window: int = 20, min_volume: float = 0):
        if short_window >= long_window:
            raise ValueError("short_window must be smaller than long_window")
        self.short_window = short_window
        self.long_window = long_window
        self.min_volume = min_volume
        self._closes: list[float] = []
        self._prev_short_ma: float | None = None
        self._prev_long_ma: float | None = None

    async def on_bar(self, broker, code: str, bar, day_open: float) -> EntrySignal | None:
        cls = float(bar["close"])
        vol = float(bar["volume"])
        self._closes.append(cls)

        if len(self._closes) < self.long_window:
            return None

        short_ma = sum(self._closes[-self.short_window:]) / self.short_window
        long_ma = sum(self._closes[-self.long_window:]) / self.long_window

        signal = None
        if (
            self._prev_short_ma is not None
            and self._prev_long_ma is not None
            and self._prev_short_ma <= self._prev_long_ma
            and short_ma > long_ma
            and vol >= self.min_volume
        ):
            signal = EntrySignal(price=cls, reason="골든크로스")

        self._prev_short_ma, self._prev_long_ma = short_ma, long_ma
        return signal
