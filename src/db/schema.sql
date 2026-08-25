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

CREATE TABLE IF NOT EXISTS system_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    level TEXT NOT NULL,
    message TEXT NOT NULL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_trades_stock_date ON trades (stock_code, entry_time);
CREATE INDEX IF NOT EXISTS idx_watchlist_date ON watchlist (trade_date);
