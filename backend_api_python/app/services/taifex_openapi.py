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
* :meth:`TaifexOpenApiClient.probe` streams only the first row and closes the
  connection, so a caller learns which day is served before a full download.

The data date is taken from the rows' ``Date`` field (``YYYYMMDD``), never from
the request time. ``Last-Modified`` is returned as published by the server.

Publication timing (observed 2026-11-03 around 06:20 Asia/Taipei)
------------------------------------------------------------------

* ``DailyMarketReportFut``: ``Last-Modified: Sun, 01 Nov 2026 22:36:01 GMT``
  (Mon 2026-11-02 06:36 Taipei), every row ``Date=20261030`` (the previous
  trading day, a Friday). ``TimeAndSalesData``: ``Last-Modified`` 22:37:51 GMT
  the same night, rows ``Date=20261029`` (night session before midnight) and
  ``Date=20261030``. Cloudflare reported ``cf-cache-status: HIT`` with
  ``age`` of about 10-15 minutes and the origin ``Last-Modified`` unchanged,
  so the CDN is not serving a days-old copy: Monday 11-02 data was simply
  not published yet at 06:24 Taipei. The timestamps point to a batch at about
  06:35-06:40 Taipei the morning after a trading day, after the night session
  closes at 05:00 (to be confirmed by a second sample).
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
* Scheduling: run once a day at about 07:30 Asia/Taipei, ``HEAD`` first and
  skip the download when ``Last-Modified`` is unchanged, and retry (e.g.
  hourly until noon) while ``data_date`` is still older than the expected
  trading day.
"""
from __future__ import annotations

import codecs
import json
import os
import re
import time
from dataclasses import dataclass
from datetime import date, datetime
from email.utils import parsedate_to_datetime
from typing import Any, Callable, Dict, Iterable, Iterator, List, Mapping, Optional

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

_ENDPOINT_RE = re.compile(r"^[A-Za-z0-9_]+$")
_DATE_RE = re.compile(r"^(\d{4})(\d{2})(\d{2})$")
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
    """What a fetcher returns: response headers and the body as byte chunks."""

    headers: Mapping[str, str]
    chunks: Iterable[bytes]


# ``fetcher(method, url)``; must raise TaifexOpenApi*Error on failure.
Fetcher = Callable[[str, str], RawResponse]


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
    """First row of an endpoint, read without downloading the rest of the body.

    ``first_date`` is the raw ``Date`` of that row. For ``TimeAndSalesData`` the
    first row may be the night session opened on the previous trading day, so
    callers convert ``first_row`` to its trading day themselves.
    """

    endpoint: str
    first_row: Optional[Dict[str, Any]]
    first_date: Optional[date]
    last_modified: Optional[datetime]
    size: Optional[int]

    @property
    def empty(self) -> bool:
        return self.first_row is None


def _content_size(headers: Mapping[str, str]) -> Optional[int]:
    text = (_header(headers, "Content-Length") or "").strip()
    return int(text) if text.isdigit() else None


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

    def probe(self, endpoint: str) -> ProbeResult:
        """Read only the first row of ``endpoint`` and close the connection.

        Tells which day an endpoint currently serves before committing to a
        full download (``TimeAndSalesData`` is about 227 MB).
        """
        result = self.fetch(endpoint)
        rows = iter(result)
        try:
            first = next(rows, None)
        finally:
            rows.close()
            result.close()
        return ProbeResult(
            endpoint=result.endpoint,
            first_row=first,
            first_date=parse_row_date(first.get("Date")) if first is not None else None,
            last_modified=result.last_modified,
            size=_content_size(result.headers),
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
) -> Fetcher:
    """``fetcher(method, url)`` over ``requests`` with streaming, retries and typed errors.

    Retries cover connection failures, timeouts and 429/5xx before the body
    starts; a failure while streaming raises :class:`TaifexOpenApiNetworkError`.
    """
    import requests

    session = requests.Session()
    session.headers.update({"User-Agent": "QuantDinger/TAIFEX-OpenAPI", "Accept": "application/json"})

    def stream(response: Any) -> Iterator[bytes]:
        try:
            for chunk in response.iter_content(chunk_size=chunk_size):
                if chunk:
                    yield chunk
        except (requests.Timeout, requests.ConnectionError, requests.exceptions.ChunkedEncodingError) as exc:
            raise TaifexOpenApiNetworkError(f"TAIFEX OpenAPI stream interrupted: {exc}") from exc
        finally:
            response.close()

    def fetch_once(method: str, url: str) -> RawResponse:
        verb = method.upper()
        if verb not in _ALLOWED_METHODS:
            raise ValueError(f"TAIFEX OpenAPI client is read-only; method {method!r} not allowed")
        attempt = 0
        while True:
            try:
                response = session.request(verb, url, timeout=timeout, stream=(verb == "GET"))
            except (requests.Timeout, requests.ConnectionError) as exc:
                if attempt >= retries:
                    raise TaifexOpenApiNetworkError(f"TAIFEX OpenAPI request failed: {url}: {exc}") from exc
            else:
                if response.status_code < 400:
                    if verb == "HEAD":
                        response.close()
                        return RawResponse(headers=dict(response.headers), chunks=())
                    return RawResponse(headers=dict(response.headers), chunks=stream(response))
                response.close()
                if response.status_code not in _RETRY_STATUSES or attempt >= retries:
                    raise TaifexOpenApiHTTPError(url, response.status_code)
            attempt += 1
            logger.warning("TAIFEX OpenAPI %s %s failed; retry %d/%d", verb, url, attempt, retries)
            sleep(backoff * attempt)

    return fetch_once
