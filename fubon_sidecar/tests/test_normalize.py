from __future__ import annotations

from fubon_sidecar import normalize


def test_iso_date_field_with_offset_converts_to_unix_seconds():
    # 2026-07-06T08:45+08:00 == 2026-07-06T00:45Z
    assert normalize.to_unix_seconds("2026-07-06T08:45:00.000+08:00") == 1783298700


def test_date_only_is_midnight_taipei():
    # 2026-07-06T00:00+08:00 == 2026-07-05T16:00Z
    assert normalize.to_unix_seconds("2026-07-06") == 1783267200


def test_naive_datetime_string_is_taipei():
    assert normalize.to_unix_seconds("2026-07-06T08:45:00") == 1783298700


def test_epoch_numbers_in_seconds_millis_and_micros():
    assert normalize.to_unix_seconds(1783298700) == 1783298700
    assert normalize.to_unix_seconds(1783298700_000) == 1783298700
    assert normalize.to_unix_seconds(1783298700_000_000) == 1783298700
    assert normalize.to_unix_seconds("1783298700000000") == 1783298700


def test_unparseable_time_returns_none():
    assert normalize.to_unix_seconds("not-a-date") is None
    assert normalize.to_unix_seconds(None) is None
    assert normalize.to_unix_seconds(True) is None


def test_candles_use_date_field_sort_ascending_and_drop_bad_rows():
    rows = [
        {"date": "2026-07-06T08:46:00.000+08:00", "open": 2, "high": 3, "low": 1, "close": 2.5, "volume": 7},
        {"date": "2026-07-06T08:45:00.000+08:00", "open": 1, "high": 2, "low": 0.5, "close": 1.5},
        {"date": "garbage", "open": 1, "high": 1, "low": 1, "close": 1},
        {"date": "2026-07-06T08:47:00.000+08:00", "open": None, "high": 1, "low": 1, "close": 1},
    ]

    candles = normalize.normalize_candles(rows)

    assert candles == [
        {"time": 1783298700, "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 0.0},
        {"time": 1783298760, "open": 2.0, "high": 3.0, "low": 1.0, "close": 2.5, "volume": 7.0},
    ]


def test_candles_fall_back_to_time_field():
    rows = [{"time": 1783298700_000_000, "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1}]
    assert normalize.normalize_candles(rows)[0]["time"] == 1783298700


def test_quote_normalization_converts_last_updated_to_seconds():
    quote = normalize.normalize_quote(
        {
            "symbol": "2330",
            "name": "台積電",
            "lastPrice": 1000,
            "change": 5,
            "changePercent": 0.5,
            "openPrice": 990,
            "highPrice": 1005,
            "lowPrice": 985,
            "previousClose": 995,
            "referencePrice": 995,
            "total": {"tradeVolume": 12345},
            "lastUpdated": 1783298700_000_000,
        },
        "2330",
    )

    assert quote == {
        "symbol": "2330",
        "name": "台積電",
        "lastPrice": 1000.0,
        "change": 5.0,
        "changePercent": 0.5,
        "open": 990.0,
        "high": 1005.0,
        "low": 985.0,
        "previousClose": 995.0,
        "referencePrice": 995.0,
        "volume": 12345.0,
        "time": 1783298700,
    }


def test_tickers_keep_only_whitelisted_fields():
    rows = [
        {"symbol": "TXFG6", "name": "臺股期貨076", "endDate": "2026-07-15", "settlementDate": "2026-07-15",
         "secret": "x"},
        {"name": "missing symbol"},
    ]

    tickers = normalize.normalize_tickers(rows, normalize.FUTOPT_TICKER_FIELDS)

    assert tickers == [
        {
            "symbol": "TXFG6",
            "name": "臺股期貨076",
            "referencePrice": None,
            "contractType": None,
            "startDate": None,
            "endDate": "2026-07-15",
            "settlementDate": "2026-07-15",
        }
    ]
