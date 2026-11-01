-- Historical perpetual-swap funding rates used by backtest funding simulation.
-- One row per settlement; the unique key deduplicates repeated backfills.
CREATE TABLE IF NOT EXISTS qd_perpetual_funding_rates (
    id BIGSERIAL PRIMARY KEY,
    exchange_id VARCHAR(40) NOT NULL,
    symbol VARCHAR(80) NOT NULL,
    funding_time TIMESTAMP NOT NULL,
    funding_rate DOUBLE PRECISION NOT NULL,
    mark_price DOUBLE PRECISION,
    source VARCHAR(80) NOT NULL DEFAULT '',
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (exchange_id, symbol, funding_time)
);
