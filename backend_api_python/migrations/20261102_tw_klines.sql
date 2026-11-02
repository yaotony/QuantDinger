-- Taiwan market K-lines accumulated from the Fubon sidecar (and later TAIFEX imports).
-- Market data, not tenant data: no user_id. The unique key makes repeated writes idempotent.
CREATE TABLE IF NOT EXISTS qd_tw_klines (
    id BIGSERIAL PRIMARY KEY,
    market VARCHAR(20) NOT NULL,
    symbol VARCHAR(40) NOT NULL,
    session VARCHAR(20) NOT NULL DEFAULT 'regular',
    timeframe VARCHAR(10) NOT NULL,
    bar_time TIMESTAMPTZ NOT NULL,
    open DOUBLE PRECISION NOT NULL,
    high DOUBLE PRECISION NOT NULL,
    low DOUBLE PRECISION NOT NULL,
    close DOUBLE PRECISION NOT NULL,
    volume DOUBLE PRECISION NOT NULL DEFAULT 0,
    source VARCHAR(20) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_tw_klines_bar UNIQUE (market, symbol, session, timeframe, bar_time)
);

-- One row per (symbol, session, trading day) of the daily collection job:
-- success, failed (retried by the next run) or missed (a gap the job could not fill).
CREATE TABLE IF NOT EXISTS qd_tw_kline_collect_status (
    id BIGSERIAL PRIMARY KEY,
    market VARCHAR(20) NOT NULL,
    symbol VARCHAR(40) NOT NULL,
    session VARCHAR(20) NOT NULL,
    trade_date DATE NOT NULL,
    status VARCHAR(20) NOT NULL,
    bar_count INTEGER NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_tw_kline_collect_status UNIQUE (market, symbol, session, trade_date)
);

CREATE INDEX IF NOT EXISTS idx_tw_kline_collect_status_date
  ON qd_tw_kline_collect_status (trade_date);
