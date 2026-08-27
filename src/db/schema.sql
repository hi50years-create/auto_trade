CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    stock_code TEXT NOT NULL,
    stock_name TEXT NOT NULL,
    entry_time TEXT NOT NULL,
    exit_time TEXT,
    buy_price REAL NOT NULL,
    sell_price REAL,
    qty INTEGER NOT NULL,
    profit_pct REAL,
    result TEXT,
    order_no TEXT,
    trading_mode TEXT NOT NULL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS watchlist (
    stock_code TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    stock_name TEXT,
    state TEXT NOT NULL DEFAULT 'IDLE',
    prev_close REAL,
    prev_high REAL,
    prev_trade_amount REAL,
    day_open_price REAL,
    gap_up_pct REAL,
    news_sentiment TEXT,
    news_summary TEXT,
    news_url TEXT,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (stock_code, trade_date)
);

CREATE TABLE IF NOT EXISTS daily_state (
    trade_date TEXT PRIMARY KEY,
    kospi_change_pct REAL,
    kosdaq_change_pct REAL,
    trading_blocked INTEGER NOT NULL DEFAULT 0,
    block_reason TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

-- 장마감(15:30) 직후에 계산해두는 "오늘 상한가/신고가 돌파 종목" 스냅샷.
-- KIS 순위 API는 항상 '현재 시점' 값만 주기 때문에(과거 특정일 조회 불가), 장 시작 전(09:00 이전)에만
-- '현재가=전일종가'가 성립해 정확했다. 장마감 직후에도 같은 논리로 '현재가=오늘 최종 확정 종가'가
-- 되므로, 09:00 정각을 급박하게 쫓아다니는 대신 여유있는 장마감 시점에 미리 계산해 저장해두고
-- 다음날 08:50에는 이 표만 읽어서(API 재호출 없이) 유동성/뉴스만 재확인한다.
CREATE TABLE IF NOT EXISTS breakout_snapshot (
    snapshot_date TEXT NOT NULL,   -- 이 값을 계산한 날짜 (= 다음 거래일 기준 '전일')
    stock_code TEXT NOT NULL,
    stock_name TEXT,
    reason TEXT,                   -- 상한가(KOSPI) / 신고가근접(KOSDAQ) 등 판정 사유
    close_price REAL,
    high_price REAL,
    trade_amount REAL,
    is_breakout_or_limit_up INTEGER NOT NULL DEFAULT 0,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (snapshot_date, stock_code)
);

CREATE TABLE IF NOT EXISTS system_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    level TEXT NOT NULL,
    message TEXT NOT NULL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_trades_stock_date ON trades (stock_code, entry_time);
CREATE INDEX IF NOT EXISTS idx_watchlist_date ON watchlist (trade_date);
