"""Read-only streaming client for the TAIFEX OpenAPI (``openapi.taifex.com.tw/v1``).

Shared base for the daily-bar and tick collectors. Every endpoint returns one
JSON array of flat string-valued objects. Some are large: ``TimeAndSalesData``
is about 227 MB and 920k rows, served uncompressed, so :meth:`TaifexOpenApiClient.fetch`
streams the body and decodes the array one element at a time with
``json.JSONDecoder.raw_decode``; the whole response is never held in memory.

The client only issues ``GET`` / ``HEAD`` requests to public market-data
endpoints. It never sends order or account requests.

Result contract
---------------

* HTTP error status -> :class:`TaifexOpenApiHTTPError` (``status`` attribute).
* Timeout / connection failure after the retries -> :class:`TaifexOpenApiNetworkError`.
* Body that is not a JSON array of objects (HTML error page, truncated stream,
  trailing garbage) -> :class:`TaifexOpenApiFormatError`.
* Empty array ``[]`` -> no exception; after iteration ``result.empty`` is true
  and ``result.data_date`` is ``None``.
* :meth:`TaifexOpenApiClient.probe` asks for ``Range: bytes=0-300`` and reads
  at most that many bytes (even when the server ignores the range and answers
  ``200``), so a caller learns which trading day is served, its
  ``Last-Modified`` and the total size before a full download. With
  ``if_modified_since`` an unchanged endpoint answers ``304`` and nothing is
  downloaded (``ProbeResult.not_modified``).

The data date is taken from the rows' ``Date`` field (``YYYYMMDD``), never from
the request time. ``Last-Modified`` is returned as published by the server.

Cloudflare
----------

The endpoints sit behind Cloudflare. Without a query string the edge answers
``cf-cache-status: HIT`` and can serve a copy that lags the origin by days;
with a unique query parameter it answers ``MISS`` and forwards to the origin
(observed 2026-11-03, also together with ``Range``). The ``requests`` fetcher
therefore appends ``?_=<epoch milliseconds>`` (strictly increasing within the
process) to every request, retries included. It also sends a browser
``User-Agent``: ``yaotony/Trading-Decision-System`` was blocked by Cloudflare's
TLS/HTTP fingerprinting from inside a container, receiving an HTML error page
on every request (which this client reports as a format error).

Publication timing
------------------

* Observed 2026-11-03: the origin ``Last-Modified`` of ``DailyMarketReportFut``
  is ``Sun, 01 Nov 2026 22:36:01 GMT`` (Mon 2026-11-02 06:36 Taipei) and every
  row is ``Date=20261030`` (the previous trading day, a Friday);
  ``TimeAndSalesData`` was modified 22:37:51 GMT the same night with rows
  ``Date=20261029`` (night session before midnight) and ``Date=20261030``.
  Data of trading day D is presumably published at about 06:30 Taipei on the
  next trading day's morning, after the night session closes at 05:00.
* Night-session attribution: in ``DailyMarketReportFut`` the ``盤後`` row of
  ``Date=D`` carries the same reference price as the ``一般`` row of D (ZFF
  202611: ``Last 3780.6 / Change -19.6`` and ``Last 3793.8 / Change -6.4``,
  both against 3800.2), i.e. it is the night session that opened at 15:00 on
  the previous trading day and belongs to trading day D. ``taifex_history``
  documents the ``futDataDown`` CSV the other way round (a ``盤後`` row of D is
  the session opening on D), so collectors must not reuse that rule for the
  OpenAPI without checking. In ``TimeAndSalesData`` ``Date`` is the calendar date
  of each trade, so the night session of trading day D spans the previous
  calendar date (before midnight) and D (after midnight); the file holds one
  trading day, and ``max(Date)`` is that trading day.
* Scheduling: run once a day at about 07:30 Asia/Taipei, ``probe`` (or
  ``HEAD``) first and skip the download when ``Last-Modified`` is unchanged
  or the served trading day is already stored, and retry (e.g.
  hourly until noon) while ``data_date`` is still older than the expected
  trading day.
"""
from __future__ import annotations

import codecs
import json
import os
import re
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, time as dtime, timezone
from email.utils import format_datetime, parsedate_to_datetime
from typing import Any, Callable, Dict, Iterable, Iterator, List, Mapping, Optional, Union

