"""Watchlist business logic."""

import re
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from typing import Optional

from app.data.market_symbols_seed import get_symbol_name as seed_get_symbol_name
from app.services.market.symbol_search import find_market_symbol
from app.services.symbol_name import normalize_crypto_symbol, persist_seed_name, resolve_symbol_name
from app.services.market_context import MarketContext, SUPPORTED_CRYPTO_EXCHANGE_IDS
from app.utils.db import get_db_connection
from app.utils.logger import get_logger

logger = get_logger(__name__)

VALID_MARKETS = frozenset({
    "Crypto", "USStock", "CNStock", "HKStock", "Forex", "Futures", "MOEX", "TWStock", "TWFutures",
})
# Continuous near month (TXF) or one contract month (TXFH6).
TW_FUTURES_PATTERN = re.compile(r"^[A-Z]{3}([A-L]\d)?$")
CN_A_SHARE_PATTERN = re.compile(r"^\d{6}$")
TW_STOCK_PATTERN = re.compile(r"^(\d{4}[0-9A-Z]{0,2}\.TWO?|IX\d{4})$")

_name_resolve_executor = ThreadPoolExecutor(
    max_workers=4,
    thread_name_prefix="watchlist-name-resolve",
)


def normalize_symbol(symbol: str) -> str:
    return (symbol or "").strip().upper()


def validate_watchlist_pair(market: str, symbol: str) -> Optional[str]:
    """Validate a market/symbol pair before persisting it."""
    if market not in VALID_MARKETS:
        return f"Unsupported market '{market}'. Must be one of: {', '.join(sorted(VALID_MARKETS))}"
    if not symbol:
        return "Empty symbol"
    if CN_A_SHARE_PATTERN.match(symbol) and market != "CNStock":
        return f"Symbol '{symbol}' looks like a Chinese A-share code; market must be CNStock, not {market}"
    if symbol.endswith(".HK") and market != "HKStock":
        return f"Symbol '{symbol}' looks like a Hong Kong stock; market must be HKStock, not {market}"
    if symbol.upper().endswith((".TW", ".TWO")) and market != "TWStock":
        return f"Symbol '{symbol}' looks like a Taiwan stock; market must be TWStock, not {market}"
    if market == "TWStock" and not TW_STOCK_PATTERN.match(symbol.upper()):
        return (
            f"Taiwan symbol '{symbol}' must be 2330.TW (TWSE), 6488.TWO (TPEx) "
            "or an index such as IX0001."
        )
    if market == "TWFutures" and not TW_FUTURES_PATTERN.match(symbol.upper()):
        return (
            f"Taiwan futures symbol '{symbol}' must be a product code such as TXF "
            "(continuous near month) or a contract month such as TXFH6."
        )
    if market == "Crypto" and "/" not in symbol:
        return f"Crypto symbol '{symbol}' must be a BASE/QUOTE pair (e.g. BTC/USDT)."
    return None


def list_watchlist(user_id: int) -> list:
    """Return watchlist rows with their exact market-data identity."""
    with get_db_connection() as db:
        cur = db.cursor()
        cur.execute(
            """
            SELECT w.id, w.market, w.symbol, w.name, w.exchange_id, w.market_type,
                   w.instrument_id, w.settle_currency,
                   COALESCE(s.asset_class, '') AS asset_class,
                   COALESCE(s.product_type, '') AS product_type,
                   COALESCE(s.api_family, '') AS api_family,
                   COALESCE(s.underlying_market, '') AS underlying_market,
                   COALESCE(s.underlying_symbol, '') AS underlying_symbol,
                   COALESCE(s.product_meta, '{}'::jsonb) AS product_meta
            FROM qd_watchlist w
            LEFT JOIN qd_market_symbols s
              ON s.market = w.market
             AND s.symbol = w.symbol
             AND s.exchange = w.exchange_id
             AND s.market_type = w.market_type
             AND s.instrument_id = w.instrument_id
            WHERE w.user_id = ?
            ORDER BY w.id DESC
            """,
            (user_id,),
        )
        stored_rows = cur.fetchall() or []
        rows = []
        for row in stored_rows:
            _backfill_row_name(cur, user_id, row)
            rows.append(row)
        db.commit()
        cur.close()
    return rows


