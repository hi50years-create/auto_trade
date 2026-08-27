"""5분 시초가 돌파 매매 시스템 v10 - 메인 오케스트레이터.
systemd 서비스(trading_bot.service)로 상시 구동되며, asyncio 이벤트 루프 위에서
(1) Telegram 봇 폴링 (2) APScheduler 기반 일정 작업 (3) 종목별 실시간 감시 태스크
(4) 읽기 전용 웹 대시보드를 함께 돌린다.
"""
from __future__ import annotations

import asyncio
import signal
from datetime import date, datetime

import uvicorn
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from src.ai import gemini_sentiment
from src.broker.kis_client import KISClient
from src.config import CONFIG
from src.data import screener
from src.db import database
from src.news import naver_news
from src.notify import telegram_bot
from src.web.dashboard import create_app as create_web_app
from src.strategy.risk_manager import SlotManager, check_gap_up, check_market_index
from src.strategy.state_machine import StockWatcher, WatchContext
from src.utils import time_utils
from src.utils.logger import get_logger

log = get_logger("main")


class TradingEngine:
    def __init__(self):
        self.broker = KISClient()
        self.slots = SlotManager()
        self.watchers: dict[str, StockWatcher] = {}
        self.watcher_tasks: dict[str, asyncio.Task] = {}
        self.name_to_code: dict[str, str] = {}
        self.passed_candidates: list[dict] = []  # 08:50 스크리닝 통과 종목
        self.emergency_stopped = False
        self.trading_blocked_today = False
        self.index_checked_today = False

    # ============================================================ 08:50 예비 스크리닝
    async def pre_screen_job(self):
        if self.emergency_stopped:
            return
        today = date.today().isoformat()
        log.info("[08:50] 장 시작 전 예비 스크리닝 시작")

        # 1순위: 전일 장마감 직후 계산해둔 스냅샷 (API 재호출 불필요, 09:00 타이밍 압박도 없음).
        snapshot = await asyncio.to_thread(database.get_latest_breakout_snapshot)
        if snapshot:
            candidates = screener.candidates_from_snapshot(snapshot)
            log.info("[08:50] 전일 장마감 스냅샷 사용: %d개 후보 (스냅샷 날짜=%s)", len(candidates), snapshot[0]["snapshot_date"])
        else:
            log.warning("[08:50] 저장된 장마감 스냅샷이 없음 - 실시간 순위 API 방식으로 폴백")
            candidates = await asyncio.to_thread(screener.get_prescreening_candidates, self.broker)
            if not candidates:
                # 후보 자체가 0건이면 "오늘 진짜 조건에 맞는 종목이 없다"가 아니라 순위 API 호출이
                # 전부 실패했을 가능성이 높다 (2026-08-27 08:50 정각 혼잡 시간대에 실제로 발생).
                # 사람이 못 보고 지나칠 수 있으니 잠시 대기 후 자동으로 한 번 더 시도한다.
                log.warning("[08:50] 후보 풀이 비어있음 - API 실패 가능성, 30초 대기 후 1회 자동 재시도")
                await asyncio.sleep(30)
                candidates = await asyncio.to_thread(screener.get_prescreening_candidates, self.broker)
            if not candidates:
                log.error("[08:50] 재시도 후에도 후보 풀이 비어있음 - 순위 API 호출 실패로 판단 (수동 /re_screen 필요)")
                await telegram_bot.notify(
                    "⚠️ [08:50] 예비 스크리닝 후보를 하나도 가져오지 못했습니다 (자동 재시도도 실패).\n"
                    "순위 API 호출이 실패했을 가능성이 높습니다 (오늘 조건에 맞는 종목이 없다는 뜻이 아닙니다).\n"
                    "/re_screen 으로 수동 재시도해주세요."
                )

        passed = []
        for cand in candidates:
            code, name = cand["code"], cand["name"]
            self.name_to_code[name] = code
            # 스냅샷 기반 후보는 daily_info 가 이미 계산되어 있어 일봉 재조회가 필요 없다.
            daily_info = cand.get("daily_info") or await asyncio.to_thread(screener.build_daily_info, self.broker, code)
            ok, reasons = screener.screen_stock_batch(daily_info)
            if not ok:
                log.info("[%s] 1차 스크리닝 탈락: %s", name, ", ".join(reasons))
                continue

            news_items = await asyncio.to_thread(naver_news.search_news, name)
            sentiment = await asyncio.to_thread(gemini_sentiment.analyze_sentiment, name, news_items)

            database.upsert_watchlist(
                today, code,
                stock_name=name, state="PRE_SCREENED",
                prev_close=daily_info["prev_close"], prev_high=daily_info["prev_high"],
                prev_trade_amount=daily_info["prev_trade_amount"],
                news_sentiment=sentiment["sentiment"], news_summary=sentiment["summary"],
                news_url=news_items[0]["link"] if news_items else "",
            )
            passed.append({**cand, "daily_info": daily_info, "sentiment": sentiment})

            await telegram_bot.notify(
                f"📢 [08:50] 1차 예비 스크리닝 통과\n"
                f"종목명: {name}\n"
                f"전일 거래대금: {daily_info['prev_trade_amount']/1e8:.1f}억원\n"
                f"AI 뉴스 분석 ({sentiment['emoji']} {sentiment['sentiment']}): {sentiment['summary']}"
            )

        self.passed_candidates = passed
        log.info("[08:50] 예비 스크리닝 완료: %d개 종목 통과", len(passed))

    # ============================================================ 09:00:00 지수/갭 필터 및 감시 개시
    async def _ensure_index_checked(self) -> bool:
        """오늘 종합주가지수 필터를 아직 안 했으면 지금 수행하고, 이미 했으면 그 결과를 재사용한다.
        /re_screen 이 09:00 이후에 호출되어도 지수를 중복으로 다시 조회하지 않기 위함.
        반환값: 오늘 거래 진행 가능 여부."""
        if self.index_checked_today:
            return not self.trading_blocked_today

        today = date.today().isoformat()
        kospi = await asyncio.to_thread(self.broker.get_index_change_pct, "KOSPI")
        kosdaq = await asyncio.to_thread(self.broker.get_index_change_pct, "KOSDAQ")
        index_ok, index_reason = check_market_index(kospi, kosdaq)
        database.set_daily_state(today, kospi, kosdaq, not index_ok, index_reason)
        self.index_checked_today = True

        await telegram_bot.notify(
            f"🔔 [09:00] 종합지수 현황: KOSPI {kospi:+.2f}% / KOSDAQ {kosdaq:+.2f}%\n"
            f"{'✅ 지수 통과' if index_ok else '🛑 ' + index_reason}"
        )
        if not index_ok:
            self.trading_blocked_today = True
            log.warning("종합주가지수 필터 작동 - 당일 거래 전면 중단: %s", index_reason)
            await telegram_bot.notify("🚨 [Emergency Block] 시장 전체 투매/급락 감지로 당일 모든 거래를 보류합니다. 현금 비중 100% 보존.")
        return index_ok

    async def _activate_candidates(self):
        """self.passed_candidates 중 아직 감시를 시작하지 않은 종목의 시가 갭 검증 + 감시 개시.
        09:00 스케줄 작업과 /re_screen(09:00~09:30 사이 호출 시) 양쪽에서 재사용된다."""
        if not time_utils.is_within_entry_window():
            return
        today = date.today().isoformat()
        cash = await asyncio.to_thread(self.broker.get_cash_balance)
        active_codes = []
        for cand in self.passed_candidates:
            code, name = cand["code"], cand["name"]
            if code in self.watchers:
                continue  # 이미 감시 중인 종목은 중복 처리하지 않음
            day_open = await asyncio.to_thread(self._get_confirmed_open_price, code)
            prev_close = cand["daily_info"]["prev_close"]
            if not day_open or not prev_close:
                continue
            gap_ok, gap_pct = check_gap_up(day_open, prev_close)
            database.upsert_watchlist(today, code, day_open_price=day_open, gap_up_pct=gap_pct,
                                       state="ACTIVE" if gap_ok else "CLOSED")
            if not gap_ok:
                await telegram_bot.notify(f"❌ [09:00] {name} 시가 갭 스크리닝 제외 (갭 {gap_pct:.2f}% > {CONFIG.gap_up_max_pct}%)")
                continue

            active_codes.append(code)
            self._start_watcher(code, name, day_open, prev_close, cash)

        if active_codes:
            await asyncio.to_thread(self.broker.subscribe_realtime, active_codes, None)
            await telegram_bot.notify(f"🟢 실시간 매수 감시 개시: {len(active_codes)}개 종목")

    async def market_open_job(self):
        if self.emergency_stopped:
            return
        if not await self._ensure_index_checked():
            return
        await self._activate_candidates()

    def _get_confirmed_open_price(self, code: str) -> float:
        df = self.broker.get_minute_ohlcv_3m(code)
        if not df.empty:
            return float(df.iloc[0]["open"])
        return self.broker.get_current_price(code)

    def _start_watcher(self, code: str, name: str, day_open: float, prev_close: float, cash: float):
        ctx = WatchContext(code=code, name=name, day_open_price=day_open, prev_close_price=prev_close)
        watcher = StockWatcher(ctx, self.broker, self.slots, cash)
        self.watchers[code] = watcher
        self.watcher_tasks[code] = asyncio.create_task(watcher.run())

    # ============================================================ 15:30 장마감 리셋
    async def eod_reset_job(self):
        log.info("[15:30] 장마감 - 잔여 태스크 정리 및 상태 리셋")
        for task in list(self.watcher_tasks.values()):
            if not task.done():
                await asyncio.wait([task], timeout=30)

        trades = database.get_today_trades()
        closed = [t for t in trades if t["result"]]
        if closed:
            win = sum(1 for t in closed if (t["profit_pct"] or 0) > 0)
            avg = sum(t["profit_pct"] or 0 for t in closed) / len(closed)
            await telegram_bot.notify(
                f"📊 [15:30] 당일 매매 마감 리포트\n"
                f"총 거래: {len(closed)}건 | 승률: {win/len(closed)*100:.1f}%\n"
                f"평균 수익률: {avg:+.2f}%"
            )
        else:
            await telegram_bot.notify("📊 [15:30] 당일 체결된 거래가 없습니다.")

        self.watchers.clear()
        self.watcher_tasks.clear()
        self.passed_candidates.clear()
        self.trading_blocked_today = False
        self.index_checked_today = False
        log.info("상태 머신 STATE_IDLE 로 복구 완료 (익일 준비)")

    # ============================================================ 15:35 장마감 스냅샷
    async def eod_snapshot_job(self):
        """장마감 직후, 09:00 타이밍 압박 없이 여유있게 오늘의 상한가/신고가 종목을 확정해 DB에
        저장한다. 다음 거래일 08:50 예비 스크리닝은 이 스냅샷을 읽기만 하면 된다."""
        log.info("[15:35] 장마감 스냅샷 계산 시작")
        today = date.today().isoformat()
        try:
            rows = await asyncio.to_thread(screener.compute_eod_breakout_snapshot, self.broker)
        except Exception:
            log.exception("[15:35] 장마감 스냅샷 계산 실패")
            await telegram_bot.notify("⚠️ [15:35] 장마감 스냅샷 계산에 실패했습니다. 내일 08:50에 실시간 순위 API로 자동 폴백됩니다.")
            return

        for row in rows:
            row["snapshot_date"] = today
        await asyncio.to_thread(database.replace_breakout_snapshot, today, rows)

        if rows:
            names = ", ".join(r["stock_name"] for r in rows)
            await telegram_bot.notify(f"🗂️ [15:35] 내일 예비 스크리닝용 스냅샷 저장 완료: {len(rows)}개 종목\n{names}")
        else:
            await telegram_bot.notify("🗂️ [15:35] 오늘은 스냅샷에 저장할 상한가/신고가 종목이 없었습니다.")
        log.info("[15:35] 장마감 스냅샷 저장 완료: %d개", len(rows))

    # ============================================================ 텔레그램 원격 명령
    async def get_status_text(self) -> str:
        lines = [f"📊 시스템 상태 ({datetime.now():%H:%M:%S})",
                 f"운영 모드: {CONFIG.trading_mode.upper()}",
                 f"거래 차단 여부: {'예 (지수 폭락)' if self.trading_blocked_today else '아니오'}",
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
        """웹 대시보드용 계좌 정보(예수금/보유종목). KIS 잔고조회 API를 그대로 사용하므로
        오늘 이 시스템이 진입한 포지션뿐 아니라 계좌에 있는 모든 보유종목이 표시된다.
        잔고조회 1회로 예수금+보유종목을 함께 가져온다 (이전엔 API를 두 번 호출했음)."""
        return await asyncio.to_thread(self.broker.get_account_snapshot)

    async def get_news_text(self, stock_name: str) -> str:
        news_items = await asyncio.to_thread(naver_news.search_news, stock_name)
        sentiment = await asyncio.to_thread(gemini_sentiment.analyze_sentiment, stock_name, news_items)
        lines = [f"📰 {stock_name} 관련 최신 뉴스", f"AI 감성 판정: {sentiment['emoji']} {sentiment['sentiment']}",
                 f"핵심 요약: {sentiment['summary']}"]
        if news_items:
            lines.append(f"원천 링크: {news_items[0]['link']}")
        return "\n".join(lines)

    async def get_supply_demand_text(self, stock_name: str) -> str:
        code = self.name_to_code.get(stock_name)
        if not code:
            return f"'{stock_name}' 종목을 오늘 감시 목록에서 찾을 수 없습니다."
        snap = await asyncio.to_thread(self.broker.get_realtime_snapshot, code)
        if not snap:
            return f"{stock_name}: 아직 실시간 수급 데이터가 수신되지 않았습니다."
        return (
            f"📈 {stock_name} 실시간 수급\n"
            f"체결강도: {snap.get('vol_power', 'N/A')}%\n"
            f"호가잔량비: {snap.get('ask_bid_ratio', 'N/A')}%\n"
            f"※ 외인/기관 가집계 순매수액은 별도 KIS 투자자별 매매동향 API 연동이 필요합니다 (현재 미구현)."
        )

    async def stop_all(self) -> str:
        self.emergency_stopped = True
        closed_count = 0
        for code, w in list(self.watchers.items()):
            if w.state == "POSITION_HOLDING":
                await asyncio.to_thread(self.broker.sell_market, code, w.qty)
                self.slots.release(code)
                closed_count += 1
            w.force_stop()
        return f"🛑 긴급 정지 완료. 보유 포지션 {closed_count}건 시장가 청산 처리. 신규 매매 영구 차단."

    async def re_screen(self) -> str:
        await self.pre_screen_job()
        msg = f"재스크리닝 완료: {len(self.passed_candidates)}개 종목 재편성."
        if time_utils.is_within_entry_window():
            # 09:00 정시 작업이 이미 지나갔더라도(예: 그때 후보가 0건이라 감시가 시작 안 된 경우),
            # 진입 시간대 안이면 새로 찾은 후보의 감시를 바로 이어서 시작한다.
            if await self._ensure_index_checked():
                before = set(self.watchers)
                await self._activate_candidates()
                started = len(self.watchers) - len(before)
                if started > 0:
                    msg += f" 진입 시간대 내라 {started}개 종목 감시를 즉시 개시했습니다."
            else:
                msg += " (단, 지수 필터에 걸려 오늘은 신규 감시를 시작하지 않습니다.)"
        return msg

    async def force_sell(self, stock_name: str) -> str:
        code = self.name_to_code.get(stock_name)
        watcher = self.watchers.get(code) if code else None
        if not watcher or watcher.state != "POSITION_HOLDING":
            return f"'{stock_name}' 은(는) 현재 보유 중인 포지션이 아닙니다."
        await watcher._exit(await asyncio.to_thread(self.broker.get_current_price, code), "수동 강제청산")
        return f"{stock_name} 강제 청산 완료."


async def run():
    database.init_db()
    engine = TradingEngine()
    app = telegram_bot.build_application(engine)

    # misfire_grace_time: 기본값(1초)이면 프로세스가 그 순간 잠깐 바쁘거나(레이트리밋 재시도 등)
    # 절전에서 막 깨어난 직후처럼 스케줄러 루프가 정시에 못 돌면 "지나간 작업"으로 간주해
    # 그냥 스킵해버린다 (2026-08-26 08:50/09:00 작업이 실제로 이렇게 누락됨을 확인).
    # 지연되더라도 최대한 늦게라도 실행되도록 넉넉히 잡는다.
    JOB_DEFAULTS = {"misfire_grace_time": 600, "coalesce": True}
    scheduler = AsyncIOScheduler(timezone="Asia/Seoul", job_defaults=JOB_DEFAULTS)
    scheduler.add_job(engine.pre_screen_job, CronTrigger(
        hour=CONFIG.pre_screen_time.hour, minute=CONFIG.pre_screen_time.minute,
        second=CONFIG.pre_screen_time.second, day_of_week="mon-fri"))
    scheduler.add_job(engine.market_open_job, CronTrigger(
        hour=CONFIG.entry_window_start.hour, minute=CONFIG.entry_window_start.minute,
        second=CONFIG.entry_window_start.second, day_of_week="mon-fri"))
    scheduler.add_job(engine.eod_reset_job, CronTrigger(
        hour=CONFIG.market_close_time.hour, minute=CONFIG.market_close_time.minute,
        second=CONFIG.market_close_time.second, day_of_week="mon-fri"))
    scheduler.add_job(engine.eod_snapshot_job, CronTrigger(
        hour=CONFIG.eod_snapshot_time.hour, minute=CONFIG.eod_snapshot_time.minute,
        second=CONFIG.eod_snapshot_time.second, day_of_week="mon-fri"))
    scheduler.start()

    web_server = None
    web_task = None
    if CONFIG.web_enabled:
        web_app = create_web_app(engine)
        web_config = uvicorn.Config(web_app, host=CONFIG.web_host, port=CONFIG.web_port, log_level="warning")
        web_server = uvicorn.Server(web_config)
        web_task = asyncio.create_task(web_server.serve())
        log.info("웹 대시보드 기동: http://%s:%d", CONFIG.web_host, CONFIG.web_port)

    log.info("트레이딩 봇 시작 (모드=%s)", CONFIG.trading_mode)
    await telegram_bot.notify(f"✅ 트레이딩 봇이 시작되었습니다. (모드: {CONFIG.trading_mode.upper()})")

    async with app:
        await app.start()
        await telegram_bot.set_command_menu()
        await app.updater.start_polling()

        stop_event = asyncio.Event()

        def _handle_signal():
            log.info("종료 시그널 수신")
            stop_event.set()

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, _handle_signal)

        await stop_event.wait()

        await app.updater.stop()
        await app.stop()

    if web_server is not None:
        web_server.should_exit = True
        await web_task
    scheduler.shutdown()


if __name__ == "__main__":
    asyncio.run(run())
