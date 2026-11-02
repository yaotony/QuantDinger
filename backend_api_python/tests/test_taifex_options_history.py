"""TAIFEX option history import: parsers, idempotent upserts, throttled downloads (issue #67).

No test touches the TAIFEX website or PostgreSQL: the downloader is an injected
fake and the repository is an in-memory stand-in keyed like the unique
constraints of ``qd_tw_option_daily`` / ``qd_tw_option_settlement``.
"""
from __future__ import annotations

import importlib.util
import io
import zipfile
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest

from app.services.taifex_history import TaifexFormatError
from app.services.taifex_options_history import (
    DAILY_OPTIONS_URL,
    FINAL_SETTLEMENT_URL,
    TaifexOptionsImporter,
    monthly_symbol,
    normalize_expiry,
    parse_final_settlements,
    parse_option_daily_report,
    parse_option_products,
    parse_right,
)

# Header of the TAIFEX "每日選擇權交易行情" download (columns are found by name).
DAILY_HEADER = (
    "交易日期,契約,到期月份(週別),履約價,買賣權,開盤價,最高價,最低價,收盤價,成交量,結算價,"
    "未沖銷契約數,最後最佳買價,最後最佳賣價,歷史最高價,歷史最低價,是否因訊息面暫停交易,交易時段,漲跌價,漲跌%"
)
DAILY_REPORT = "\n".join([
    DAILY_HEADER,
    # Regular-session call and put of the August 2026 monthly series.
    "2026/08/03,TXO,202608     ,23000.0000,買權,95,110,90,100,1500,100,8000,99,101,300,5,,一般,5,5.26%",
    "2026/08/03,TXO,202608     ,23000.0000,賣權,120,130,105,110,1300,110,7000,109,111,400,8,,一般,-5,-4.35%",
    # After-hours session of the same call.
    "2026/08/03,TXO,202608     ,23000.0000,買權,100,104,98,102,300,-,-,101,103,300,5,,盤後,2,2%",
    # No trade today: prices are '-', but settlement and open interest still matter.
    "2026/08/03,TXO,202609     ,24000.0000,買權,-,-,-,-,0,35,120,-,-,90,30,,一般,-,-",
    # Weekly series is stored with its week label.
    "2026/08/03,TXO,202608W1   ,23100.0000,賣權,40,45,38,41,900,41,2000,40,42,90,20,,一般,1,2%",
    # Another product, filtered out when only TXO is requested.
    "2026/08/03,TEO,202608     ,1200.0000,買權,10,11,9,10,5,10,20,-,-,20,5,,一般,0,0%",
    # Invalid rows: unknown right, bad date, a spread month, a missing strike, a truncated row.
    "2026/08/03,TXO,202608     ,23000.0000,XX,1,1,1,1,1,1,1,-,-,1,1,,一般,0,0%",
    "2026/13/45,TXO,202608     ,23000.0000,買權,1,1,1,1,1,1,1,-,-,1,1,,一般,0,0%",
    "2026/08/03,TXO,202608/202609,23000.0000,買權,1,1,1,1,1,1,1,-,-,1,1,,一般,0,0%",
    "2026/08/03,TXO,202608     ,-,買權,1,1,1,1,1,1,1,-,-,1,1,,一般,0,0%",
    "2026/08/03,TXO,202608",
])

# Wide final-settlement table: one column per product.
SETTLEMENT_WIDE = "\n".join([
    "最後結算日,契約月份,臺股期貨(TX),臺指選擇權(TXO),電子期貨(TE)",
    "2026/07/15,202607,22850.12,22850.12,1100.5",
    "2026/08/19,202608,23150.00,23150.00,1120.0",
    "2026/08/05,202608W1,23010.55,-,-",
    "2026/09/16,202609,-,-,-",
])

SETTLEMENT_LONG = "\n".join([
    "最後結算日,契約,契約月份,最後結算價",
    "2026/08/19,TXO,202608,23150",
    "2026/08/19,TX,202608,23150",
    "2026/09/16,TXO,202609,-",
])


class FakeRepository:
    def __init__(self) -> None:
        self.daily: Dict[Tuple[Any, ...], Any] = {}
        self.settlements: Dict[Tuple[str, str], Any] = {}
        self.calls = 0

    def upsert_daily(self, rows):
        rows = list(rows)
        self.calls += 1
        for row in rows:
            self.daily[(row.trade_date, row.product, row.expiry, row.strike, row.right, row.session)] = row
        return len(rows)

    def upsert_settlements(self, rows):
        rows = list(rows)
        for row in rows:
            self.settlements[(row.product, row.contract_month)] = row
        return len(rows)


