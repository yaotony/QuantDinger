"""TAIFEX OpenAPI streaming client (issue #76).

No test touches the network: the fetcher is an injected fake, and the
``requests``-based fetcher runs against a fake session.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from typing import Any, Dict, Iterator, List, Optional

import pytest
import requests

from app.services import taifex_openapi
from app.services.taifex_openapi import (
    DEFAULT_BASE_URL,
    RawResponse,
    TaifexOpenApiClient,
    TaifexOpenApiFormatError,
    TaifexOpenApiHTTPError,
    TaifexOpenApiNetworkError,
    http_fetcher,
    iter_json_array,
    parse_number,
    parse_row_date,
)

# Shaped like the real feeds: a regular and a night (盤後) row, a calendar
# spread month with an escaped slash, "NULL" / "-" values, and tick rows whose
# night session spans the previous calendar date.
DAILY_FIXTURE = (
    '﻿[{"Date":"20261030","Contract":"TX","ContractMonth(Week)":"202611","Open":"23000",'
    '"High":"23100","Low":"22900","Last":"23050","Change":"-50","%":"-0.22%","Volume":"81234",'
    '"SettlementPrice":"23040","OpenInterest":"60123","TradingSession":"一般"},\n'
    '{"Date":"20261030","Contract":"TX","ContractMonth(Week)":"202611","Open":"23120",'
    '"High":"23180","Low":"23010","Last":"23060","Change":"-40","%":"-0.17%","Volume":"40210",'
    '"SettlementPrice":"NULL","OpenInterest":"-","TradingSession":"盤後"},\n'
    '{"Date":"20261030","Contract":"TX","ContractMonth(Week)":"202611\\/202612","Open":"-",'
    '"High":"-","Low":"-","Last":"35","Change":"-","%":"-","Volume":"12",'
    '"SettlementPrice":"NULL","OpenInterest":"-","TradingSession":"一般"}]'
)
TICK_FIXTURE = (
    '[{"Date":"20261029","ProductCode":"TX","ContractMonth(Week)":"202611","TimeOfTrades":"204051",'
    '"TradePrice":"23110","Volume(Buy+Sell)":"2","PriceforNearerDeliveryMonthContract":"",'
    '"PriceforFurtherDeliveryMonthContract":"","OpeningCallAuction":" "},\n'
    '{"Date":"20261030","ProductCode":"TX","ContractMonth(Week)":"202611","TimeOfTrades":"013000",'
    '"TradePrice":"23090","Volume(Buy+Sell)":"4","PriceforNearerDeliveryMonthContract":"",'
    '"PriceforFurtherDeliveryMonthContract":"","OpeningCallAuction":" "},\n'
    '{"Date":"20261030","ProductCode":"TX","ContractMonth(Week)":"202611/202612","TimeOfTrades":"084500",'
    '"TradePrice":"35","Volume(Buy+Sell)":"2","PriceforNearerDeliveryMonthContract":"23000",'
    '"PriceforFurtherDeliveryMonthContract":"23035","OpeningCallAuction":"*"}]'
)
LAST_MODIFIED = "Sun, 01 Nov 2026 22:36:01 GMT"


def _chunks(text: str, size: int) -> List[bytes]:
    data = text.encode("utf-8")
    return [data[i:i + size] for i in range(0, len(data), size)]


class _Feeder:
    """Byte-chunk source that records how far the parser has read."""

    def __init__(self, chunks: List[bytes]):
        self._chunks = chunks
        self.consumed = 0

    def __iter__(self) -> Iterator[bytes]:
        for chunk in self._chunks:
            self.consumed += 1
            yield chunk


def _fake_fetcher(body: str, *, size: int = 7, headers: Optional[Dict[str, str]] = None):
    calls: List[tuple] = []

    def fetch(method: str, url: str) -> RawResponse:
        calls.append((method, url))
        sent = {"Last-Modified": LAST_MODIFIED} if headers is None else headers
        return RawResponse(headers=sent, chunks=_chunks(body, size))

    fetch.calls = calls  # type: ignore[attr-defined]
    return fetch


@pytest.mark.parametrize("fixture", [DAILY_FIXTURE, TICK_FIXTURE])
@pytest.mark.parametrize("size", [1, 2, 3, 7, 64, 100000])
def test_streamed_rows_match_full_parse(fixture: str, size: int):
    expected = json.loads(fixture.lstrip("﻿"))
    assert list(iter_json_array(_chunks(fixture, size))) == expected


def test_parser_reads_incrementally():
    rows = [{"Date": "20261030", "Contract": "TX", "Seq": str(i)} for i in range(200)]
    feeder = _Feeder(_chunks(json.dumps(rows), 16))
    stream = iter_json_array(feeder)
    first = next(stream)
    assert first == rows[0]
    # Only the chunks covering the first element were pulled, not the whole body.
    assert feeder.consumed < 10 < len(feeder._chunks)
    assert list(stream) == rows[1:]
    assert feeder.consumed == len(feeder._chunks)


def test_multibyte_characters_split_across_chunks():
    rows = list(iter_json_array(_chunks(DAILY_FIXTURE, 1)))
    assert [row["TradingSession"] for row in rows] == ["一般", "盤後", "一般"]
    assert rows[2]["ContractMonth(Week)"] == "202611/202612"


@pytest.mark.parametrize(
    "body",
    [
        "<html>502 Bad Gateway</html>",
        '{"message": "not found"}',
        "",
        "   ",
        '[{"Date":"20261030"},',  # truncated
        '[{"Date":"20261030"}',  # missing closing bracket
        '[{"Date":"20261030"},]',  # trailing comma
        '[{"Date":"20261030"}] trailing',
        '[{"Date":"20261030"} {"Date":"20261030"}]',
        "[1, 2]",
    ],
)
def test_non_json_or_malformed_body_raises_format_error(body: str):
    with pytest.raises(TaifexOpenApiFormatError):
        list(iter_json_array(_chunks(body, 3) or [b""]))


def test_invalid_utf8_raises_format_error():
    with pytest.raises(TaifexOpenApiFormatError):
        list(iter_json_array([b'[{"a":"\xff"}]']))


def test_fetch_reports_last_modified_and_row_dates():
    fetcher = _fake_fetcher(TICK_FIXTURE)
    client = TaifexOpenApiClient(base_url="https://example.test/v1/", fetcher=fetcher)
    result = client.fetch("TimeAndSalesData")
    assert fetcher.calls == [("GET", "https://example.test/v1/TimeAndSalesData")]
    assert result.last_modified == datetime(2026, 11, 1, 22, 36, 1, tzinfo=timezone.utc)
    assert result.last_modified_raw == LAST_MODIFIED
    assert not result.complete
    rows = list(result)
    assert len(rows) == 3 and result.row_count == 3
    assert result.complete and not result.empty
    # Night-session trades before midnight carry the previous calendar date.
    assert result.data_dates == [date(2026, 10, 29), date(2026, 10, 30)]
    assert result.data_date == date(2026, 10, 30)
    with pytest.raises(RuntimeError):
        iter(result)


def test_data_date_comes_from_rows_not_request_time():
    result = taifex_openapi.fetch("DailyMarketReportFut", fetcher=_fake_fetcher(DAILY_FIXTURE))
    list(result)
    assert result.data_date == date(2026, 10, 30)


def test_empty_array_is_a_recognisable_result():
    result = taifex_openapi.fetch("DailyMarketReportFut", fetcher=_fake_fetcher(" [ ] ", headers={}))
    assert list(result) == []
    assert result.empty and result.complete
    assert result.data_date is None and result.data_dates == []
    assert result.last_modified is None


def test_last_modified_uses_head():
    fetcher = _fake_fetcher("", headers={"last-modified": LAST_MODIFIED})
    client = TaifexOpenApiClient(fetcher=fetcher)
    assert client.last_modified("DailyMarketReportFut") == datetime(2026, 11, 1, 22, 36, 1, tzinfo=timezone.utc)
    assert fetcher.calls == [("HEAD", f"{DEFAULT_BASE_URL}/DailyMarketReportFut")]


def test_base_url_env_override(monkeypatch):
    monkeypatch.setenv("TAIFEX_OPENAPI_BASE_URL", "https://mirror.example/v1/")
    assert TaifexOpenApiClient(fetcher=_fake_fetcher("[]")).url_for("X") == "https://mirror.example/v1/X"
    monkeypatch.delenv("TAIFEX_OPENAPI_BASE_URL")
    assert TaifexOpenApiClient(fetcher=_fake_fetcher("[]")).url_for("X") == f"{DEFAULT_BASE_URL}/X"


@pytest.mark.parametrize("endpoint", ["", "../admin", "a/b", "X?y=1", "http://evil"])
def test_invalid_endpoint_rejected(endpoint: str):
    with pytest.raises(ValueError):
        TaifexOpenApiClient(fetcher=_fake_fetcher("[]")).url_for(endpoint)


def test_value_helpers():
    assert parse_number("NULL") is None
    assert parse_number("-") is None
    assert parse_number("") is None
    assert parse_number(" ") is None
    assert parse_number("3790.2") == 3790.2
    assert parse_number("-0.52%") == -0.52
    assert parse_number("1,234") == 1234.0
    assert parse_row_date("20261030") == date(2026, 10, 30)
    assert parse_row_date("20261332") is None
    assert parse_row_date("2026-10-30") is None


# --- requests-based fetcher -------------------------------------------------


class _FakeResponse:
    def __init__(self, status: int, body: bytes = b"", headers: Optional[Dict[str, str]] = None, fail_after: int = -1):
        self.status_code = status
        self.headers = headers or {}
        self._body = body
        self._fail_after = fail_after
        self.closed = False
        self.bytes_read = 0

    def iter_content(self, chunk_size: int):
        for index, start in enumerate(range(0, len(self._body), chunk_size)):
            if index == self._fail_after:
                raise requests.exceptions.ChunkedEncodingError("connection reset")
            if self.closed:
                raise AssertionError("read after close")
            chunk = self._body[start:start + chunk_size]
            self.bytes_read += len(chunk)
            yield chunk

    def close(self):
        self.closed = True


class _FakeSession:
    def __init__(self, outcomes: List[Any]):
        self.headers: Dict[str, str] = {}
        self.outcomes = outcomes
        self.requests: List[tuple] = []
        self.params: List[Dict[str, str]] = []
        self.sent_headers: List[Dict[str, str]] = []

    def request(self, method: str, url: str, *, params=None, headers=None, timeout: float, stream: bool):
        self.requests.append((method, url, timeout, stream))
        self.params.append(dict(params or {}))
        self.sent_headers.append({**self.headers, **(headers or {})})
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _install_session(monkeypatch, outcomes: List[Any]) -> _FakeSession:
    session = _FakeSession(outcomes)
    monkeypatch.setattr(requests, "Session", lambda: session)
    return session


def test_http_fetcher_streams_with_timeout(monkeypatch):
    body = TICK_FIXTURE.encode("utf-8")
    session = _install_session(monkeypatch, [_FakeResponse(200, body, {"Last-Modified": LAST_MODIFIED})])
    fetcher = http_fetcher(timeout=12, chunk_size=5, sleep=lambda _s: None)
    result = TaifexOpenApiClient(base_url="https://example.test/v1", fetcher=fetcher).fetch("TimeAndSalesData")
    assert list(result) == json.loads(TICK_FIXTURE)
    assert session.requests == [("GET", "https://example.test/v1/TimeAndSalesData", 12, True)]
    assert result.data_date == date(2026, 10, 30)


def test_http_fetcher_cache_bust_is_unique_per_request_and_ua_is_a_browser(monkeypatch):
    outcomes = [requests.Timeout("slow"), _FakeResponse(200, b"[]"), _FakeResponse(200, b"[]"),
                _FakeResponse(200, headers={"Last-Modified": LAST_MODIFIED})]
    session = _install_session(monkeypatch, outcomes)
    # A frozen clock: values must still differ between requests (and retries).
    fetcher = http_fetcher(sleep=lambda _s: None, clock=lambda: 1793658000.123)
    client = TaifexOpenApiClient(fetcher=fetcher)
    list(client.fetch("DailyMarketReportFut"))  # timeout + retry
    list(client.fetch("DailyMarketReportFut"))
    client.last_modified("DailyMarketReportFut")
    busts = [params[taifex_openapi.CACHE_BUST_PARAM] for params in session.params]
    assert len(busts) == 4 and len(set(busts)) == 4
    assert busts[0] == "1793658000123" and all(value.isdigit() for value in busts)
    for headers in session.sent_headers:
        agent = headers["User-Agent"]
        assert agent == taifex_openapi.BROWSER_USER_AGENT
        assert agent.startswith("Mozilla/5.0") and "Chrome/" in agent and "QuantDinger" not in agent


def test_http_fetcher_cache_bust_follows_the_clock(monkeypatch):
    times = iter([100.0, 100.5])
    session = _install_session(monkeypatch, [_FakeResponse(200, b"[]"), _FakeResponse(200, b"[]")])
    fetcher = http_fetcher(sleep=lambda _s: None, clock=lambda: next(times))
    fetcher("GET", f"{DEFAULT_BASE_URL}/X")
    fetcher("GET", f"{DEFAULT_BASE_URL}/X")
    assert [params["_"] for params in session.params] == ["100000", "100500"]


# --- probe --------------------------------------------------------------------

TICK_SIZE = 227_000_000


def _ranged_session(monkeypatch, body: str, *, status: int = 206, headers: Optional[Dict[str, str]] = None):
    data = body.encode("utf-8")
    if status == 206:
        sent = {"Content-Range": f"bytes 0-300/{TICK_SIZE}", "Content-Length": str(min(len(data), 301)),
                "Last-Modified": LAST_MODIFIED}
        data = data[:301]
    else:
        sent = {"Content-Length": str(len(data)), "Last-Modified": LAST_MODIFIED}
    sent.update(headers or {})
    response = _FakeResponse(status, data, sent)
    return _install_session(monkeypatch, [response]), response


def test_probe_partial_content_reads_trading_day_size_and_last_modified(monkeypatch):
    session, response = _ranged_session(monkeypatch, TICK_FIXTURE)
    fetcher = http_fetcher(sleep=lambda _s: None)
    probe = TaifexOpenApiClient(fetcher=fetcher).probe("TimeAndSalesData", is_trading_day=lambda d: d.weekday() < 5)
    sent = session.sent_headers[0]
    assert sent["Range"] == "bytes=0-300" and sent["Accept-Encoding"] == "identity"
    assert "If-Modified-Since" not in sent
    assert session.params[0]["_"].isdigit()
    assert probe.status == 206 and not probe.not_modified and not probe.empty
    assert probe.size == TICK_SIZE
    assert probe.last_modified == datetime(2026, 11, 1, 22, 36, 1, tzinfo=timezone.utc)
    # The first trade is the night session opened 10/29 20:40, part of trading day 10/30.
    assert probe.first_date == date(2026, 10, 29)
    assert probe.trading_day == date(2026, 10, 30)
    assert response.closed


def test_probe_first_row_cut_by_the_range_keeps_complete_fields():
    # A daily-report row with Chinese text is longer than 301 bytes.
    first = json.loads(DAILY_FIXTURE.lstrip("﻿"))[0]
    first["Contract"] = "臺股期貨" * 20
    body = json.dumps([first], ensure_ascii=False).encode("utf-8")
    assert len(body) > 301

    def fetch(method, url, headers=None):
        return RawResponse(headers={"Content-Range": f"bytes 0-300/{len(body)}"}, chunks=[body[:301]], status=206)

    probe = TaifexOpenApiClient(fetcher=fetch).probe("DailyMarketReportFut")
    assert probe.first_row["Date"] == "20261030"
    assert "TradingSession" not in probe.first_row  # beyond the range
    assert probe.first_date == probe.trading_day == date(2026, 10, 30)  # daily rows: Date is the trading day
    assert probe.size == len(body)


def test_probe_not_modified_downloads_nothing(monkeypatch):
    response = _FakeResponse(304, b"", {})
    session = _install_session(monkeypatch, [response])
    fetcher = http_fetcher(sleep=lambda _s: None)
    since = datetime(2026, 11, 1, 22, 36, 1, tzinfo=timezone.utc)
    probe = TaifexOpenApiClient(fetcher=fetcher).probe("TimeAndSalesData", if_modified_since=since)
    assert session.sent_headers[0]["If-Modified-Since"] == LAST_MODIFIED
    assert probe.not_modified and probe.status == 304 and not probe.empty
    assert probe.first_row is None and probe.trading_day is None
    assert probe.last_modified == since
    assert response.closed and response.bytes_read == 0


def test_probe_if_modified_since_still_reads_when_changed(monkeypatch):
    session, _ = _ranged_session(monkeypatch, DAILY_FIXTURE, headers={"Last-Modified": "Mon, 02 Nov 2026 22:36:01 GMT"})
    fetcher = http_fetcher(sleep=lambda _s: None)
    probe = TaifexOpenApiClient(fetcher=fetcher).probe("DailyMarketReportFut", if_modified_since=LAST_MODIFIED)
    assert session.sent_headers[0]["If-Modified-Since"] == LAST_MODIFIED
    assert not probe.not_modified and probe.trading_day == date(2026, 10, 30)
    assert probe.last_modified == datetime(2026, 11, 2, 22, 36, 1, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    "row, expected",
    [
        ({"Date": "20261029", "TimeOfTrades": "150000"}, date(2026, 10, 30)),  # night before midnight
        ({"Date": "20261030", "TimeOfTrades": "013000"}, date(2026, 10, 30)),  # night after midnight
        ({"Date": "20261030", "TimeOfTrades": "084500"}, date(2026, 10, 30)),  # day session
        ({"Date": "20261030", "TimeOfTrades": "150000"}, date(2026, 11, 2)),  # Friday night -> Monday
        ({"Date": "20261022", "TimeOfTrades": "150000"}, date(2026, 10, 27)),  # skips 10/23, 10/26 holidays
        ({"Date": "20261030"}, date(2026, 10, 30)),  # daily report row
    ],
)
def test_probe_converts_the_first_row_to_its_trading_day(row, expected):
    holidays = {date(2026, 10, 23), date(2026, 10, 26)}

    def fetch(method, url, headers=None):
        return RawResponse(headers={}, chunks=_chunks(json.dumps([row]), 5), status=206)

    probe = TaifexOpenApiClient(fetcher=fetch).probe(
        "TimeAndSalesData", is_trading_day=lambda d: d.weekday() < 5 and d not in holidays,
    )
    assert probe.trading_day == expected
    assert probe.first_date == taifex_openapi.parse_row_date(row["Date"])


def test_probe_when_server_ignores_range_reads_only_the_head(monkeypatch):
    rows = json.loads(TICK_FIXTURE) * 5000  # about 1.2 MB
    body = json.dumps(rows)
    _session, response = _ranged_session(monkeypatch, body, status=200)
    fetcher = http_fetcher(chunk_size=64, sleep=lambda _s: None)
    probe = TaifexOpenApiClient(fetcher=fetcher).probe("TimeAndSalesData")
    assert probe.status == 200 and probe.size == len(body.encode("utf-8"))
    assert probe.trading_day == date(2026, 10, 30)
    assert response.closed
    assert response.bytes_read <= 301 + 64  # stopped after the probe range, not the whole body


def test_probe_with_a_fetcher_ignoring_range_stops_the_stream():
    feeder = _Feeder(_chunks(json.dumps(json.loads(TICK_FIXTURE) * 100), 16))
    closed: List[bool] = []

    def stream():
        try:
            yield from feeder
        finally:
            closed.append(True)

    def fetch(method, url, headers=None):
        assert headers["Range"] == "bytes=0-300"
        return RawResponse(headers={}, chunks=stream(), status=200)

    probe = TaifexOpenApiClient(fetcher=fetch).probe("TimeAndSalesData")
    assert probe.first_date == date(2026, 10, 29)
    assert feeder.consumed == 19  # ceil(301 / 16)
    assert closed == [True]


@pytest.mark.parametrize("body", ["<html><body>Attention Required! | Cloudflare</body></html>", "", '{"a":"b"}', "[1]"])
def test_probe_non_json_head_is_a_format_error(body: str):
    def fetch(method, url, headers=None):
        return RawResponse(headers={}, chunks=_chunks(body, 4), status=200)

    with pytest.raises(TaifexOpenApiFormatError):
        TaifexOpenApiClient(fetcher=fetch).probe("TimeAndSalesData")


def test_probe_of_an_empty_array():
    def fetch(method, url, headers=None):
        return RawResponse(headers={"Content-Range": "bytes 0-1/2"}, chunks=[b"[]"], status=206)

    probe = TaifexOpenApiClient(fetcher=fetch).probe("TimeAndSalesData")
    assert probe.empty and probe.first_date is None and probe.trading_day is None and probe.size == 2


def test_format_http_date():
    assert taifex_openapi.format_http_date(datetime(2026, 11, 1, 22, 36, 1)) == LAST_MODIFIED
    taipei = datetime(2026, 11, 2, 6, 36, 1, tzinfo=taifex_openapi.TAIPEI_TZ)
    assert taifex_openapi.format_http_date(taipei) == LAST_MODIFIED
    assert taifex_openapi.format_http_date(LAST_MODIFIED) == LAST_MODIFIED


def test_http_fetcher_retries_then_succeeds(monkeypatch):
    sleeps: List[float] = []
    session = _install_session(
        monkeypatch,
        [requests.Timeout("slow"), _FakeResponse(503), _FakeResponse(200, b"[]")],
    )
    fetcher = http_fetcher(retries=2, backoff=1.0, sleep=sleeps.append)
    result = TaifexOpenApiClient(fetcher=fetcher).fetch("DailyMarketReportFut")
    assert list(result) == [] and result.empty
    assert len(session.requests) == 3
    assert sleeps == [1.0, 2.0]


def test_http_fetcher_http_error_is_typed(monkeypatch):
    _install_session(monkeypatch, [_FakeResponse(404)])
    fetcher = http_fetcher(sleep=lambda _s: None)
    with pytest.raises(TaifexOpenApiHTTPError) as excinfo:
        TaifexOpenApiClient(fetcher=fetcher).fetch("Nope")
    assert excinfo.value.status == 404


def test_http_fetcher_gives_up_after_retries(monkeypatch):
    _install_session(monkeypatch, [requests.ConnectionError("down")] * 3)
    fetcher = http_fetcher(retries=2, sleep=lambda _s: None)
    with pytest.raises(TaifexOpenApiNetworkError):
        TaifexOpenApiClient(fetcher=fetcher).fetch("DailyMarketReportFut")


def test_http_fetcher_persistent_5xx_is_http_error(monkeypatch):
    _install_session(monkeypatch, [_FakeResponse(502)] * 2)
    fetcher = http_fetcher(retries=1, sleep=lambda _s: None)
    with pytest.raises(TaifexOpenApiHTTPError) as excinfo:
        TaifexOpenApiClient(fetcher=fetcher).fetch("DailyMarketReportFut")
    assert excinfo.value.status == 502


def test_http_fetcher_mid_stream_failure_is_network_error(monkeypatch):
    response = _FakeResponse(200, TICK_FIXTURE.encode("utf-8"), fail_after=3)
    _install_session(monkeypatch, [response])
    fetcher = http_fetcher(chunk_size=8, sleep=lambda _s: None)
    result = TaifexOpenApiClient(fetcher=fetcher).fetch("TimeAndSalesData")
    with pytest.raises(TaifexOpenApiNetworkError):
        list(result)
    assert response.closed and not result.complete


def test_http_fetcher_is_read_only(monkeypatch):
    session = _install_session(monkeypatch, [])
    fetcher = http_fetcher(sleep=lambda _s: None)
    for method in ("POST", "PUT", "DELETE", "PATCH"):
        with pytest.raises(ValueError):
            fetcher(method, f"{DEFAULT_BASE_URL}/DailyMarketReportFut")
    assert session.requests == []


def test_http_fetcher_head_does_not_stream(monkeypatch):
    response = _FakeResponse(200, headers={"Last-Modified": LAST_MODIFIED})
    session = _install_session(monkeypatch, [response])
    fetcher = http_fetcher(sleep=lambda _s: None)
    assert TaifexOpenApiClient(fetcher=fetcher).last_modified("TimeAndSalesData") is not None
    assert session.requests[0][0] == "HEAD" and session.requests[0][3] is False
    assert response.closed
