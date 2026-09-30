"""이동평균 골든크로스 매수 전략. 체결강도/호가잔량비 같은 국내 전용 실시간 수급 데이터 없이도
동작하도록 설계했다 - 미국주식 등 그런 데이터를 못 받는 시장의 기본 전략으로 쓴다.

진입: 단기이동평균이 장기이동평균을 아래에서 위로 교차(골든크로스)하는 봉의 종가에 매수.
청산: 공통 목표익절/고정손절/장마감청산에 더해 데드크로스(단기선이 장기선 아래로 역교차)도
청산 조건으로 본다. 2026-09-30 실측: 미국 대형주는 하루 변동폭이 ±2% 안팎이라 국내용으로
잡은 목표익절(+5%)/고정손절(-3%)에 하루 종일 안 닿고 매번 장마감 강제청산으로만 끝났다 -
추세추종 전략답게 "추세가 꺾이면 나온다"는 조건을 추가한다.
"""
from __future__ import annotations

from src.strategy.plugin_base import EntrySignal, ExitSignal, Strategy


class GoldenCrossStrategy(Strategy):
    requires_dip_below_open = False
    needs_bar_based_exit = True

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
        signal = self._update_ma(cls)

        if signal == "golden" and vol >= self.min_volume:
            return EntrySignal(price=cls, reason="골든크로스")
        return None

    def on_bar_holding(self, bar) -> ExitSignal | None:
        cls = float(bar["close"])
        signal = self._update_ma(cls)
        if signal == "dead":
            return ExitSignal(price=cls, reason="데드크로스")
        return None

    def _update_ma(self, cls: float) -> str | None:
        """종가를 누적하고 이동평균을 갱신한 뒤, 이번 봉에서 교차가 발생했으면
        "golden"/"dead"를, 아니면 None을 반환한다. 진입(on_bar)과 보유중(on_bar_holding)이
        같은 self._closes/이동평균 상태를 그대로 이어받아 쓴다 - 포지션을 잡았다고 이동평균
        히스토리가 끊기면 안 되기 때문."""
        self._closes.append(cls)
        if len(self._closes) < self.long_window:
            return None

        short_ma = sum(self._closes[-self.short_window:]) / self.short_window
        long_ma = sum(self._closes[-self.long_window:]) / self.long_window

        signal = None
        if self._prev_short_ma is not None and self._prev_long_ma is not None:
            if self._prev_short_ma <= self._prev_long_ma and short_ma > long_ma:
                signal = "golden"
            elif self._prev_short_ma >= self._prev_long_ma and short_ma < long_ma:
                signal = "dead"

        self._prev_short_ma, self._prev_long_ma = short_ma, long_ma
        return signal
