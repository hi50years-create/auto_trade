"""SQLite 접근 계층. sqlite3 는 커넥션이 스레드 세이프하지 않으므로 호출마다 새 커넥션을 연다
(트래픽이 낮은 개인용 자동매매 규모에서는 매 호출 재접속 오버헤드가 무시할 수준)."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path

from src.config import CONFIG, PROJECT_ROOT
from src.utils.logger import get_logger

log = get_logger("database")

DB_FILE = PROJECT_ROOT / CONFIG.db_path
SCHEMA_FILE = Path(__file__).parent / "schema.sql"


def init_db():
    DB_FILE.parent.mkdir(parents=True, exist_ok=True)
    with _connect() as conn:
        conn.executescript(SCHEMA_FILE.read_text(encoding="utf-8"))
        _migrate_add_market_column(conn)
    log.info("DB 초기화 완료: %s", DB_FILE)


def _migrate_add_market_column(conn: sqlite3.Connection):
    """2026-09-21 미국 마켓 추가: 기존에 이미 데이터가 쌓인 VM DB는 schema.sql의
    CREATE TABLE IF NOT EXISTS 로는 컬럼이 새로 안 생기므로(테이블이 이미 존재), 직접
    ALTER TABLE로 추가한다. 이미 컬럼이 있으면(신규 설치) 조용히 건너뛴다."""
    for table in ("trades", "watchlist", "daily_state"):
        cols = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if "market" not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN market TEXT NOT NULL DEFAULT 'KR'")
            log.info("DB 마이그레이션: %s 테이블에 market 컬럼 추가", table)


@contextmanager
def _connect():
    conn = sqlite3.connect(DB_FILE, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------- trades
def insert_trade_entry(
    stock_code: str, stock_name: str, entry_time: str, buy_price: float, qty: int, order_no: str,
    market: str = "KR",
) -> int:
    with _connect() as conn:
        cur = conn.execute(
            """INSERT INTO trades (stock_code, stock_name, entry_time, buy_price, qty, order_no, trading_mode, market)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (stock_code, stock_name, entry_time, buy_price, qty, order_no, CONFIG.trading_mode, market),
        )
        return cur.lastrowid


def close_trade(trade_id: int, exit_time: str, sell_price: float, profit_pct: float, result: str):
    with _connect() as conn:
        conn.execute(
            """UPDATE trades SET exit_time=?, sell_price=?, profit_pct=?, result=? WHERE id=?""",
            (exit_time, sell_price, profit_pct, result, trade_id),
        )


def get_today_trades(market: str = "KR") -> list[sqlite3.Row]:
    today = date.today().isoformat()
    with _connect() as conn:
        return conn.execute(
            "SELECT * FROM trades WHERE entry_time LIKE ? AND market=? ORDER BY id", (f"{today}%", market)
        ).fetchall()


# ---------------------------------------------------------------- watchlist
def upsert_watchlist(trade_date: str, stock_code: str, **fields):
    if not fields:
        return
    with _connect() as conn:
        existing = conn.execute(
            "SELECT 1 FROM watchlist WHERE stock_code=? AND trade_date=?", (stock_code, trade_date)
        ).fetchone()
        fields["updated_at"] = datetime.now().isoformat()
        if existing:
            set_clause = ", ".join(f"{k}=?" for k in fields)
            conn.execute(
                f"UPDATE watchlist SET {set_clause} WHERE stock_code=? AND trade_date=?",
                (*fields.values(), stock_code, trade_date),
            )
        else:
            fields["stock_code"] = stock_code
            fields["trade_date"] = trade_date
            cols = ", ".join(fields.keys())
            placeholders = ", ".join("?" for _ in fields)
            conn.execute(f"INSERT INTO watchlist ({cols}) VALUES ({placeholders})", tuple(fields.values()))


def get_watchlist(trade_date: str) -> list[sqlite3.Row]:
    with _connect() as conn:
        return conn.execute("SELECT * FROM watchlist WHERE trade_date=?", (trade_date,)).fetchall()


# ---------------------------------------------------------------- daily_state
def set_daily_state(trade_date: str, kospi_pct: float, kosdaq_pct: float, blocked: bool, reason: str = ""):
    with _connect() as conn:
        conn.execute(
            """INSERT INTO daily_state (trade_date, kospi_change_pct, kosdaq_change_pct, trading_blocked, block_reason)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(trade_date) DO UPDATE SET
                 kospi_change_pct=excluded.kospi_change_pct,
                 kosdaq_change_pct=excluded.kosdaq_change_pct,
                 trading_blocked=excluded.trading_blocked,
                 block_reason=excluded.block_reason""",
            (trade_date, kospi_pct, kosdaq_pct, int(blocked), reason),
        )


def get_daily_state(trade_date: str) -> sqlite3.Row | None:
    with _connect() as conn:
        return conn.execute("SELECT * FROM daily_state WHERE trade_date=?", (trade_date,)).fetchone()


# ---------------------------------------------------------------- breakout_snapshot
def replace_breakout_snapshot(snapshot_date: str, rows: list[dict]):
    """해당 날짜의 스냅샷을 통째로 교체한다 (같은 날 재계산 시 중복/잔여 방지)."""
    with _connect() as conn:
        conn.execute("DELETE FROM breakout_snapshot WHERE snapshot_date=?", (snapshot_date,))
        conn.executemany(
            """INSERT INTO breakout_snapshot
               (snapshot_date, stock_code, stock_name, reason, close_price, high_price,
                trade_amount, is_breakout_or_limit_up)
               VALUES (:snapshot_date, :stock_code, :stock_name, :reason, :close_price, :high_price,
                       :trade_amount, :is_breakout_or_limit_up)""",
            rows,
        )


def get_breakout_snapshot(snapshot_date: str) -> list[sqlite3.Row]:
    with _connect() as conn:
        return conn.execute(
            "SELECT * FROM breakout_snapshot WHERE snapshot_date=?", (snapshot_date,)
        ).fetchall()


def get_latest_breakout_snapshot() -> list[sqlite3.Row]:
    """가장 최근에 저장된 스냅샷 날짜의 전체 행을 반환한다. 주말/공휴일 등으로 정확히
    '어제'가 아닐 수 있으므로 날짜를 직접 계산하지 않고 '가장 최근 것'을 그대로 쓴다."""
    with _connect() as conn:
        latest = conn.execute("SELECT MAX(snapshot_date) AS d FROM breakout_snapshot").fetchone()
        if not latest or not latest["d"]:
            return []
        return conn.execute(
            "SELECT * FROM breakout_snapshot WHERE snapshot_date=?", (latest["d"],)
        ).fetchall()


# ---------------------------------------------------------------- system_log
def log_event(level: str, message: str):
    with _connect() as conn:
        conn.execute("INSERT INTO system_log (level, message) VALUES (?, ?)", (level, message))
