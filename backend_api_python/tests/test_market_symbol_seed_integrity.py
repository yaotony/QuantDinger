from pathlib import Path


MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"


def test_market_symbol_seed_has_no_known_mojibake() -> None:
    sql = (MIGRATIONS / "init.sql").read_text(encoding="utf-8")
    corrupted_fragments = (
        "璐靛窞鑼呭彴",
        "鎷涘晢閾惰",
        "涓浗骞冲畨",
        "鑵捐鎺ц偂",
        "小斜械褉斜邪薪泻",
    )

    assert not any(fragment in sql for fragment in corrupted_fragments)


def test_master_seed_ships_twstock_hot_symbols() -> None:
    sql = (MIGRATIONS / "market_symbols_master.sql").read_text(encoding="utf-8")
    expected = {
        "2330.TW": "台積電",
        "2317.TW": "鴻海",
        "2454.TW": "聯發科",
        "0050.TW": "元大台灣50",
        "0056.TW": "元大高股息",
        "IX0001": "發行量加權股價指數",
    }

    insert_end = sql.index("is_active = 1;")
    for symbol, name in expected.items():
        row = f"  ('TWStock', '{symbol}', '{name}', 'TWSE', 'TWD', 1, 0, 0),"
        assert row in sql
        assert sql.index(row) < insert_end

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


def test_a_share_hot_seed_uses_canonical_exchange() -> None:
    sql = (MIGRATIONS / "init.sql").read_text(encoding="utf-8")
    hot_symbols = (
        "600519",
        "600036",
        "601318",
        "600900",
        "601899",
        "000858",
        "000333",
        "002594",
        "300750",
        "000001",
    )

    for symbol in hot_symbols:
        rows = [line for line in sql.splitlines() if line.startswith(f"('CNStock', '{symbol}',")]
        assert rows
        assert all(", 'CN', 'CNY'," in row for row in rows)

    assert "legacy.exchange IN ('SSE', 'SZSE')" in sql
    assert "canonical.exchange = 'CN'" in sql
