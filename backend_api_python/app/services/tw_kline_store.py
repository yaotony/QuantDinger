"""PostgreSQL storage for Taiwan market K-lines (``qd_tw_klines``).

Rows are market data shared by every tenant (no ``user_id``). ``bar_time`` is the
bar open instant in UTC. Writes are idempotent upserts on
``(market, symbol, session, timeframe, bar_time)``; when the same bar arrives from
several sources the ``fubon`` row wins, so a later TAIFEX import never replaces it.

``qd_tw_kline_collect_status`` records the daily collection outcome per
(market, symbol, session, trading day) so failed days are retried and days that
could no longer be collected are visible as gaps.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

MARKET_STOCK = "TWStock"
MARKET_FUTURES = "TWFutures"
SESSION_REGULAR = "regular"
SESSION_AFTERHOURS = "afterhours"
SOURCE_FUBON = "fubon"
SOURCE_TAIFEX = "taifex"

STATUS_SUCCESS = "success"
STATUS_FAILED = "failed"
STATUS_MISSED = "missed"

StatusKey = Tuple[str, str, str, date]

_MAX_ERROR_CHARS = 500


def _utc_datetime(ts: int) -> datetime:
    return datetime.fromtimestamp(int(ts), tz=timezone.utc)


def _unix(value: Any) -> int:
    if isinstance(value, datetime):
        moment = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return int(moment.timestamp())
    return int(value)


def _as_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


class TWKlineRepository:
    """Read and write ``qd_tw_klines`` and the collection status table."""

    # ------------------------------------------------------------------ K-lines
    def upsert_bars(
        self,
        *,
        market: str,
        symbol: str,
        session: str,
        timeframe: str,
        bars: Iterable[Dict[str, Any]],
        source: str,
    ) -> int:
        """Insert or update bars; returns the number of bars written."""
        from app.utils.db import get_db_connection

        rows = [
            (
                market,
                symbol,
                session,
                timeframe,
                _utc_datetime(bar["time"]),
                float(bar["open"]),
                float(bar["high"]),
                float(bar["low"]),
                float(bar["close"]),
                float(bar.get("volume") or 0),
                source,
            )
            for bar in bars
        ]
        if not rows:
            return 0
        with get_db_connection() as db:
            cur = db.cursor()
            cur.executemany(
                """
                INSERT INTO qd_tw_klines
                  (market, symbol, session, timeframe, bar_time, open, high, low, close, volume, source)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (market, symbol, session, timeframe, bar_time) DO UPDATE SET
                  open = EXCLUDED.open,
                  high = EXCLUDED.high,
                  low = EXCLUDED.low,
                  close = EXCLUDED.close,
                  volume = EXCLUDED.volume,
                  source = EXCLUDED.source,
                  updated_at = NOW()
                WHERE EXCLUDED.source = 'fubon' OR qd_tw_klines.source <> 'fubon'
                """,
                rows,
            )
            db.commit()
            cur.close()
        return len(rows)

    def load_bars(
        self,
        *,
        market: str,
        symbol: str,
        session: str,
        timeframe: str,
        start_ts: Optional[int] = None,
        end_ts: Optional[int] = None,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Return bars with ``start_ts <= time < end_ts`` sorted ascending.

        Without ``start_ts`` and with ``limit``, the latest ``limit`` bars before
        ``end_ts`` are returned.
        """
        from app.utils.db import get_db_connection

        clauses = ["market = ?", "symbol = ?", "session = ?", "timeframe = ?"]
        params: List[Any] = [market, symbol, session, timeframe]
        if start_ts is not None:
            clauses.append("bar_time >= ?")
            params.append(_utc_datetime(start_ts))
        if end_ts is not None:
            clauses.append("bar_time < ?")
            params.append(_utc_datetime(end_ts))
        sql = (
            "SELECT bar_time, open, high, low, close, volume FROM qd_tw_klines WHERE "
            + " AND ".join(clauses)
        )
        newest_first = start_ts is None and limit is not None
        sql += " ORDER BY bar_time DESC" if newest_first else " ORDER BY bar_time ASC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(max(1, int(limit)))
        with get_db_connection() as db:
            cur = db.cursor()
            cur.execute(sql, tuple(params))
            rows = cur.fetchall() or []
            cur.close()
        bars = [
            {
                "time": _unix(row["bar_time"]),
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "volume": round(float(row["volume"] or 0), 2),
            }
            for row in rows
        ]
        bars.sort(key=lambda item: item["time"])
        return bars

    def earliest_bar_time(self, *, market: str, symbol: str, session: str, timeframe: str) -> Optional[int]:
        from app.utils.db import get_db_connection

        with get_db_connection() as db:
            cur = db.cursor()
            cur.execute(
                """
                SELECT MIN(bar_time) AS earliest FROM qd_tw_klines
                WHERE market = ? AND symbol = ? AND session = ? AND timeframe = ?
                """,
                (market, symbol, session, timeframe),
            )
            row = cur.fetchone()
            cur.close()
        value = (row or {}).get("earliest")
        return _unix(value) if value is not None else None

    def earliest_bar_time_like(
        self,
        *,
        market: str,
        symbol_pattern: str,
        sessions: Sequence[str],
        timeframe: str,
    ) -> Optional[int]:
        """Earliest bar of any symbol matching the SQL LIKE ``symbol_pattern`` in ``sessions``.

        ``TXF__`` covers every stored monthly contract of a futures product.
        """
        from app.utils.db import get_db_connection

        session_list = [str(item) for item in sessions if item]
        if not session_list:
            return None
        placeholders = ", ".join("?" for _ in session_list)
        with get_db_connection() as db:
            cur = db.cursor()
            cur.execute(
                f"""
                SELECT MIN(bar_time) AS earliest FROM qd_tw_klines
                WHERE market = ? AND symbol LIKE ? AND session IN ({placeholders}) AND timeframe = ?
                """,
                (market, symbol_pattern, *session_list, timeframe),
            )
            row = cur.fetchone()
            cur.close()
        value = (row or {}).get("earliest")
        return _unix(value) if value is not None else None

    def latest_bar_time(
        self,
        *,
        market: str,
        session: str,
        timeframe: str,
        source: str,
        since: Optional[datetime] = None,
    ) -> Optional[int]:
        """Latest ``bar_time`` of any symbol for one session, timeframe and source.

        ``since`` bounds the scan to recent bars; None when nothing matches.
        """
        from app.utils.db import get_db_connection

        clauses = "market = ? AND session = ? AND timeframe = ? AND source = ?"
        params: List[Any] = [market, session, timeframe, source]
        if since is not None:
            clauses += " AND bar_time >= ?"
            params.append(since)
        with get_db_connection() as db:
            cur = db.cursor()
            cur.execute(f"SELECT MAX(bar_time) AS latest FROM qd_tw_klines WHERE {clauses}", tuple(params))
            row = cur.fetchone()
            cur.close()
        value = (row or {}).get("latest")
        return _unix(value) if value is not None else None

    # ------------------------------------------------------------------ status
    def load_statuses(self, since: date) -> Dict[StatusKey, str]:
        from app.utils.db import get_db_connection

        with get_db_connection() as db:
            cur = db.cursor()
            cur.execute(
                """
                SELECT market, symbol, session, trade_date, status
                FROM qd_tw_kline_collect_status
                WHERE trade_date >= ?
                """,
                (since,),
            )
            rows = cur.fetchall() or []
            cur.close()
        return {
            (row["market"], row["symbol"], row["session"], _as_date(row["trade_date"])): str(row["status"])
            for row in rows
        }

    def record_status(
        self,
        *,
        market: str,
        symbol: str,
        session: str,
        trade_date: date,
        status: str,
        bar_count: int = 0,
        error: str = "",
        attempted: bool = True,
    ) -> None:
        """Upsert one status row; ``attempted`` counts a fetch attempt."""
        from app.utils.db import get_db_connection

        increment = 1 if attempted else 0
        with get_db_connection() as db:
            cur = db.cursor()
            cur.execute(
                """
                INSERT INTO qd_tw_kline_collect_status
                  (market, symbol, session, trade_date, status, bar_count, attempts, last_error)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (market, symbol, session, trade_date) DO UPDATE SET
                  status = EXCLUDED.status,
                  bar_count = EXCLUDED.bar_count,
                  attempts = qd_tw_kline_collect_status.attempts + EXCLUDED.attempts,
                  last_error = EXCLUDED.last_error,
                  updated_at = NOW()
                """,
                (
                    market,
                    symbol,
                    session,
                    trade_date,
                    status,
                    int(bar_count),
                    increment,
                    str(error or "")[:_MAX_ERROR_CHARS],
                ),
            )
            db.commit()
            cur.close()

    # ------------------------------------------------------------------ maintenance
    def find_bars_at(self, *, market: str, timeframe: str, bar_times: Sequence[int]) -> List[Dict[str, Any]]:
        """Every stored bar of ``market`` / ``timeframe`` opening at one of ``bar_times``."""
        from app.utils.db import get_db_connection

        instants = sorted({int(ts) for ts in bar_times})
        if not instants:
            return []
        placeholders = ", ".join("?" for _ in instants)
        with get_db_connection() as db:
            cur = db.cursor()
            cur.execute(
                f"""
                SELECT market, symbol, session, timeframe, bar_time, open, high, low, close, volume, source
                FROM qd_tw_klines
                WHERE market = ? AND timeframe = ? AND bar_time IN ({placeholders})
                ORDER BY bar_time, symbol, session
                """,
                (market, timeframe, *[_utc_datetime(ts) for ts in instants]),
            )
            rows = cur.fetchall() or []
            cur.close()
        return [
            {
                "market": str(row["market"]),
                "symbol": str(row["symbol"]),
                "session": str(row["session"]),
                "timeframe": str(row["timeframe"]),
                "time": _unix(row["bar_time"]),
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "volume": float(row["volume"] or 0),
                "source": str(row["source"]),
            }
            for row in rows
        ]

    def delete_bars(self, keys: Iterable[Tuple[str, str, str, str, int]]) -> int:
        """Delete bars by ``(market, symbol, session, timeframe, bar_time)``; returns the keys given."""
        from app.utils.db import get_db_connection

        rows = [(m, s, sess, tf, _utc_datetime(ts)) for m, s, sess, tf, ts in keys]
        if not rows:
            return 0
        with get_db_connection() as db:
            cur = db.cursor()
            cur.executemany(
                """
                DELETE FROM qd_tw_klines
                WHERE market = ? AND symbol = ? AND session = ? AND timeframe = ? AND bar_time = ?
                """,
                rows,
            )
            db.commit()
            cur.close()
        return len(rows)

    def find_statuses(self, *, trade_dates: Sequence[date], status: str) -> List[Dict[str, Any]]:
        """Collection status rows with ``status`` on any of ``trade_dates``."""
        from app.utils.db import get_db_connection

        days = sorted(set(trade_dates))
        if not days:
            return []
        placeholders = ", ".join("?" for _ in days)
        with get_db_connection() as db:
            cur = db.cursor()
            cur.execute(
                f"""
                SELECT market, symbol, session, trade_date, status, attempts, last_error
                FROM qd_tw_kline_collect_status
                WHERE status = ? AND trade_date IN ({placeholders})
                ORDER BY trade_date, market, symbol, session
                """,
                (status, *days),
            )
            rows = cur.fetchall() or []
            cur.close()
        return [
            {
                "market": str(row["market"]),
                "symbol": str(row["symbol"]),
                "session": str(row["session"]),
                "trade_date": _as_date(row["trade_date"]),
                "status": str(row["status"]),
                "attempts": int(row["attempts"] or 0),
                "last_error": str(row["last_error"] or ""),
            }
            for row in rows
        ]

    def mark_statuses(
        self,
        keys: Iterable[StatusKey],
        *,
        from_status: str,
        to_status: str,
        note: str,
    ) -> int:
        """Move status rows still at ``from_status`` to ``to_status`` with ``note`` as the error."""
        from app.utils.db import get_db_connection

        rows = [
            (to_status, str(note or "")[:_MAX_ERROR_CHARS], market, symbol, session, trade_date, from_status)
            for market, symbol, session, trade_date in keys
        ]
        if not rows:
            return 0
        with get_db_connection() as db:
            cur = db.cursor()
            cur.executemany(
                """
                UPDATE qd_tw_kline_collect_status
                SET status = ?, last_error = ?, updated_at = NOW()
                WHERE market = ? AND symbol = ? AND session = ? AND trade_date = ? AND status = ?
                """,
                rows,
            )
            db.commit()
            cur.close()
        return len(rows)

    # ------------------------------------------------------------------ watchlist
    def watchlist_symbols(self) -> List[Tuple[str, str]]:
        """Distinct Taiwan (market, symbol) pairs on any watchlist.

        Only the symbol identity is read, to decide which market data to collect.
        """
        from app.utils.db import get_db_connection

        with get_db_connection() as db:
            cur = db.cursor()
            cur.execute(
                """
                SELECT DISTINCT market, symbol FROM qd_watchlist
                WHERE market IN (?, ?)
                ORDER BY market, symbol
                """,
                (MARKET_STOCK, MARKET_FUTURES),
            )
            rows = cur.fetchall() or []
            cur.close()
        return [(str(row["market"]), str(row["symbol"])) for row in rows]