# --------------------------------------------------------------------------- helpers
def test_small_helpers():
    assert parse_option_products("txo, TXO,TEO") == ["TXO", "TEO"]
    with pytest.raises(ValueError):
        parse_option_products("TX O")
    assert parse_right("買權") == "call" and parse_right("賣權") == "put"
    assert parse_right("Put") == "put" and parse_right("?") is None
    assert normalize_expiry(" 202608   ") == "202608"
    assert normalize_expiry("202608W1") == "202608W1"
    assert normalize_expiry("202613") is None
    assert normalize_expiry("202608/202609") is None
    # Option codes come from #59's single implementation.
    assert monthly_symbol("TXO", "202608", 23000.0, "call") == "TXO23000H6"
    assert monthly_symbol("TXO", "202608", 23000.0, "put") == "TXO23000T6"
    assert monthly_symbol("TXO", "202608W1", 23000.0, "put") == ""
    assert monthly_symbol("TEO", "202608", 1200.0, "call") == ""


# --------------------------------------------------------------------------- daily parser
def _by_key(rows):
    return {(r.expiry, r.strike, r.right, r.session): r for r in rows}


def test_daily_parser_reads_months_strikes_rights_and_sessions():
    result = parse_option_daily_report(DAILY_REPORT, products=["TXO"])
    rows = _by_key(result.rows)
    assert len(result.rows) == 5
    assert result.invalid == 5

    call = rows[("202608", 23000.0, "call", "regular")]
    assert call.trade_date == date(2026, 8, 3)
    assert (call.open, call.high, call.low, call.close) == (95, 110, 90, 100)
    assert (call.volume, call.settlement_price, call.open_interest) == (1500, 100, 8000)
    assert call.symbol == "TXO23000H6" and call.source == "taifex"

    put = rows[("202608", 23000.0, "put", "regular")]
    assert put.close == 110 and put.symbol == "TXO23000T6"

    night = rows[("202608", 23000.0, "call", "afterhours")]
    assert night.close == 102 and night.settlement_price is None and night.open_interest is None
    assert night.trade_date == date(2026, 8, 3)  # 交易日期 kept as published

    idle = rows[("202609", 24000.0, "call", "regular")]
    assert idle.close is None and idle.volume == 0
    assert idle.settlement_price == 35 and idle.open_interest == 120

    weekly = rows[("202608W1", 23100.0, "put", "regular")]
    assert weekly.symbol == "" and not weekly.is_monthly


def test_daily_parser_keeps_other_products_without_filter():
    result = parse_option_daily_report(DAILY_REPORT)
    assert {r.product for r in result.rows} == {"TXO", "TEO"}


def test_daily_parser_reads_big5_and_utf8_files():
    importer = TaifexOptionsImporter(repository=FakeRepository(), fetcher=lambda *a: b"", delay=0)
    for payload in (DAILY_REPORT.encode("cp950"), DAILY_REPORT.encode("utf-8-sig"), DAILY_REPORT.encode("utf-8")):
        repo = FakeRepository()
        importer.repository = repo
        report = importer.import_daily_bytes(payload, products=["TXO"])
        assert report.daily_rows == 5 and report.invalid_rows == 5
        assert repo.daily[(date(2026, 8, 3), "TXO", "202608", 23000.0, "call", "regular")].close == 100


def test_daily_parser_reads_a_zipped_report():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("OptionsDaily_2026_08_03.csv", DAILY_REPORT.encode("cp950"))
    repo = FakeRepository()
    TaifexOptionsImporter(repository=repo, fetcher=lambda *a: b"", delay=0).import_daily_bytes(buffer.getvalue())
    assert len(repo.daily) == 6


def test_daily_parser_finds_columns_by_name_not_position():
    reordered = "\n".join([
        "交易時段,買賣權,履約價,到期月份(週別),契約,交易日期,收盤價,開盤價,最高價,最低價,未沖銷契約數,結算價,成交量",
        "一般,賣權,22000,202608,TXO,2026/08/03,55,50,60,45,900,56,300",
    ])
    (row,) = parse_option_daily_report(reordered).rows
    assert (row.right, row.strike, row.close, row.settlement_price, row.open_interest, row.volume) == (
        "put", 22000.0, 55.0, 56.0, 900.0, 300.0)


def test_daily_parser_rejects_unknown_files_and_accepts_no_data_pages():
    with pytest.raises(TaifexFormatError):
        parse_option_daily_report("foo,bar\n1,2")
    assert parse_option_daily_report("<html>查無資料</html>").rows == []
    # A report without the session column (older files) is the regular session.
    old = "\n".join([
        "交易日期,契約,到期月份,履約價,買賣權,開盤價,最高價,最低價,收盤價,成交量,結算價,未沖銷契約數",
        "2020/01/02,TXO,202001,12000,買權,100,120,90,110,50,111,500",
    ])
    (row,) = parse_option_daily_report(old).rows
    assert row.session == "regular" and row.symbol == "TXO12000A0"


# --------------------------------------------------------------------------- settlements
def test_wide_settlement_table():
    result = parse_final_settlements(SETTLEMENT_WIDE, products=["TXO"])
    prices = {(r.contract_month, r.settlement_date, r.price) for r in result.rows}
    assert prices == {("202607", date(2026, 7, 15), 22850.12), ("202608", date(2026, 8, 19), 23150.0)}
    assert result.invalid == 0  # '-' cells are "no settlement", never zero