from app.data_sources.tw_futures_contracts import TAIPEI_TZ, TradingDayCheck, xtai_is_trading_day
from app.utils.logger import get_logger

logger = get_logger(__name__)

DEFAULT_BASE_URL = "https://openapi.taifex.com.tw/v1"
BASE_URL_ENV = "TAIFEX_OPENAPI_BASE_URL"
REQUEST_TIMEOUT_SECONDS = 60
REQUEST_RETRIES = 2
RETRY_BACKOFF_SECONDS = 2.0
CHUNK_SIZE = 64 * 1024
# An undecodable element larger than this is malformed input, not a short read.
MAX_ELEMENT_CHARS = 1024 * 1024
# ``probe`` asks for ``Range: bytes=0-PROBE_RANGE_END`` and never reads more.
PROBE_RANGE_END = 300
# Query parameter carrying the per-request cache-bust value (see "Cloudflare").
CACHE_BUST_PARAM = "_"
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)

_ENDPOINT_RE = re.compile(r"^[A-Za-z0-9_]+$")
_DATE_RE = re.compile(r"^(\d{4})(\d{2})(\d{2})$")
_CONTENT_RANGE_RE = re.compile(r"^\s*bytes\s+[\d*-]+/(\d+)\s*$", re.IGNORECASE)
# One complete ``"key":"value"`` pair of a flat string-valued object.
_STRING_PAIR_RE = re.compile(r'"((?:[^"\\]|\\.)*)"\s*:\s*"((?:[^"\\]|\\.)*)"')
_EMPTY_VALUES = frozenset({"", "-", "NULL", "null", "--"})
_ALLOWED_METHODS = frozenset({"GET", "HEAD"})
_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})


class TaifexOpenApiError(RuntimeError):
    """Base class for TAIFEX OpenAPI failures."""


class TaifexOpenApiHTTPError(TaifexOpenApiError):
    def __init__(self, url: str, status: int):
        super().__init__(f"TAIFEX OpenAPI HTTP {status}: {url}")
        self.url = url
        self.status = status


class TaifexOpenApiNetworkError(TaifexOpenApiError):
    """Timeout or connection failure (after retries, or mid-stream)."""


class TaifexOpenApiFormatError(TaifexOpenApiError):
    """The body is not a JSON array of objects."""


@dataclass
class RawResponse:
    """What a fetcher returns: response headers, the body as byte chunks and the status.

    ``status`` is ``200``, ``206`` (range request honoured) or ``304`` (not
    modified, no body).
    """

    headers: Mapping[str, str]
    chunks: Iterable[bytes]
    status: int = 200


# ``fetcher(method, url)`` or ``fetcher(method, url, headers)`` with extra
# request headers (``probe`` only); must raise TaifexOpenApi*Error on failure.
Fetcher = Callable[..., RawResponse]


def default_base_url() -> str:
    return (os.getenv(BASE_URL_ENV) or "").strip().rstrip("/") or DEFAULT_BASE_URL


def parse_number(value: Any) -> Optional[float]:
    """TAIFEX numeric string -> float; ``"NULL"``, ``"-"`` and blanks -> ``None``."""
    if value is None:
        return None
    text = str(value).strip().replace(",", "").rstrip("%")
    if text in _EMPTY_VALUES:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def parse_row_date(value: Any) -> Optional[date]:
    match = _DATE_RE.match(str(value or "").strip())
    if not match:
        return None
    try:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None


