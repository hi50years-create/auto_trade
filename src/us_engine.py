"""미국주식 모의투자 엔진. 국내 TradingEngine(main.py)과 구조는 비슷하지만, 국내 쪽의
뉴스/감성분석·전일 상한가 스크리너·지수폭락 필터·시가갭 필터는 이번 1차 범위 밖이라 뺐다
(계획서 참고). 대신 정적 관심종목 파일(config/watchlist_us.txt)을 읽어 골든크로스 전략으로
감시한다. 국내 파이프라인(main.TradingEngine)은 이 파일과 무관하게 그대로 동작한다 - 승률
검증 중인 기존 전략을 건드리지 않기 위해 완전히 분리했다.
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime
from pathlib import Path

from src.broker.kis_overseas_client import KISOverseasClient
from src.config import CONFIG, PROJECT_ROOT
from src.db import database
from src.notify import telegram_bot
from src.strategy.golden_cross import GoldenCrossStrategy
from src.strategy.risk_manager import SlotManager
from src.strategy.state_machine import StockWatcher, WatchContext
from src.utils import us_time_utils
from src.utils.logger import get_logger

log = get_logger("us_engine")

WATCHLIST_PATH = PROJECT_ROOT / "config" / "watchlist_us.txt"


def _load_watchlist() -> list[dict]:
    if not WATCHLIST_PATH.exists():
        return []
    out = []
    for line in WATCHLIST_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split(",")]
        code = parts[0]
        name = parts[1] if len(parts) > 1 else code
        out.append({"code": code, "name": name})
    return out


class USTradingEngine:
    def __init__(self):
        self.broker = KISOverseasClient()
        self.slots = SlotManager()
        self.watchers: dict[str, StockWatcher] = {}
        self.watcher_tasks: dict[str, asyncio.Task] = {}
        self.name_to_code: dict[str, str] = {}
        self.emergency_stopped = False

    async def market_open_job(self):
        if self.emergency_stopped:
            return
        candidates = _load_watchlist()
        if not candidates:
            log.warning("[US] %s 가 비어있어 감시할 종목이 없습니다", WATCHLIST_PATH)
            return

        today = date.today().isoformat()
        cash = await asyncio.to_thread(self.broker.get_cash_balance)
        active = []
        for cand in candidates:
            code, name = cand["code"], cand["name"]
            if code in self.watchers:
                continue
            self.name_to_code[name] = code
            day_open = await asyncio.to_thread(self._get_confirmed_open_price, code)
            if not day_open:
                log.warning("[US][%s] 시가 조회 실패 - 감시 제외", name)
                continue
            database.upsert_watchlist(today, code, stock_name=name, state="ACTIVE",
                                       day_open_price=day_open, market="US")
            self._start_watcher(code, name, day_open, cash)
            active.append(code)

        if active:
            await telegram_bot.notify(f"🇺🇸 [US 09:30 ET] 실시간 매수 감시 개시: {len(active)}개 종목 (골든크로스 전략)")

    def _get_confirmed_open_price(self, code: str) -> float:
        df = self.broker.get_minute_ohlcv_3m(code)
        if not df.empty:
            return float(df.iloc[0]["open"])
        return self.broker.get_current_price(code)

    def _start_watcher(self, code: str, name: str, day_open: float, cash: float):
        ctx = WatchContext(code=code, name=name, day_open_price=day_open, prev_close_price=0.0)
        strategy = GoldenCrossStrategy(
            short_window=CONFIG.us_golden_cross_short_window, long_window=CONFIG.us_golden_cross_long_window,
        )
        watcher = StockWatcher(
            ctx, self.broker, self.slots, cash, strategy=strategy,
            market_tag="US", market_emoji="🇺🇸", currency="USD",
            time_module=us_time_utils, entry_window_label=us_time_utils.ENTRY_WINDOW_LABEL,
        )
        self.watchers[code] = watcher
        self.watcher_tasks[code] = asyncio.create_task(watcher.run())

    async def eod_reset_job(self):
        log.info("[US 16:00 ET] 장마감 - 잔여 태스크 정리 및 상태 리셋")
        for task in list(self.watcher_tasks.values()):
            if not task.done():
                await asyncio.wait([task], timeout=30)

        trades = database.get_today_trades(market="US")
        closed = [t for t in trades if t["result"]]
        if closed:
            win = sum(1 for t in closed if (t["profit_pct"] or 0) > 0)
            avg = sum(t["profit_pct"] or 0 for t in closed) / len(closed)
            await telegram_bot.notify(
                f"🇺🇸 [US 16:00 ET] 당일 매매 마감 리포트\n"
                f"총 거래: {len(closed)}건 | 승률: {win/len(closed)*100:.1f}%\n"
                f"평균 수익률: {avg:+.2f}%"
            )
        else:
            await telegram_bot.notify("🇺🇸 [US 16:00 ET] 당일 체결된 거래가 없습니다.")

        self.watchers.clear()
        self.watcher_tasks.clear()

    # ------------------------------------------------------------ EngineProtocol (telegram_bot.py 공용 인터페이스)
    async def get_status_text(self) -> str:
        lines = [f"🇺🇸 미국 모의투자 상태 ({datetime.now():%H:%M:%S} KST)",
                 f"활성 슬롯: {self.slots.active_slots_count}/{self.slots.max_slots}"]
        if not self.watchers:
            lines.append("감시 중인 종목이 없습니다.")
        for code, w in self.watchers.items():
            line = f"• {w.ctx.name}: [{w.state}]"
            if w.state == "POSITION_HOLDING":
                cur = await asyncio.to_thread(self.broker.get_current_price, code)
                pnl = (cur - w.buy_price) / w.buy_price * 100 if w.buy_price else 0
                line += f" 평가손익 {pnl:+.2f}%"
            lines.append(line)
        return "\n".join(lines)

    async def get_account_info(self) -> dict:
        return await asyncio.to_thread(self.broker.get_account_snapshot)

    async def get_news_text(self, stock_name: str) -> str:
        return f"'{stock_name}': 미국 종목 뉴스 감성분석은 이번 범위에 포함되지 않았습니다."

    async def get_supply_demand_text(self, stock_name: str) -> str:
        code = self.name_to_code.get(stock_name)
        if not code:
            return f"'{stock_name}' 종목을 오늘 미국 감시 목록에서 찾을 수 없습니다."
        price = await asyncio.to_thread(self.broker.get_current_price, code)
        return f"🇺🇸 {stock_name} 현재가: ${price:,.2f}\n※ 미국 종목은 체결강도/호가잔량비를 제공하지 않습니다."

    async def stop_all(self) -> str:
        self.emergency_stopped = True
        closed_count = 0
        for code, w in list(self.watchers.items()):
            if w.state == "POSITION_HOLDING":
                await asyncio.to_thread(self.broker.sell_market, code, w.qty)
                self.slots.release(code)
                closed_count += 1
            w.force_stop()
        return f"🛑 미국 긴급 정지 완료. 보유 포지션 {closed_count}건 청산 처리. 신규 매매 영구 차단."

    async def re_screen(self) -> str:
        before = set(self.watchers)
        if us_time_utils.is_within_entry_window():
            await self.market_open_job()
        started = len(self.watchers) - len(before)
        return f"미국 재스크리닝 완료: watchlist_us.txt 기준 {started}개 종목 신규 감시 개시."

    async def force_sell(self, stock_name: str) -> str:
        code = self.name_to_code.get(stock_name)
        watcher = self.watchers.get(code) if code else None
        if not watcher or watcher.state != "POSITION_HOLDING":
            return f"'{stock_name}' 은(는) 현재 보유 중인 미국 포지션이 아닙니다."
        price = await asyncio.to_thread(self.broker.get_current_price, code)
        await watcher._exit(price, "수동 강제청산")
        return f"{stock_name} 강제 청산 완료."
