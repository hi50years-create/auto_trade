"""종목별 실시간 상태 머신. morning_breakout_backtest-v10.py 의 판정 로직을
실거래 폴링 루프(3분봉 완성 감지 + 실시간 틱 감시)로 이식한 버전이다.

백테스트 대비 개선점:
- 슬롯 부족으로 진입이 막힌 경우, 백테스트(데모)는 즉시 CLOSED 처리했지만
  요구사항 3.5.2("대기") 취지에 맞게 여기서는 해당 3분봉 신호만 스킵하고
  진입마감(ENTRY_WINDOW_END, 기본 10:30) 전까지 다음 3분봉에서 슬롯 여유를 재확인한다.
- 진입 제한 시간(ENTRY_WINDOW_END) 초과는 요구사항대로 영구 CLOSED 처리한다.
- 2026-09-16: 시가 돌파 신호가 아예 없는 날(전일 급등주 차익실현 매도일)을 대비해 저점
  반등(눌림목) 매수 경로를 추가했다.
- 2026-09-21: 진입/청산 판정 로직을 src.strategy.plugin_base.Strategy 플러그인으로 분리했다.
  이 파일은 슬롯 관리/주문 집행/DB 기록/텔레그램 알림 같은 공통 배관만 담당하고, "언제
  사고 팔지"는 주입된 Strategy 구현체(breakout_pullback.py, golden_cross.py 등)가 결정한다.
  국내 시가돌파+저점반등 전략의 동작 자체는 이 리팩터링으로 바뀌지 않았다.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime

from src.broker.base import BrokerBase
from src.config import CONFIG
from src.db import database
from src.notify import telegram_bot
from src.strategy.plugin_base import Strategy
from src.strategy.risk_manager import SlotManager
from src.utils import time_utils
from src.utils.logger import get_logger
from src.utils.tick import round_up_ticks, snap_up_to_tick, usd_round

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
    def __init__(
        self, ctx: WatchContext, broker: BrokerBase, slot_manager: SlotManager, total_cash: float,
        strategy: Strategy, market_tag: str = "KR", market_emoji: str = "🇰🇷", currency: str = "KRW",
        time_module=time_utils, entry_window_label: str | None = None,
    ):
        self.ctx = ctx
        self.broker = broker
        self.slots = slot_manager
        self.total_cash = total_cash
        self.strategy = strategy
        self.market_tag = market_tag
        self.market_emoji = market_emoji
        self.currency = currency
        # 국내는 src.utils.time_utils, 미국은 src.utils.us_time_utils (서머타임 자동 반영) -
        # 같은 함수 시그니처(is_within_entry_window/is_before_market_close/is_market_close_reached)
        # 를 맞춰뒀으므로 아무 모듈이나 주입해도 동작한다.
        self.time_module = time_module
        self.entry_window_label = entry_window_label or str(CONFIG.entry_window_end)

        self.state = "IDLE"
        self.buy_price = 0.0
        self.qty = 0
        self.entry_time: str | None = None
        self.trade_id: int | None = None
        self._last_bar_time: str | None = None
        self._watch_started_at: str | None = None  # WAIT_FOR_BREAKOUT 진입 시각(3분봉 버킷)
        self._stop_requested = False

        # 진입 경로 기록 (POSITION_HOLDING 청산 로직 분기에 사용).
        self.entry_reason: str | None = None
        self.structural_stop_price: float | None = None

    def force_stop(self):
        self._stop_requested = True

    def _fmt(self, price: float) -> str:
        return f"${price:,.2f}" if self.currency == "USD" else f"{price:,.0f}원"

    def _round_up_entry(self, price: float) -> float:
        """슬리피지 방지용 매수 지정가 (기준가보다 살짝 높게). 통화별로 다르게 계산해야 한다 -
        2026-09-22 실측: USD 가격에 KRX 계단 호가단위(round_up_ticks)를 그대로 썼더니 소수점
        (센트)이 통째로 날아가 정수 달러로 반올림되는 문제가 있었다."""
        if self.currency == "USD":
            return usd_round(price + 0.02)
        return round_up_ticks(price, n_ticks=2)

    def _round_up_exit(self, price: float) -> float:
        """익절 지정가 (목표가 이상만 인정). 통화별 분기는 _round_up_entry와 동일한 이유."""
        if self.currency == "USD":
            return usd_round(price)
        return snap_up_to_tick(price)

    @staticmethod
    def _current_bar_bucket() -> str:
        """지금 이 순간이 속한 3분봉의 시작 시각("HH:MM")을 반환한다 (kis_client._resample_3min
        의 버킷팅 규칙과 동일하게 자정 기준 총 분을 3분 단위로 내림)."""
        now = datetime.now()
        bucket = (now.hour * 60 + now.minute) // 3 * 3
        return f"{bucket // 60:02d}:{bucket % 60:02d}"

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

                # 포지션 미보유 상태로 진입 제한 시간이 지나면 더 기다려도 매수가 나갈 수 없으므로,
                # 장마감까지 폴링을 계속하지 않고 여기서 감시를 종료한다 - 불필요한 API 호출을 줄이기 위함.
                if self.state in ("IDLE", "WAIT_FOR_BREAKOUT") and not self.time_module.is_within_entry_window():
                    log.info("[%s] 진입 제한 시간(%s) 경과 및 포지션 미보유 - 감시 종료",
                             self.ctx.name, self.entry_window_label)
                    break

                if self.state != "POSITION_HOLDING" and self.time_module.is_market_close_reached():
                    log.info("[%s] 장마감 도달, 미체결 관찰 종료", self.ctx.name)
                    break
            except Exception:
                log.exception("[%s] 감시 tick 오류 (일시적 오류로 간주, 계속 재시도)", self.ctx.name)

            await asyncio.sleep(POLL_INTERVAL_SEC)

    # ------------------------------------------------------------ IDLE
    async def _tick_idle(self):
        if not self.strategy.requires_dip_below_open:
            self.state = "WAIT_FOR_BREAKOUT"
            self._watch_started_at = self._current_bar_bucket()
            log.info("[%s] 감시 개시 (전략이 시가 이탈 전제조건 불필요)", self.ctx.name)
            return

        snap = await asyncio.to_thread(self.broker.get_realtime_snapshot, self.ctx.code)
        rt_price = snap.get("price") or await asyncio.to_thread(self.broker.get_current_price, self.ctx.code)
        if rt_price and rt_price < self.ctx.day_open_price:
            self.state = "WAIT_FOR_BREAKOUT"
            self._watch_started_at = self._current_bar_bucket()
            log.info("[%s] 시가 이탈 확인 (개미털기 구간 진입) 현재가=%s", self.ctx.name, rt_price)

    # ------------------------------------------------------------ WAIT_FOR_BREAKOUT
    async def _tick_wait_for_breakout(self):
        df = await asyncio.to_thread(self.broker.get_minute_ohlcv_3m, self.ctx.code)
        if df.empty:
            return

        # 2026-09-21 실측: get_minute_ohlcv_3m 은 최근 ~10개 3분봉을 매번 통째로 반환하는데,
        # 예전엔 그중 df.iloc[-1](가장 최신 봉) 하나만 보고 나머지는 버렸다. KIS 모의투자 서버가
        # 빈번히 일시적 오류/연결끊김을 내는 걸 감안하면, 한 번의 폴링 실패로 3분봉 하나가 통째로
        # 누락될 수 있는데 - 저점반등 판정은 "신저가+양봉" -> "확인봉" 2개 봉이 연속으로 필요해서
        # 봉 하나만 스킵돼도 패턴이 통째로 깨진다. 마지막 처리 시각 이후의 봉을 전부 순서대로
        # 처리하도록 고쳤었다.
        #
        # 2026-09-22 실측: 그런데도 "첫 성공 폴링"에서는 여전히 df.iloc[-1] 하나만 보고 있었다.
        # 감시 시작(WAIT_FOR_BREAKOUT 진입) 직후 몇 번의 폴링이 연달아 실패하면(오늘 09:00~09:02
        # 사이에만 여러 번 발생), "첫 성공 폴링" 시점엔 이미 몇 분이 지나있어서 그 사이의 진짜
        # 신호 봉(예: 09:00봉)이 "가장 최신 봉"이 아니게 되어 영구히 누락됐다 - 6종목 중 5종목이
        # 이 이유로 신호를 놓친 게 오프라인 재현으로 확인됨. "첫 성공 폴링"이 아니라 "감시 시작
        # 이후"를 기준으로 삼아야 한다.
        if self._last_bar_time is None:
            unprocessed = df[df["time"] >= self._watch_started_at] if self._watch_started_at else df.iloc[[-1]]
        else:
            unprocessed = df[df["time"] > self._last_bar_time]
        if unprocessed.empty:
            await asyncio.sleep(BAR_POLL_INTERVAL_SEC - POLL_INTERVAL_SEC)
            return

        for _, last_bar in unprocessed.iterrows():
            self._last_bar_time = last_bar["time"]
            entered = await self._process_bar(last_bar)
            if entered:
                return

    async def _process_bar(self, last_bar) -> bool:
        """완성된 봉 하나를 전략에 위임해 판정한다. 진입을 시도했으면(체결 여부와 무관) True."""
        signal = await self.strategy.on_bar(self.broker, self.ctx.code, last_bar, self.ctx.day_open_price)
        if signal is None:
            return False
        await self._attempt_entry(
            signal_price=signal.price, entry_reason=signal.reason,
            structural_stop_price=signal.structural_stop_price,
        )
        return True

    async def _attempt_entry(self, signal_price: float, entry_reason: str, structural_stop_price: float | None = None):
        # v10 진입 제한 시간 필터 (하드 컷오프 - 진입창 마감 시각 초과 시 해당 종목 영구 진입 금지)
        if not self.time_module.is_within_entry_window():
            self.state = "CLOSED"
            log.info("[%s] %s 신호 발생했으나 진입 제한 시간(%s) 초과로 패스", self.ctx.name, entry_reason, self.entry_window_label)
            await telegram_bot.notify(
                f"🚫 {self.market_emoji} [진입 보류] {self.ctx.name} ({entry_reason})\n신규 진입 제한 시간 경과 ({self.entry_window_label} 이후)\n"
                f"조치: 시스템 규칙에 따라 자동 패스 (뇌동매매 방지)"
            )
            return

        # v10 슬롯 기반 자금관리 필터 (슬롯이 차있으면 이번 신호만 스킵하고 계속 대기)
        if not self.slots.has_room():
            log.info("[%s] %s 조건 충족했으나 슬롯 부족(%d/%d) - 대기", self.ctx.name, entry_reason, self.slots.active_slots_count, self.slots.max_slots)
            await telegram_bot.notify(
                f"⏳ {self.market_emoji} [진입 대기] {self.ctx.name} ({entry_reason})\n가용 투자 슬롯 초과 ({self.slots.active_slots_count}/{self.slots.max_slots})\n"
                f"조치: 기존 종목 청산 후 슬롯 개방 시 재시도"
            )
            return

        self.entry_reason = entry_reason
        self.structural_stop_price = structural_stop_price
        await self._execute_entry(signal_price=signal_price)

    async def _execute_entry(self, signal_price: float):
        limit_price = self._round_up_entry(signal_price)
        alloc_amount = self.slots.allocation_amount(self.total_cash)
        qty = int(alloc_amount // limit_price)
        if qty <= 0:
            log.warning("[%s] 배분자금 부족으로 주문 스킵 (배분액=%.0f, 주문가=%s)", self.ctx.name, alloc_amount, limit_price)
            return

        # 2026-09-22 실측: 해외 통합증거금 계좌는 예수금 조회값이 신뢰하기 어려워(원화/달러
        # 단위 착오로 21만주 매수 시도가 나간 적 있음), 브로커가 지원하면 실제 매수가능수량으로
        # 한 번 더 상한을 건다 (get_max_buyable_qty 없는 브로커는 조용히 건너뜀).
        cap = getattr(self.broker, "get_max_buyable_qty", None)
        if cap is not None:
            max_qty = await asyncio.to_thread(cap, self.ctx.code, limit_price)
            if max_qty is not None and max_qty < qty:
                log.warning("[%s] 배분 기준 수량(%d)이 실제 매수가능수량(%d) 초과 - 상한으로 축소",
                            self.ctx.name, qty, max_qty)
                qty = max_qty
            if qty <= 0:
                log.warning("[%s] 실제 매수가능수량이 0 - 주문 스킵", self.ctx.name)
                return

        order = await asyncio.to_thread(self.broker.buy_limit, self.ctx.code, qty, limit_price)
        if not order.success:
            log.error("[%s] 매수 주문 실패: %s", self.ctx.name, order.message)
            await telegram_bot.notify(f"❌ {self.market_emoji} [주문 실패] {self.ctx.name} 매수 주문 실패: {order.message}")
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
            self.ctx.code, self.ctx.name, self.entry_time, self.buy_price, self.qty, order.order_no,
            market=self.market_tag,
        )
        self.state = "POSITION_HOLDING"
        log.info("[%s] 매수 체결 완료 (%s) %s주 @ %s", self.ctx.name, self.entry_reason, self.qty, self.buy_price)
        await telegram_bot.notify(
            f"🛒 {self.market_emoji} [실시간 체결] {self.ctx.name} {self.entry_reason} 매수 성공\n"
            f"체결가: {self._fmt(self.buy_price)} x {self.qty}주\n"
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

        if self.time_module.is_market_close_reached():
            await self._exit(rt_price, "장마감 동시청산")
            return
        if rt_price >= tp_price:
            await self._exit(tp_price, "익절 (Target)", prefer_limit=True)
            return
        if rt_price <= sl_price:
            await self._exit(sl_price, "손절 (Stop Loss)")
            return

        strategy_exit = self.strategy.on_tick_holding(
            rt_price, self.buy_price, self.ctx.day_open_price, self.structural_stop_price
        )
        if strategy_exit is not None:
            await self._exit(strategy_exit.price, strategy_exit.reason, prefer_limit=strategy_exit.prefer_limit)
            return

    async def _exit(self, exit_price: float, reason: str, prefer_limit: bool = False):
        remaining_qty = self.qty
        order = None

        if prefer_limit:
            # 3.4절: 익절은 시장가가 아니라 지정가로 나가야 계산된 목표수익률 미만으로 체결되는
            # 슬리피지를 막을 수 있다. 단, 지정가가 하필 안 채워지는 사이 반등분이 꺼지면 이익을
            # 통째로 놓칠 수 있으므로 매수 진입과 동일한 패턴(15초 미체결시 자동취소)으로 재시도하고,
            # 그래도 남은 수량은 시장가로 전환해 반드시 청산한다.
            limit_price = self._round_up_exit(exit_price)
            limit_order = await asyncio.to_thread(self.broker.sell_limit, self.ctx.code, remaining_qty, limit_price)
            if limit_order.success:
                filled_qty = await self._await_fill_or_cancel(limit_order.order_no, self.ctx.code, remaining_qty)
                if filled_qty > 0:
                    order = limit_order
                    remaining_qty -= filled_qty
            else:
                log.warning("[%s] 익절 지정가 주문 실패, 시장가로 전환: %s", self.ctx.name, limit_order.message)

        if remaining_qty > 0:
            if order is not None:
                log.info("[%s] 익절 지정가 부분체결(%d/%d주) - 잔여 %d주 시장가 전환 청산",
                         self.ctx.name, self.qty - remaining_qty, self.qty, remaining_qty)
            order = await asyncio.to_thread(self.broker.sell_market, self.ctx.code, remaining_qty)

        # 체결가는 지정가/시장가 혼합이어도 판정가 기준으로 단순화해 기록한다 (시장가 실제 체결가는
        # 체결통보/잔고조회로 사후 보정 가능 - 기존과 동일한 단순화).
        actual_price = exit_price
        profit_pct = ((actual_price - self.buy_price) / self.buy_price) * 100

        exit_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if self.trade_id is not None:
            database.close_trade(self.trade_id, exit_time, actual_price, profit_pct, reason)
        self.slots.release(self.ctx.code)
        self.state = "CLOSED"

        emoji = "💰" if profit_pct > 0 else "💔"
        log.info("[%s] 청산 완료 (%s) 수익률=%.2f%%", self.ctx.name, reason, profit_pct)
        await telegram_bot.notify(
            f"{emoji} {self.market_emoji} [실시간 청산] {self.ctx.name}\n사유: {reason}\n"
            f"매수가: {self._fmt(self.buy_price)} → 청산가: {self._fmt(actual_price)}\n"
            f"확정 수익률: {profit_pct:+.2f}%\n"
            f"잔여 슬롯: {self.slots.active_slots_count}/{self.slots.max_slots}\n"
            f"(주문결과: {order.message})"
        )
