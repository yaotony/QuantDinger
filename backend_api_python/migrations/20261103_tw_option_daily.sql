-- TAIFEX option daily history and final settlement prices (issue #67).
-- Market data, not tenant data: no user_id. The unique keys make re-imports idempotent.
CREATE TABLE IF NOT EXISTS qd_tw_option_daily (
    id BIGSERIAL PRIMARY KEY,
    trade_date DATE NOT NULL,
    product VARCHAR(10) NOT NULL,
    expiry VARCHAR(12) NOT NULL,
    strike DOUBLE PRECISION NOT NULL,
    option_right VARCHAR(4) NOT NULL,
    session VARCHAR(20) NOT NULL DEFAULT 'regular',
    symbol VARCHAR(40) NOT NULL DEFAULT '',
    open DOUBLE PRECISION,
    high DOUBLE PRECISION,
    low DOUBLE PRECISION,
    close DOUBLE PRECISION,
    volume DOUBLE PRECISION NOT NULL DEFAULT 0,
    settlement_price DOUBLE PRECISION,
    open_interest DOUBLE PRECISION,
    source VARCHAR(20) NOT NULL DEFAULT 'taifex',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_tw_option_daily UNIQUE (trade_date, product, expiry, strike, option_right, session)
);

CREATE INDEX IF NOT EXISTS idx_tw_option_daily_product_date
  ON qd_tw_option_daily (product, session, trade_date);

-- One final settlement price per product and contract month (TXO is cash settled).
CREATE TABLE IF NOT EXISTS qd_tw_option_settlement (
    id BIGSERIAL PRIMARY KEY,
    product VARCHAR(10) NOT NULL,
    contract_month VARCHAR(12) NOT NULL,
    settlement_date DATE NOT NULL,
    final_settlement_price DOUBLE PRECISION NOT NULL,
    source VARCHAR(20) NOT NULL DEFAULT 'taifex',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_tw_option_settlement UNIQUE (product, contract_month)
);
