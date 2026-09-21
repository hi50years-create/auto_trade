"""v10 스펙의 '시가 돌파' 전략 + 2026-09-16에 추가한 '저점 반등' 보조 경로.
state_machine.py 에 있던 판정 로직을 그대로 옮긴 것으로, 동작은 이식 전과 동일해야 한다.
"""
from __future__ import annotations

import asyncio

from src.config import CONFIG
from src.strategy.plugin_base import EntrySignal, ExitSignal, Strategy


class BreakoutPullbackStrategy(Strategy):
    requires_dip_below_open = True

    def __init__(self):
        # 저점 반등(눌림목) 매수용 상태. 감시 시작(WAIT_FOR_BREAKOUT 진입) 이후의 장중 저점
        # 기준으로 추적한다 - 그 이전 저가는 대상이 아니다.
        self._running_low: float | None = None
        self._pending_reversal: dict | None = None  # {"close", "low"} - 신저가+양봉 확정, 다음봉 확인 대기

    async def on_bar(self, broker, code: str, bar, day_open: float) -> EntrySignal | None:
        opn, cls, low, vol = (
            float(bar["open"]), float(bar["close"]), float(bar["low"]), float(bar["volume"])
        )

        # 저점 반등 후보는 시가 돌파 판정과 무관하게 매 봉 갱신해야 한다 (돌파가 먼저 나면 그쪽으로
        # 진입하고 반등 추적은 자연히 의미를 잃는다).
        pullback_signal = (
            self._update_pullback_state(low=low, opn=opn, cls=cls, vol=vol)
            if CONFIG.pullback_reversal_enabled else None
        )

        is_cross_above = (opn < day_open and cls >= day_open) or (
            opn >= day_open and cls >= day_open and low < day_open
        )
        is_bullish = cls > opn
        body_pct = (cls - opn) / opn if opn else 0
        is_volume_ok = vol >= 10_000

        if is_cross_above and is_bullish and body_pct >= CONFIG.breakout_body_min_pct and is_volume_ok:
            snap = await asyncio.to_thread(broker.get_realtime_snapshot, code)
            vol_power = snap.get("vol_power", 0.0)
            ask_bid_ratio = snap.get("ask_bid_ratio", 0.0)
            is_sd_ok = vol_power >= 100.0 and ask_bid_ratio >= 120.0
            if is_sd_ok:
                return EntrySignal(price=cls, reason="시가 돌파")

        if pullback_signal is not None:
            return EntrySignal(price=pullback_signal["price"], reason="저점 반등", structural_stop_price=pullback_signal["stop"])

        return None

    def _update_pullback_state(self, low: float, opn: float, cls: float, vol: float) -> dict | None:
        """시가 돌파 신호가 없는 날에도 장중 저점을 찍고 반등하는 종목을 잡기 위한 보조 경로.
        2026-09-16 실측: 전일 급등주가 익일 차익실현 매도로 시가를 못 뚫는 날, 10종목 중
        8종목이 장중 저점 대비 +1~14.5% 반등했다 (단, 그 저점/반등은 09:00~09:30 밖에서 발생).

        판정: (1) 장중 신저가를 찍은 캔들이 양봉으로 마감 -> 반등 후보로 대기,
              (2) 바로 다음 캔들이 신저가를 갱신하지 않으면서 양봉+후보 캔들 종가 상회+거래량
                  조건을 만족하면 반등 확정으로 진입 신호를 낸다.
        구조적 손절가는 반등 후보 캔들의 저가(=이번 반등의 근거가 된 저점)로 둔다 - 그 아래로
        다시 깨지면 반등 시나리오 자체가 무효화된 것으로 본다.
        """
        is_new_low = self._running_low is None or low <= self._running_low
        if is_new_low:
            self._running_low = low
            self._pending_reversal = {"close": cls, "low": low} if cls > opn else None
            return None

        pending = self._pending_reversal
        self._pending_reversal = None
        if pending is None:
            return None

        if cls > opn and cls > pending["close"] and vol >= CONFIG.pullback_min_volume:
            return {"price": cls, "stop": pending["low"]}
        return None

    def on_tick_holding(
        self, rt_price: float, buy_price: float, day_open: float, structural_stop_price: float | None
    ) -> ExitSignal | None:
        if structural_stop_price is not None:
            # 저점 반등 진입: 매수 근거였던 반등 저점이 다시 깨지면 시나리오 무효화로 간주하고 즉시 청산한다.
            # (시가 돌파 진입과 달리 매수가 자체가 시가보다 낮은 게 정상이므로 "시가 이탈" 규칙은 적용하지 않는다.)
            if rt_price <= structural_stop_price:
                return ExitSignal(price=structural_stop_price, reason="반등 저점 이탈 손절")
        elif rt_price < day_open:
            return ExitSignal(price=day_open, reason="실시간 시가 이탈 손절")
        return None
