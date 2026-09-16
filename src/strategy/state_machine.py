"""종목별 실시간 상태 머신. morning_breakout_backtest-v10.py 의 판정 로직을
실거래 폴링 루프(3분봉 완성 감지 + 실시간 틱 감시)로 이식한 버전이다.

백테스트 대비 개선점:
- 슬롯 부족으로 진입이 막힌 경우, 백테스트(데모)는 즉시 CLOSED 처리했지만
  요구사항 3.5.2("대기") 취지에 맞게 여기서는 해당 3분봉 신호만 스킵하고
  진입마감(ENTRY_WINDOW_END, 기본 10:30) 전까지 다음 3분봉에서 슬롯 여유를 재확인한다.
- 진입 제한 시간(ENTRY_WINDOW_END) 초과는 요구사항대로 영구 CLOSED 처리한다.
- 2026-09-16: 시가 돌파 신호가 아예 없는 날(전일 급등주 차익실현 매도일)을 대비해 저점
  반등(눌림목) 매수 경로를 추가했다. _update_pullback_state 참고.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, time

from src.broker.base import BrokerBase
from src.config import CONFIG
from src.db import database
from src.notify import telegram_bot
from src.strategy.risk_manager import SlotManager, is_entry_allowed_now, is_exit_monitoring_active
from src.utils import time_utils
from src.utils.logger import get_logger
from src.utils.tick import round_up_ticks

log = get_logger("state_machine")

POLL_INTERVAL_SEC = 3           # 실시간 틱(익절/손절/시가재이탈) 감시 주기
BAR_POLL_INTERVAL_SEC = 15      # 3분봉 갱신 확인 주기
ORDER_FILL_TIMEOUT_SEC = 15     # 미체결 자동취소 대기 시간
ORDER_FILL_POLL_SEC = 2


@dataclass
class WatchContext:
    code: str
    name: str
    day_open_price: float
    prev_close_price: float


class StockWatcher:
    def __init__(self, ctx: WatchContext, broker: BrokerBase, slot_manager: SlotManager, total_cash: float):
        self.ctx = ctx
        self.broker = broker
        self.slots = slot_manager
        self.total_cash = total_cash

        self.state = "IDLE"
        self.buy_price = 0.0
        self.qty = 0
        self.entry_time: str | None = None
        self.trade_id: int | None = None
        self._last_bar_time: str | None = None
        self._stop_requested = False

        # 저점 반등(눌림목) 매수용 상태. 시가 돌파와 별개 경로이므로 WAIT_FOR_BREAKOUT 진입과
        # 함께 추적을 시작한다 (감시 시작 이후의 장중 저점 기준 - 그 이전 저가는 대상이 아님).
        self._running_low: float | None = None
        self._pending_reversal: dict | None = None  # {"close", "low"} - 신저가+양봉 확정, 다음봉 확인 대기

        # 진입 경로 기록 (POSITION_HOLDING 청산 로직 분기에 사용).
        self.entry_reason: str | None = None
        self.structural_stop_price: float | None = None

    def force_stop(self):
        self._stop_requested = True

    async def run(self):
        log.info("[%s] 감시 시작 (시가=%s)", self.ctx.name, self.ctx.day_open_price)
        # try/except 를 while 루프 전체가 아니라 매 tick마다 걸어둔다 - KIS 연결이 순간 끊기는
        # 등 일시적 오류 한 번에 이 종목 감시가 통째로 조용히 죽는 것을 방지한다
        # (2026-08-28 실측: 가온전선이 09:00:44 RemoteDisconnected 한 번으로 이후 감시가 전부 중단됨).
        while not self._stop_requested:
            try:
                if self.state == "IDLE":
                    await self._tick_idle()
                elif self.state == "WAIT_FOR_BREAKOUT":
                    await self._tick_wait_for_breakout()
                elif self.state == "POSITION_HOLDING":
                    await self._tick_position_holding()
                elif self.state == "CLOSED":
                    break

                # 포지션 미보유 상태로 진입 제한 시간(ENTRY_WINDOW_END)이 지나면 더 기다려도 매수가 나갈 수
                # 없으므로(risk_manager.is_entry_allowed_now), 장마감까지 폴링을 계속하지 않고
                # 여기서 감시를 종료한다 - 불필요한 API 호출을 줄이기 위함.
                if self.state in ("IDLE", "WAIT_FOR_BREAKOUT") and not time_utils.is_within_entry_window():
                    log.info("[%s] 진입 제한 시간(%s) 경과 및 포지션 미보유 - 감시 종료",
                             self.ctx.name, CONFIG.entry_window_end)
                    break

                if self.state != "POSITION_HOLDING" and time_utils.is_market_close_reached():
                    log.info("[%s] 장마감 도달, 미체결 관찰 종료", self.ctx.name)
                    break
            except Exception:
                log.exception("[%s] 감시 tick 오류 (일시적 오류로 간주, 계속 재시도)", self.ctx.name)

            await asyncio.sleep(POLL_INTERVAL_SEC)

    # ------------------------------------------------------------ IDLE
    async def _tick_idle(self):
        snap = await asyncio.to_thread(self.broker.get_realtime_snapshot, self.ctx.code)
        rt_price = snap.get("price") or await asyncio.to_thread(self.broker.get_current_price, self.ctx.code)
        if rt_price and rt_price < self.ctx.day_open_price:
            self.state = "WAIT_FOR_BREAKOUT"
            log.info("[%s] 시가 이탈 확인 (개미털기 구간 진입) 현재가=%s", self.ctx.name, rt_price)

    # ------------------------------------------------------------ WAIT_FOR_BREAKOUT
    async def _tick_wait_for_breakout(self):
        df = await asyncio.to_thread(self.broker.get_minute_ohlcv_3m, self.ctx.code)
        if df.empty:
            return
        last_bar = df.iloc[-1]
        if self._last_bar_time == last_bar["time"]:
            await asyncio.sleep(BAR_POLL_INTERVAL_SEC - POLL_INTERVAL_SEC)
            return
        self._last_bar_time = last_bar["time"]

        opn, cls, low, vol = (
            float(last_bar["open"]), float(last_bar["close"]), float(last_bar["low"]), float(last_bar["volume"])
        )
        day_open = self.ctx.day_open_price

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
            snap = await asyncio.to_thread(self.broker.get_realtime_snapshot, self.ctx.code)
            vol_power = snap.get("vol_power", 0.0)
            ask_bid_ratio = snap.get("ask_bid_ratio", 0.0)
            is_sd_ok = vol_power >= 100.0 and ask_bid_ratio >= 120.0
            if is_sd_ok:
                await self._attempt_entry(signal_price=cls, entry_reason="시가 돌파")
                return

        if pullback_signal is not None:
            await self._attempt_entry(
                signal_price=pullback_signal["price"], entry_reason="저점 반등",
                structural_stop_price=pullback_signal["stop"],
            )
            return

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

    async def _attempt_entry(self, signal_price: float, entry_reason: str, structural_stop_price: float | None = None):
        # v10 진입 제한 시간 필터 (하드 컷오프 - 진입창 마감 시각 초과 시 해당 종목 영구 진입 금지)
        if not is_entry_allowed_now():
            self.state = "CLOSED"
            log.info("[%s] %s 신호 발생했으나 진입 제한 시간(%s) 초과로 패스", self.ctx.name, entry_reason, CONFIG.entry_window_end)
            await telegram_bot.notify(
                f"🚫 [진입 보류] {self.ctx.name} ({entry_reason})\n신규 진입 제한 시간 경과 ({CONFIG.entry_window_end} 이후)\n"
                f"조치: 시스템 규칙에 따라 자동 패스 (뇌동매매 방지)"
            )
            return

        # v10 슬롯 기반 자금관리 필터 (슬롯이 차있으면 이번 신호만 스킵하고 계속 대기)
        if not self.slots.has_room():
            log.info("[%s] %s 조건 충족했으나 슬롯 부족(%d/%d) - 대기", self.ctx.name, entry_reason, self.slots.active_slots_count, self.slots.max_slots)
            await telegram_bot.notify(
                f"⏳ [진입 대기] {self.ctx.name} ({entry_reason})\n가용 투자 슬롯 초과 ({self.slots.active_slots_count}/{self.slots.max_slots})\n"
                f"조치: 기존 종목 청산 후 슬롯 개방 시 재시도"
            )
            return

        self.entry_reason = entry_reason
        self.structural_stop_price = structural_stop_price
        await self._execute_entry(signal_price=signal_price)

    async def _execute_entry(self, signal_price: float):
        limit_price = round_up_ticks(signal_price, n_ticks=2)
        alloc_amount = self.slots.allocation_amount(self.total_cash)
        qty = int(alloc_amount // limit_price)
        if qty <= 0:
            log.warning("[%s] 배분자금 부족으로 주문 스킵 (배분액=%.0f, 주문가=%s)", self.ctx.name, alloc_amount, limit_price)
            return

        order = await asyncio.to_thread(self.broker.buy_limit, self.ctx.code, qty, limit_price)
        if not order.success:
            log.error("[%s] 매수 주문 실패: %s", self.ctx.name, order.message)
            await telegram_bot.notify(f"❌ [주문 실패] {self.ctx.name} 매수 주문 실패: {order.message}")
            return

        filled_qty = await self._await_fill_or_cancel(order.order_no, self.ctx.code, qty)
        if filled_qty <= 0:
            log.info("[%s] 15초 미체결로 주문 자동 취소, 계속 감시", self.ctx.name)
            return

        if not self.slots.acquire(self.ctx.code, self.ctx.name, limit_price, filled_qty):
            # 체결 직후 슬롯이 이미 마감된 극단적 동시성 케이스: 즉시 반대 청산으로 리스크 제거
            log.warning("[%s] 체결 후 슬롯 획득 실패 - 즉시 청산", self.ctx.name)
            await asyncio.to_thread(self.broker.sell_market, self.ctx.code, filled_qty)
            return

        self.buy_price = limit_price
        self.qty = filled_qty
        self.entry_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.trade_id = database.insert_trade_entry(
            self.ctx.code, self.ctx.name, self.entry_time, self.buy_price, self.qty, order.order_no
        )
        self.state = "POSITION_HOLDING"
        log.info("[%s] 매수 체결 완료 (%s) %s주 @ %s", self.ctx.name, self.entry_reason, self.qty, self.buy_price)
        await telegram_bot.notify(
            f"🛒 [실시간 체결] {self.ctx.name} {self.entry_reason} 매수 성공\n"
            f"체결가: {self.buy_price:,.0f}원 x {self.qty}주\n"
            f"투자 슬롯: {self.slots.active_slots_count}/{self.slots.max_slots}"
        )

    async def _await_fill_or_cancel(self, order_no: str, code: str, qty: int) -> int:
        elapsed = 0
        while elapsed < ORDER_FILL_TIMEOUT_SEC:
            await asyncio.sleep(ORDER_FILL_POLL_SEC)
            elapsed += ORDER_FILL_POLL_SEC
            filled = await asyncio.to_thread(self.broker.get_order_filled_qty, order_no)
            if filled == -1:  # 목록에서 사라짐 = 전량 체결/취소완료로 간주
                return qty
            if filled > 0:
                return filled
        await asyncio.to_thread(self.broker.cancel_order, order_no, code, qty)
        return 0

    # ------------------------------------------------------------ POSITION_HOLDING
    async def _tick_position_holding(self):
        snap = await asyncio.to_thread(self.broker.get_realtime_snapshot, self.ctx.code)
        rt_price = snap.get("price") or await asyncio.to_thread(self.broker.get_current_price, self.ctx.code)
        if not rt_price:
            return

        tp_price = self.buy_price * (1 + CONFIG.target_profit_pct)
        sl_price = self.buy_price * (1 + CONFIG.stop_loss_pct)

        if time_utils.is_market_close_reached():
            await self._exit(rt_price, "장마감 동시청산")
            return
        if rt_price >= tp_price:
            await self._exit(tp_price, "익절 (Target)")
            return
        if rt_price <= sl_price:
            await self._exit(sl_price, "손절 (Stop Loss)")
            return

        if self.structural_stop_price is not None:
            # 저점 반등 진입: 매수 근거였던 반등 저점이 다시 깨지면 시나리오 무효화로 간주하고 즉시 청산한다.
            # (시가 돌파 진입과 달리 매수가 자체가 시가보다 낮은 게 정상이므로 "시가 이탈" 규칙은 적용하지 않는다.)
            if rt_price <= self.structural_stop_price:
                await self._exit(self.structural_stop_price, "반등 저점 이탈 손절")
                return
        elif rt_price < self.ctx.day_open_price:
            await self._exit(self.ctx.day_open_price, "실시간 시가 이탈 손절")
            return

    async def _exit(self, exit_price: float, reason: str):
        order = await asyncio.to_thread(self.broker.sell_market, self.ctx.code, self.qty)
        actual_price = exit_price  # 시장가 체결가는 체결통보/잔고조회로 사후 보정 가능 (여기서는 판정가 기준 기록)
        profit_pct = ((actual_price - self.buy_price) / self.buy_price) * 100

        exit_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if self.trade_id is not None:
            database.close_trade(self.trade_id, exit_time, actual_price, profit_pct, reason)
        self.slots.release(self.ctx.code)
        self.state = "CLOSED"

        emoji = "💰" if profit_pct > 0 else "💔"
        log.info("[%s] 청산 완료 (%s) 수익률=%.2f%%", self.ctx.name, reason, profit_pct)
        await telegram_bot.notify(
            f"{emoji} [실시간 청산] {self.ctx.name}\n사유: {reason}\n"
            f"매수가: {self.buy_price:,.0f}원 → 청산가: {actual_price:,.0f}원\n"
            f"확정 수익률: {profit_pct:+.2f}%\n"
            f"잔여 슬롯: {self.slots.active_slots_count}/{self.slots.max_slots}\n"
            f"(주문결과: {order.message})"
        )