def test_long_settlement_table_and_missing_price_is_not_zero():
    result = parse_final_settlements(SETTLEMENT_LONG.encode("cp950").decode("cp950"), products=["TXO"])
    assert [(r.product, r.contract_month, r.price) for r in result.rows] == [("TXO", "202608", 23150.0)]
    assert result.invalid == 1


def test_settlement_parser_rejects_unknown_layout():
    with pytest.raises(TaifexFormatError):
        parse_final_settlements("a,b,c\n1,2,3")


# --------------------------------------------------------------------------- import
def test_rerunning_the_import_is_idempotent_and_throttled():
    repo = FakeRepository()
    sleeps: List[float] = []
    requests: List[Tuple[str, str, Dict[str, str]]] = []

    def fetch(method, url, form=None):
        requests.append((method, url, dict(form or {})))
        if url == FINAL_SETTLEMENT_URL:
            return SETTLEMENT_WIDE.encode("cp950")
        return DAILY_REPORT.encode("cp950") if form["queryStartDate"] == "2026/08/01" else "查無資料".encode("cp950")

    importer = TaifexOptionsImporter(repository=repo, fetcher=fetch, sleep=sleeps.append, delay=1.0)
    first = importer.import_daily(date(2026, 7, 20), date(2026, 8, 31), ["TXO"])
    importer.import_settlements(date(2026, 7, 20), date(2026, 8, 31), ["TXO"], report=first)
    snapshot = (dict(repo.daily), dict(repo.settlements))

    second = importer.import_daily(date(2026, 7, 20), date(2026, 8, 31), ["TXO"])
    importer.import_settlements(date(2026, 7, 20), date(2026, 8, 31), ["TXO"], report=second)

    assert (dict(repo.daily), dict(repo.settlements)) == snapshot
    assert len(repo.daily) == 5 and len(repo.settlements) == 2
    assert first.as_dict() == second.as_dict()
    assert first.daily_rows == 5 and first.settlements == 2 and first.empty == ["TXO 2026-07-20..2026-07-31"]
    # One request per month plus the settlement table, 1 s apart.
    assert [r[1] for r in requests[:3]] == [DAILY_OPTIONS_URL, DAILY_OPTIONS_URL, FINAL_SETTLEMENT_URL]
    assert requests[1][2]["commodity_id"] == "TXO"
    assert requests[1][2]["queryStartDate"] == "2026/08/01" and requests[1][2]["queryEndDate"] == "2026/08/31"
    assert sleeps == [1.0] * (len(requests) - 1)


def test_failed_month_is_reported_and_rerun_fills_it():
    repo = FakeRepository()
    state = {"fail": True}

    def fetch(method, url, form=None):
        if state["fail"]:
            raise ConnectionError("timeout")
        return DAILY_REPORT.encode("utf-8")

    importer = TaifexOptionsImporter(repository=repo, fetcher=fetch, sleep=lambda _: None)
    report = importer.import_daily(date(2026, 8, 1), date(2026, 8, 31), ["TXO"])
    assert report.failed and report.failed[0]["chunk"] == "TXO 2026-08-01..2026-08-31"
    assert not repo.daily
    state["fail"] = False
    assert importer.import_daily(date(2026, 8, 1), date(2026, 8, 31), ["TXO"]).failed == []
    assert len(repo.daily) == 5


# --------------------------------------------------------------------------- CLI
def _load_script():
    path = Path(__file__).resolve().parents[1] / "scripts" / "import_taifex_options_history.py"
    spec = importlib.util.spec_from_file_location("import_taifex_options_history", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_cli_imports_downloaded_files_and_validates_arguments(tmp_path, capsys):
    script = _load_script()
    daily = tmp_path / "daily.csv"
    daily.write_bytes(DAILY_REPORT.encode("cp950"))
    settlement = tmp_path / "settlement.csv"
    settlement.write_bytes(SETTLEMENT_WIDE.encode("utf-8"))
    repo = FakeRepository()
    importer = TaifexOptionsImporter(repository=repo, fetcher=lambda *a: b"", delay=0)

    code = script.run(["--daily-file", str(daily), "--settlement-file", str(settlement)], importer=importer)
    assert code == 0
    assert len(repo.daily) == 5 and len(repo.settlements) == 2
    assert '"daily_rows": 5' in capsys.readouterr().out

    assert script.run(["--daily-file", str(tmp_path / "missing.csv")], importer=importer) == 1
    with pytest.raises(SystemExit):
        script.run([], importer=importer)
    with pytest.raises(SystemExit):
        script.run(["--from", "2026-08-01"], importer=importer)
    with pytest.raises(SystemExit):
        script.run(["--from", "2026-08-31", "--to", "2026-08-01"], importer=importer)