def add_watchlist_item(
    user_id: int,
    market: str,
    raw_symbol: str,
    name_in: str = "",
    *,
    exchange_id: str = "",
    market_type: str = "",
    instrument_id: str = "",
    settle_currency: str = "",
) -> tuple[bool, str]:
    """Validate and persist a watchlist item."""
    market = (market or "").strip()
    symbol = normalize_symbol(raw_symbol)
    if not market or not symbol:
        return False, "Missing market or symbol"

    if market == "Crypto":
        symbol = normalize_crypto_symbol(symbol)
    context = MarketContext.from_mapping({
        "market": market,
        "symbol": symbol,
        "exchange_id": exchange_id,
        "market_type": market_type,
        "instrument_id": instrument_id,
        "settle_currency": settle_currency,
    })
    if market == "Crypto" and context.exchange_id not in SUPPORTED_CRYPTO_EXCHANGE_IDS:
        return False, f"Unsupported crypto exchange: {context.exchange_id}"

    validation_err = validate_watchlist_pair(market, symbol)
    if validation_err:
        logger.info("Rejecting watchlist add for user %s: %s", user_id, validation_err)
        return False, validation_err

    matched = find_market_symbol(
        market,
        symbol,
        exchange_id=context.exchange_id,
        market_type=context.market_type,
    )
    if not matched:
        err = (
            f"Symbol '{symbol}' not found on {market}. "
            "Please verify the ticker and market, or pick from search results."
        )
        logger.info("Rejecting watchlist add for user %s: %s", user_id, err)
        return False, err
    matched_instrument_id = str(matched.get("instrument_id") or "").strip()
    if instrument_id and matched_instrument_id != str(instrument_id).strip():
        err = f"Instrument '{instrument_id}' does not match {context.exchange_id}:{context.market_type}:{symbol}."
        logger.info("Rejecting watchlist add for user %s: %s", user_id, err)
        return False, err

    resolved = (
        (matched.get("name") or "").strip()
        or resolve_symbol_name_bounded(market, symbol)
        or seed_get_symbol_name(market, symbol)
    )
    name = (name_in or "").strip() or resolved or symbol
    persist_seed_name(market, symbol, name)
    settle_currency = str((matched or {}).get("settle_currency") or context.settle_currency or "").strip().upper()
    stored_exchange_id = str(matched.get("exchange_id") or context.exchange_id or "").strip().lower()
    stored_market_type = str(matched.get("market_type") or context.market_type or "spot").strip().lower()
    stored_instrument_id = matched_instrument_id or str(context.instrument_id or "").strip()

    with get_db_connection() as db:
        cur = db.cursor()
        cur.execute(
            """
            INSERT INTO qd_watchlist (
                user_id, market, symbol, name, exchange_id, market_type,
                instrument_id, settle_currency, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, NOW(), NOW())
            ON CONFLICT(user_id, market, symbol, exchange_id, market_type, instrument_id) DO UPDATE SET
                name = excluded.name,
                settle_currency = excluded.settle_currency,
                updated_at = NOW()
            """,
            (
                user_id,
                market,
                symbol,
                name,
                stored_exchange_id,
                stored_market_type,
                stored_instrument_id,
                settle_currency,
            ),
        )
        db.commit()
        cur.close()
    return True, "success"


