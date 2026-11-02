"""PostgreSQL storage for TAIFEX option daily history (issue #67).

``qd_tw_option_daily`` holds one row per (trade_date, product, expiry, strike,
right, session); ``qd_tw_option_settlement`` one final settlement price per
(product, contract_month). Both are market data shared by every tenant (no
``user_id``) and every write is an idempotent upsert on the unique key.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple

from app.services.tw_kline_store import SESSION_REGULAR

_DAILY_UPSERT = """
INSERT INTO qd_tw_option_daily
  (trade_date, product, expiry, strike, option_right, session, symbol,
   open, high, low, close, volume, settlement_price, open_interest, source)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (trade_date, product, expiry, strike, option_right, session) DO UPDATE SET
  symbol = EXCLUDED.symbol,
  open = EXCLUDED.open,
  high = EXCLUDED.high,
  low = EXCLUDED.low,
  close = EXCLUDED.close,
  volume = EXCLUDED.volume,
  settlement_price = EXCLUDED.settlement_price,
  open_interest = EXCLUDED.open_interest,
  source = EXCLUDED.source,
  updated_at = NOW()
"""

_SETTLEMENT_UPSERT = """
INSERT INTO qd_tw_option_settlement
  (product, contract_month, settlement_date, final_settlement_price, source)
VALUES (?, ?, ?, ?, ?)
ON CONFLICT (product, contract_month) DO UPDATE SET
  settlement_date = EXCLUDED.settlement_date,
  final_settlement_price = EXCLUDED.final_settlement_price,
  source = EXCLUDED.source,
  updated_at = NOW()
"""


def _as_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def _float(value: Any) -> Optional[float]:
    return None if value is None else float(value)


def _daily_params(row: Any) -> Tuple[Any, ...]:
    return (
        row.trade_date, row.product, row.expiry, float(row.strike), row.right, row.session, row.symbol,
        row.open, row.high, row.low, row.close, float(row.volume or 0),
        row.settlement_price, row.open_interest, row.source,
    )


class TWOptionRepository:
    """Read and write ``qd_tw_option_daily`` and ``qd_tw_option_settlement``."""

    def upsert_daily(self, rows: Iterable[Any]) -> int:
        """Insert or update parsed :class:`OptionDailyRow` items; returns rows written."""
        from app.utils.db import get_db_connection

        # Duplicate keys inside one batch would make a single statement ambiguous;
        # the last occurrence wins, as a re-run would.
        unique: Dict[Tuple[Any, ...], Tuple[Any, ...]] = {}
        for row in rows:
            params = _daily_params(row)
            unique[params[:6]] = params
        if not unique:
            return 0
        with get_db_connection() as db:
            cur = db.cursor()
            cur.executemany(_DAILY_UPSERT, list(unique.values()))
            db.commit()
            cur.close()
        return len(unique)

    def upsert_settlements(self, rows: Iterable[Any]) -> int:
        from app.utils.db import get_db_connection

        unique: Dict[Tuple[str, str], Tuple[Any, ...]] = {}
        for row in rows:
            unique[(row.product, row.contract_month)] = (
                row.product, row.contract_month, row.settlement_date, float(row.price), row.source,
            )
        if not unique:
            return 0
        with get_db_connection() as db:
            cur = db.cursor()
            cur.executemany(_SETTLEMENT_UPSERT, list(unique.values()))
            db.commit()
            cur.close()
        return len(unique)

    def load_daily(self, *, product: str, start: date, end: date,
                   session: str = SESSION_REGULAR) -> List[Dict[str, Any]]:
        """Rows of ``product`` / ``session`` with ``start <= trade_date <= end``."""
        from app.utils.db import get_db_connection

        with get_db_connection() as db:
            cur = db.cursor()
            cur.execute(
                """
                SELECT trade_date, expiry, strike, option_right, close, volume,
                       settlement_price, open_interest
                FROM qd_tw_option_daily
                WHERE product = ? AND session = ? AND trade_date >= ? AND trade_date <= ?
                ORDER BY trade_date, expiry, strike, option_right
                """,
                (product, session, start, end),
            )
            rows = cur.fetchall() or []
            cur.close()
        return [
            {
                "trade_date": _as_date(row["trade_date"]),
                "expiry": str(row["expiry"]),
                "strike": float(row["strike"]),
                "right": str(row["option_right"]),
                "close": _float(row["close"]),
                "volume": float(row["volume"] or 0),
                "settlement": _float(row["settlement_price"]),
                "open_interest": _float(row["open_interest"]),
            }
            for row in rows
        ]

    def load_settlements(self, *, product: str) -> Dict[str, Tuple[date, float]]:
        """``{contract_month: (settlement_date, final_settlement_price)}``."""
        from app.utils.db import get_db_connection

        with get_db_connection() as db:
            cur = db.cursor()
            cur.execute(
                """
                SELECT contract_month, settlement_date, final_settlement_price
                FROM qd_tw_option_settlement WHERE product = ?
                """,
                (product,),
            )
            rows = cur.fetchall() or []
            cur.close()
        return {
            str(row["contract_month"]): (_as_date(row["settlement_date"]), float(row["final_settlement_price"]))
            for row in rows
        }