def parse_last_modified(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None


def format_http_date(value: Union[datetime, str]) -> str:
    """``datetime`` (naive means UTC) -> RFC 7231 date; strings pass through unchanged."""
    if isinstance(value, str):
        return value
    moment = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    return format_datetime(moment.astimezone(timezone.utc), usegmt=True)


def row_timestamp(row: Mapping[str, Any]) -> Optional[int]:
    """``Date`` + ``TimeOfTrades`` (Asia/Taipei wall time) -> unix seconds, or None."""
    day = parse_row_date(row.get("Date"))
    digits = re.sub(r"\D", "", str(row.get("TimeOfTrades") or "").split(".", 1)[0])
    clock = digits.zfill(6)
    if day is None or not digits or len(clock) != 6:
        return None
    try:
        moment = datetime.combine(day, dtime(int(clock[:2]), int(clock[2:4]), int(clock[4:6])), tzinfo=TAIPEI_TZ)
    except ValueError:
        return None
    return int(moment.timestamp())


def row_trading_day(row: Optional[Mapping[str, Any]], is_trading_day: Optional[TradingDayCheck] = None) -> Optional[date]:
    """Trading day a row belongs to.

    Rows with ``TimeOfTrades`` (``TimeAndSalesData``) carry the calendar date of
    the trade; a night-session trade belongs to the next XTAI trading day after
    the session opened (``Date=20261029 150000`` -> 2026-10-30), the rule of
    ``app.data_sources.tw_futures.trading_day_of``. Daily-report rows have no
    trade time and their ``Date`` already is the trading day.
    """
    if not row:
        return None
    day = parse_row_date(row.get("Date"))
    ts = row_timestamp(row)
    if ts is None:
        return day
    from app.data_sources.tw_futures import trading_day_of

    return trading_day_of(ts, is_trading_day or xtai_is_trading_day) or day


def _header(headers: Mapping[str, str], name: str) -> Optional[str]:
    lowered = name.lower()
    for key, value in headers.items():
        if key.lower() == lowered:
            return value
    return None


def iter_json_array(chunks: Iterable[bytes]) -> Iterator[Dict[str, Any]]:
    """Decode a top-level JSON array of objects incrementally from byte chunks.

    Holds at most one partially received element plus one chunk in memory.
    Raises :class:`TaifexOpenApiFormatError` when the body is not such an array.
    """
    decoder = json.JSONDecoder()
    utf8 = codecs.getincrementaldecoder("utf-8-sig")(errors="strict")
    source = iter(chunks)
    buf = ""
    pos = 0
    done = False

    def more() -> bool:
        nonlocal buf, pos, done
        if done:
            return False
        for chunk in source:
            try:
                text = utf8.decode(chunk)
            except UnicodeDecodeError as exc:
                raise TaifexOpenApiFormatError(f"body is not UTF-8: {exc}") from exc
            if text:
                buf = buf[pos:] + text
                pos = 0
                return True
        try:
            tail = utf8.decode(b"", final=True)
        except UnicodeDecodeError as exc:
            raise TaifexOpenApiFormatError(f"body is not UTF-8: {exc}") from exc
        done = True
        if tail:
            buf = buf[pos:] + tail
            pos = 0
            return True
        return False

    def next_token() -> Optional[str]:
        """Skip whitespace; return the next character without consuming it."""
        nonlocal pos
        while True:
            while pos < len(buf) and buf[pos] in " \t\r\n":
                pos += 1
            if pos < len(buf):
                return buf[pos]
            if not more():
                return None

    first = next_token()
    if first is None:
        raise TaifexOpenApiFormatError("empty body")
    if first != "[":
        raise TaifexOpenApiFormatError(f"expected a JSON array, got {buf[pos:pos + 40]!r}")
    pos += 1

    expect_value = True
    first_element = True
    while True:
        token = next_token()
        if token is None:
            raise TaifexOpenApiFormatError("truncated JSON array")
        if token == "]" and (first_element or not expect_value):
            pos += 1
            break
        if not expect_value:
            if token != ",":
                raise TaifexOpenApiFormatError(f"expected ',' or ']' at {buf[pos:pos + 40]!r}")
            pos += 1
            expect_value = True
            continue
        while True:
            try:
                value, end = decoder.raw_decode(buf, pos)
                break
            except json.JSONDecodeError as exc:
                if len(buf) - pos > MAX_ELEMENT_CHARS:
                    raise TaifexOpenApiFormatError(f"invalid JSON element: {exc}") from exc
                if not more():
                    raise TaifexOpenApiFormatError(f"invalid or truncated JSON: {exc}") from exc
        if not isinstance(value, dict):
            raise TaifexOpenApiFormatError(f"array element is not an object: {type(value).__name__}")
        pos = end
        expect_value = False
        first_element = False
        yield value

    if next_token() is not None:
        raise TaifexOpenApiFormatError(f"trailing data after JSON array: {buf[pos:pos + 40]!r}")


class OpenApiResult:
    """One endpoint download. Iterate once to stream the rows.

    ``row_count``, ``data_dates``, ``data_date`` and ``empty`` are filled in
    while iterating and are final once ``complete`` is true.
    """

    def __init__(self, endpoint: str, url: str, response: RawResponse):
        self.endpoint = endpoint
        self.url = url
        self.headers: Mapping[str, str] = response.headers
        self.last_modified_raw: Optional[str] = _header(response.headers, "Last-Modified")
        self.last_modified: Optional[datetime] = parse_last_modified(self.last_modified_raw)
        self.row_count = 0
        self.complete = False
        self._dates: set = set()
        self._chunks = response.chunks
        self._started = False

    def __iter__(self) -> Iterator[Dict[str, Any]]:
        if self._started:
            raise RuntimeError("OpenApiResult rows can be iterated only once")
        self._started = True
        return self._rows()

    def _rows(self) -> Iterator[Dict[str, Any]]:
        for row in iter_json_array(self._chunks):
            self.row_count += 1
            day = parse_row_date(row.get("Date"))
            if day is not None:
                self._dates.add(day)
            yield row
        self.complete = True

    @property
    def data_dates(self) -> List[date]:
        return sorted(self._dates)

    @property
    def data_date(self) -> Optional[date]:
        """Latest ``Date`` seen in the rows (the trading day for daily files)."""
        return max(self._dates) if self._dates else None

    @property
    def empty(self) -> bool:
        return self.complete and self.row_count == 0

    def close(self) -> None:
        """Release the underlying stream (drops the connection of an unfinished download)."""
        close = getattr(self._chunks, "close", None)
        if callable(close):
            close()


@dataclass
class ProbeResult:
    """Head of an endpoint, read without downloading the rest of the body.

    * ``first_row``: the first array element. When it is longer than the probe
      range it holds only the leading string fields that arrived complete
      (``Date`` is always the first field of the TAIFEX feeds).
    * ``first_date``: the raw ``Date`` of that row (for ``TimeAndSalesData``
      possibly the calendar day before, when the night session leads).
    * ``trading_day``: the trading day the response holds, see
      :func:`row_trading_day`.
    * ``last_modified``: the server's ``Last-Modified``; on ``304`` without
      that header, the ``If-Modified-Since`` value that was confirmed.
    * ``size``: the total body size in bytes (``Content-Range`` total, or
      ``Content-Length`` when the server answered ``200``).
    * ``not_modified``: the server answered ``304`` to ``If-Modified-Since``;
      the row fields are then ``None``.
    """

    endpoint: str
    first_row: Optional[Dict[str, Any]]
    first_date: Optional[date]
    last_modified: Optional[datetime]
    size: Optional[int]
    trading_day: Optional[date] = None
    not_modified: bool = False
    status: int = 200

    @property
    def empty(self) -> bool:
        return self.first_row is None and not self.not_modified


def _content_size(headers: Mapping[str, str]) -> Optional[int]:
    text = (_header(headers, "Content-Length") or "").strip()
    return int(text) if text.isdigit() else None


def _total_size(headers: Mapping[str, str], status: int) -> Optional[int]:
    """Full body size: the ``Content-Range`` total of a ``206``, else ``Content-Length``."""
    match = _CONTENT_RANGE_RE.match(_header(headers, "Content-Range") or "")
    if match:
        return int(match.group(1))
    return _content_size(headers) if status == 200 else None


def _read_prefix(chunks: Iterable[bytes], limit: int) -> bytes:
    """At most ``limit`` bytes from the start of ``chunks``; closes the stream."""
    buf = bytearray()
    iterator = iter(chunks)
    try:
        for chunk in iterator:
            buf.extend(chunk)
            if len(buf) >= limit:
                break
    finally:
        for source in (iterator, chunks):
            close = getattr(source, "close", None)
            if callable(close):
                close()
    return bytes(buf[:limit])


def _first_row_of_prefix(prefix: bytes, truncated: bool) -> Optional[Dict[str, Any]]:
    """First object of a JSON array whose first ``len(prefix)`` bytes are given.

    Returns None for an empty array. A first object cut off by the probe range
    yields its complete leading ``"key":"value"`` pairs. Raises
    :class:`TaifexOpenApiFormatError` when the prefix is not a JSON array of
    objects (an HTML error page, for example).
    """
    try:
        # Not final: a multibyte character cut by the range end stays buffered.
        text = codecs.getincrementaldecoder("utf-8-sig")(errors="strict").decode(prefix, final=not truncated)
    except UnicodeDecodeError as exc:
        raise TaifexOpenApiFormatError(f"body is not UTF-8: {exc}") from exc
    body = text.lstrip(" \t\r\n")
    if not body:
        raise TaifexOpenApiFormatError("empty body")
    if body[0] != "[":
        raise TaifexOpenApiFormatError(f"expected a JSON array, got {body[:40]!r}")
    rest = body[1:].lstrip(" \t\r\n")
    if rest.startswith("]"):
        return None
    if not rest:
        raise TaifexOpenApiFormatError("truncated JSON array")
    if rest[0] != "{":
        raise TaifexOpenApiFormatError(f"array element is not an object: {rest[:40]!r}")
    try:
        value, _end = json.JSONDecoder().raw_decode(rest)
    except json.JSONDecodeError as exc:
        if not truncated:
            raise TaifexOpenApiFormatError(f"invalid or truncated JSON: {exc}") from exc
    else:
        if not isinstance(value, dict):
            raise TaifexOpenApiFormatError(f"array element is not an object: {type(value).__name__}")
        return value
    head = rest.split("}", 1)[0]
    row: Dict[str, Any] = {}
    for match in _STRING_PAIR_RE.finditer(head):
        try:
            row[json.loads(f'"{match.group(1)}"')] = json.loads(f'"{match.group(2)}"')
        except json.JSONDecodeError as exc:
            raise TaifexOpenApiFormatError(f"invalid JSON string in first row: {exc}") from exc
    if "Date" not in row:
        raise TaifexOpenApiFormatError(f"first row has no Date within {len(prefix)} bytes")
    return row


class TaifexOpenApiClient:
    def __init__(self, base_url: Optional[str] = None, fetcher: Optional[Fetcher] = None):
        self.base_url = (base_url or default_base_url()).rstrip("/")
        self._fetcher = fetcher

    def _fetch(self) -> Fetcher:
        if self._fetcher is None:
            self._fetcher = http_fetcher()
        return self._fetcher

    def url_for(self, endpoint: str) -> str:
        name = str(endpoint or "").strip().strip("/")
        if not _ENDPOINT_RE.match(name):
            raise ValueError(f"invalid TAIFEX OpenAPI endpoint: {endpoint!r}")
        return f"{self.base_url}/{name}"

    def fetch(self, endpoint: str) -> OpenApiResult:
        """Start a streamed ``GET``; iterate the result for the rows."""
        url = self.url_for(endpoint)
        return OpenApiResult(endpoint, url, self._fetch()("GET", url))

    def probe(
        self,
        endpoint: str,
        *,
        if_modified_since: Optional[Union[datetime, str]] = None,
        is_trading_day: Optional[TradingDayCheck] = None,
    ) -> ProbeResult:
        """Read only the head of ``endpoint`` and close the connection.

        Sends ``Range: bytes=0-300`` and reads at most those bytes, also when
        the server ignores the range and streams the whole body with ``200``
        (``TimeAndSalesData`` is about 227 MB). With ``if_modified_since`` an
        unchanged endpoint answers ``304`` and :attr:`ProbeResult.not_modified`
        is true.
        """
        url = self.url_for(endpoint)
        limit = PROBE_RANGE_END + 1
        headers = {"Range": f"bytes=0-{PROBE_RANGE_END}", "Accept-Encoding": "identity"}
        if if_modified_since is not None:
            headers["If-Modified-Since"] = format_http_date(if_modified_since)
        response = self._fetch()("GET", url, headers)
        last_modified = parse_last_modified(_header(response.headers, "Last-Modified"))
        size = _total_size(response.headers, response.status)
        if response.status == 304:
            close = getattr(response.chunks, "close", None)
            if callable(close):
                close()
            if last_modified is None and if_modified_since is not None:
                last_modified = parse_last_modified(headers["If-Modified-Since"])
            return ProbeResult(
                endpoint=endpoint, first_row=None, first_date=None, last_modified=last_modified,
                size=size, not_modified=True, status=304,
            )
        prefix = _read_prefix(response.chunks, limit)
        first = _first_row_of_prefix(prefix, truncated=len(prefix) >= limit)
        return ProbeResult(
            endpoint=endpoint,
            first_row=first,
            first_date=parse_row_date(first.get("Date")) if first is not None else None,
            last_modified=last_modified,
            size=size,
            trading_day=row_trading_day(first, is_trading_day),
            status=response.status,
        )

    def last_modified(self, endpoint: str) -> Optional[datetime]:
        """``HEAD`` the endpoint and return its ``Last-Modified`` (no body download)."""
        response = self._fetch()("HEAD", self.url_for(endpoint))
        return parse_last_modified(_header(response.headers, "Last-Modified"))


def fetch(endpoint: str, *, base_url: Optional[str] = None, fetcher: Optional[Fetcher] = None) -> OpenApiResult:
    return TaifexOpenApiClient(base_url=base_url, fetcher=fetcher).fetch(endpoint)


def http_fetcher(
    timeout: float = REQUEST_TIMEOUT_SECONDS,
    retries: int = REQUEST_RETRIES,
    backoff: float = RETRY_BACKOFF_SECONDS,
    chunk_size: int = CHUNK_SIZE,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.time,
) -> Fetcher:
    """``fetcher(method, url, headers=None)`` over ``requests`` with streaming, retries and typed errors.

    Every request (retries included) carries a browser ``User-Agent`` and a
    unique ``?_=<epoch ms>`` cache-bust parameter (see "Cloudflare" above).
    Retries cover connection failures, timeouts and 429/5xx before the body
    starts; a failure while streaming raises :class:`TaifexOpenApiNetworkError`.
    ``304`` returns an empty body with ``status=304``.
    """
    import requests

    session = requests.Session()
    session.headers.update({
        "User-Agent": BROWSER_USER_AGENT,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
    })
    bust_lock = threading.Lock()
    last_bust = 0

    def cache_bust() -> str:
        """Epoch milliseconds, strictly increasing so two requests never share a value."""
        nonlocal last_bust
        with bust_lock:
            last_bust = max(int(clock() * 1000), last_bust + 1)
            return str(last_bust)

    def stream(response: Any) -> Iterator[bytes]:
        try:
            for chunk in response.iter_content(chunk_size=chunk_size):
                if chunk:
                    yield chunk
        except (requests.Timeout, requests.ConnectionError, requests.exceptions.ChunkedEncodingError) as exc:
            raise TaifexOpenApiNetworkError(f"TAIFEX OpenAPI stream interrupted: {exc}") from exc
        finally:
            response.close()

    def fetch_once(method: str, url: str, headers: Optional[Mapping[str, str]] = None) -> RawResponse:
        verb = method.upper()
        if verb not in _ALLOWED_METHODS:
            raise ValueError(f"TAIFEX OpenAPI client is read-only; method {method!r} not allowed")
        extra = dict(headers or {})
        attempt = 0
        while True:
            try:
                response = session.request(
                    verb, url, params={CACHE_BUST_PARAM: cache_bust()}, headers=extra,
                    timeout=timeout, stream=(verb == "GET"),
                )
            except (requests.Timeout, requests.ConnectionError) as exc:
                if attempt >= retries:
                    raise TaifexOpenApiNetworkError(f"TAIFEX OpenAPI request failed: {url}: {exc}") from exc
            else:
                status = response.status_code
                if status < 400:
                    if verb == "HEAD" or status == 304:
                        response.close()
                        return RawResponse(headers=dict(response.headers), chunks=(), status=status)
                    return RawResponse(headers=dict(response.headers), chunks=stream(response), status=status)
                response.close()
                if response.status_code not in _RETRY_STATUSES or attempt >= retries:
                    raise TaifexOpenApiHTTPError(url, response.status_code)
            attempt += 1
            logger.warning("TAIFEX OpenAPI %s %s failed; retry %d/%d", verb, url, attempt, retries)
            sleep(backoff * attempt)

    return fetch_once