def remove_watchlist_item(
    user_id: int,
    market: str,
    raw_symbol: str,
    *,
    exchange_id: str = "",
    market_type: str = "",
    instrument_id: str = "",
) -> bool:
    """Remove one market context, or all legacy contexts when none is supplied."""
    market = (market or "").strip()
    raw_symbol = normalize_symbol(raw_symbol)
    canonical_symbol = normalize_crypto_symbol(raw_symbol) if market == "Crypto" else raw_symbol

    with get_db_connection() as db:
        cur = db.cursor()
        has_market_context = bool(exchange_id or market_type or instrument_id)
        if market and has_market_context:
            context = MarketContext.from_mapping({
                "market": market,
                "symbol": canonical_symbol,
                "exchange_id": exchange_id,
                "market_type": market_type,
                "instrument_id": instrument_id,
            })
            clauses = ["user_id = ?", "market = ?", "symbol = ?"]
            params = [user_id, market, canonical_symbol]
            if exchange_id:
                clauses.append("exchange_id = ?")
                params.append(context.exchange_id)
            if market_type:
                clauses.append("market_type = ?")
                params.append(context.market_type)
            if instrument_id:
                clauses.append("instrument_id = ?")
                params.append(context.instrument_id)
            cur.execute(f"DELETE FROM qd_watchlist WHERE {' AND '.join(clauses)}", tuple(params))
        elif market:
            cur.execute(
                "DELETE FROM qd_watchlist WHERE user_id = ? AND market = ? AND symbol = ?",
                (user_id, market, canonical_symbol),
            )
            deleted = cur.rowcount or 0
            if deleted == 0 and canonical_symbol != raw_symbol:
                cur.execute(
                    "DELETE FROM qd_watchlist WHERE user_id = ? AND market = ? AND symbol = ?",
                    (user_id, market, raw_symbol),
                )
        else:
            logger.info(
                "remove_watchlist called without market (user=%s, symbol=%s); using legacy symbol-only delete",
                user_id,
                raw_symbol,
            )
            cur.execute(
                "DELETE FROM qd_watchlist WHERE user_id = ? AND symbol = ?",
                (user_id, raw_symbol),
            )
        db.commit()
        cur.close()
    return True


def get_user_watchlist_pairs(user_id: int) -> list:
    """Return exact market contexts for quote fetching."""
    with get_db_connection() as db:
        cur = db.cursor()
        cur.execute(
            """
            SELECT market, symbol, exchange_id, market_type,
                   instrument_id, settle_currency
            FROM qd_watchlist
            WHERE user_id = ?
            """,
            (user_id,),
        )
        rows = cur.fetchall() or []
        cur.close()
    out = []
    seen = set()
    for row in rows:
        context_key = (
            row.get("market"), row.get("symbol"), row.get("exchange_id"),
            row.get("market_type"), row.get("instrument_id"),
        )
        if context_key in seen:
            continue
        seen.add(context_key)
        out.append({
            "market": row.get("market") or "",
            "symbol": row.get("symbol") or "",
            "exchange_id": row.get("exchange_id") or "",
            "market_type": row.get("market_type") or "",
            "instrument_id": row.get("instrument_id") or "",
            "settle_currency": row.get("settle_currency") or "",
        })
    return out


def resolve_symbol_name_bounded(market: str, symbol: str, timeout_sec: float = 4.0) -> Optional[str]:
    """Resolve a display name with a hard wall-clock cap."""
    try:
        future = _name_resolve_executor.submit(resolve_symbol_name, market, symbol)
        return future.result(timeout=timeout_sec)
    except FuturesTimeoutError:
        logger.info(
            "Symbol name resolve timed out after %.1fs for %s:%s",
            timeout_sec,
            market,
            symbol,
        )
        return None
    except Exception as exc:
        logger.debug("Symbol name resolve raised for %s:%s: %s", market, symbol, exc)
        return None


def _backfill_row_name(cur, user_id: int, row: dict) -> None:
    try:
        market = row.get("market")
        symbol = row.get("symbol")
        current_name = (row.get("name") or "").strip()
        if not market or not symbol or (current_name and current_name != symbol):
            return
        resolved = resolve_symbol_name(market, symbol) or seed_get_symbol_name(market, symbol)
        if resolved and resolved != current_name:
            row["name"] = resolved
            cur.execute(
                "UPDATE qd_watchlist SET name = ?, updated_at = NOW() WHERE id = ? AND user_id = ?",
                (resolved, row.get("id"), user_id),
            )
    except Exception:
        return

