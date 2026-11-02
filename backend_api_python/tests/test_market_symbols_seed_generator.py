from scripts.generate_market_symbols_seed_sql import STATIC_MARKET_ROWS, build_sql


TW_HOT_SYMBOLS = ("2330.TW", "2317.TW", "2454.TW", "0050.TW", "0056.TW", "IX0001")


def test_generated_seed_includes_twstock_starter_rows_as_hot():
    tw_rows = [row for row in STATIC_MARKET_ROWS if row[0] == "TWStock"]
    assert {row[1] for row in tw_rows} == set(TW_HOT_SYMBOLS)
    assert all(row[3] == "TWSE" and row[4] == "TWD" for row in tw_rows)

    sql = build_sql(tw_rows, [])

    assert "('TWStock', '2330.TW', '台積電', 'TWSE', 'TWD', 'spot', '', 1, 0, 0)" in sql
    insert_end = sql.index("is_active = 1;")
    for asset_class, symbols in (
        ("equity", "'2330.TW','2317.TW','2454.TW'"),
        ("etf", "'0050.TW','0056.TW'"),
        ("index", "'IX0001'"),
    ):
        update = (
            f"SET asset_class = '{asset_class}', is_hot = 1, sort_order = GREATEST(sort_order, 80)\n"
            f"WHERE market = 'TWStock' AND symbol IN ({symbols});"
        )
        assert update in sql
        assert sql.index(update) > insert_end


def test_generated_seed_repairs_curated_etf_metadata_after_symbol_insert():
    sql = build_sql(
        [
            ("HKStock", "02800", "Tracker Fund", "HKEX", "HKD"),
            ("USStock", "SPY", "SPDR S&P 500 ETF Trust", "NYSE Arca", "USD"),
        ],
        [],
    )

    insert_end = sql.index("is_active = 1;")
    repair_start = sql.index("SET asset_class = 'etf', is_hot = 1")

    assert repair_start > insert_end
    assert "WHERE market = 'HKStock'" in sql
    assert "WHERE market = 'USStock'" in sql
    assert "'02800'" in sql
    assert "'SPY'" in sql
