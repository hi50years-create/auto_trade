"""Telegram 알림 + 원격 제어 명령어.
python-telegram-bot v20+ (asyncio 기반) 사용. main.py 의 asyncio 이벤트 루프 안에서 함께 구동된다.
"""
from __future__ import annotations

from typing import Protocol

from telegram import Bot, BotCommand, Update
from telegram.ext import Application, CommandHandler, ContextTypes

# 텔레그램 "/" 메뉴에 표시될 명령어 목록. 여기 추가/수정하면 set_command_menu() 가 반영한다.
COMMAND_MENU = [
    BotCommand("status", "시스템 상태, 활성 슬롯, 보유종목 평가손익"),
    BotCommand("news", "종목명 뉴스+AI 감성 요약 (/news 삼성전자)"),
    BotCommand("supply_demand", "종목명 실시간 체결강도/호가잔량비 (/sd 삼성전자)"),
    BotCommand("re_screen", "장전 스크리닝 파이프라인 수동 재기동"),
    BotCommand("force_sell", "종목명 즉시 시장가 청산 (/force_sell 삼성전자)"),
    BotCommand("stop_all", "긴급 전체 중지 + 보유잔고 전량 시장가 청산"),
]

from src.config import CONFIG
from src.utils.logger import get_logger

log = get_logger("telegram_bot")

_bot: Bot | None = None


class EngineProtocol(Protocol):
    """main.py 의 TradingEngine 이 구현해야 하는 인터페이스 (원격 명령 처리용)."""

    async def get_status_text(self) -> str: ...
    async def get_news_text(self, stock_name: str) -> str: ...
    async def get_supply_demand_text(self, stock_name: str) -> str: ...
    async def stop_all(self) -> str: ...
    async def re_screen(self) -> str: ...
    async def force_sell(self, stock_name: str) -> str: ...


def _get_bot() -> Bot:
    global _bot
    if _bot is None:
        _bot = Bot(token=CONFIG.telegram_bot_token)
    return _bot


async def set_command_menu():
    """텔레그램 클라이언트의 '/' 자동완성 메뉴에 COMMAND_MENU 를 등록한다.
    등록해두지 않으면 명령어를 알아도 자동완성/목록이 뜨지 않는다."""
    bot = _get_bot()
    await bot.set_my_commands(COMMAND_MENU)


async def notify(text: str):
    """시스템 알림(체결/청산/스크리닝 결과 등)은 그룹 채팅(TELEGRAM_CHAT_ID)으로만 발송한다.
    명령 실행 권한은 TELEGRAM_USER_CHAT_ID(개인 DM)에도 별도로 부여된다 (CONFIG.telegram_allowed_chat_ids 참고)."""
    bot = _get_bot()
    try:
        await bot.send_message(chat_id=CONFIG.telegram_chat_id, text=text, parse_mode=None)
    except Exception:
        log.exception("텔레그램 전송 실패 chat_id=%s", CONFIG.telegram_chat_id)


async def send_to_personal(text: str) -> bool:
    """로그인 코드처럼 그룹 구성원에게 보이면 안 되는 메시지는 개인 DM(TELEGRAM_USER_CHAT_ID)으로만 보낸다.
    개인 DM이 설정되어 있지 않으면 전송하지 않고 False 를 반환한다 (그룹으로 새는 것을 방지)."""
    if not CONFIG.telegram_user_chat_id:
        log.warning("TELEGRAM_USER_CHAT_ID 미설정 - 개인 DM 전송 불가")
        return False
    bot = _get_bot()
    try:
        await bot.send_message(chat_id=CONFIG.telegram_user_chat_id, text=text, parse_mode=None)
        return True
    except Exception:
        log.exception("텔레그램 개인 DM 전송 실패")
        return False


def _is_authorized(update: Update) -> bool:
    chat_id = update.effective_chat.id if update.effective_chat else None
    return chat_id in CONFIG.telegram_allowed_chat_ids


def _require_arg(context: ContextTypes.DEFAULT_TYPE) -> str:
    return " ".join(context.args) if context.args else ""


def build_application(engine: EngineProtocol) -> Application:
    app = Application.builder().token(CONFIG.telegram_bot_token).build()

    async def guard(update: Update) -> bool:
        if not _is_authorized(update):
            await update.message.reply_text("⛔ 권한이 없는 사용자입니다.")
            log.warning("미허가 사용자 명령 시도: chat_id=%s", update.effective_chat.id)
            return False
        return True

    async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await guard(update):
            return
        await update.message.reply_text(await engine.get_status_text())

    async def cmd_news(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await guard(update):
            return
        stock_name = _require_arg(context)
        if not stock_name:
            await update.message.reply_text("사용법: /news [종목명]")
            return
        await update.message.reply_text(await engine.get_news_text(stock_name))

    async def cmd_supply_demand(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await guard(update):
            return
        stock_name = _require_arg(context)
        if not stock_name:
            await update.message.reply_text("사용법: /supply_demand [종목명] (또는 /sd)")
            return
        await update.message.reply_text(await engine.get_supply_demand_text(stock_name))

    async def cmd_stop_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await guard(update):
            return
        await update.message.reply_text("🚨 긴급 전체 중지 명령을 수신했습니다. 처리 중...")
        await update.message.reply_text(await engine.stop_all())

    async def cmd_re_screen(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await guard(update):
            return
        await update.message.reply_text("🔄 실시간 스크리닝을 강제 재기동합니다...")
        await update.message.reply_text(await engine.re_screen())

    async def cmd_force_sell(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await guard(update):
            return
        stock_name = _require_arg(context)
        if not stock_name:
            await update.message.reply_text("사용법: /force_sell [종목명]")
            return
        await update.message.reply_text(await engine.force_sell(stock_name))

    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("news", cmd_news))
    app.add_handler(CommandHandler("supply_demand", cmd_supply_demand))
    app.add_handler(CommandHandler("sd", cmd_supply_demand))
    app.add_handler(CommandHandler("stop_all", cmd_stop_all))
    app.add_handler(CommandHandler("re_screen", cmd_re_screen))
    app.add_handler(CommandHandler("force_sell", cmd_force_sell))

    return app
