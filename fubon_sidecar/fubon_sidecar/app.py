"""FastAPI application exposing the read-only v1 market data contract.

There are deliberately no order, cancel, account or position endpoints. Every request,
including ``/health``, must carry ``X-Sidecar-Token`` equal to ``FUBON_SIDECAR_TOKEN``.
"""

from __future__ import annotations

import hmac
import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator

from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from .config import TOKEN_HEADER, Settings
from .errors import SidecarError, UnauthorizedError
from .service import FubonMarketData, SdkFactory, default_sdk_factory

logger = logging.getLogger("fubon_sidecar")


def _error(exc: SidecarError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content=exc.to_payload())


def create_app(
    settings: Settings,
    *,
    service: FubonMarketData | None = None,
    sdk_factory: SdkFactory = default_sdk_factory,
    start_login: bool = True,
) -> FastAPI:
    market = service or FubonMarketData(settings, sdk_factory)
    expected_token = settings.sidecar_token.encode("utf-8")

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncGenerator[None]:
        if start_login:
            market.start()
        try:
            yield
        finally:
            market.close()

    app = FastAPI(
        title="QuantDinger Fubon market data sidecar",
        version="1",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.middleware("http")
    async def require_token(request: Request, call_next: Any) -> Any:
        supplied = request.headers.get(TOKEN_HEADER, "").encode("utf-8")
        if not supplied or not hmac.compare_digest(supplied, expected_token):
            return _error(UnauthorizedError("Missing or invalid X-Sidecar-Token"))
        return await call_next(request)

    @app.exception_handler(SidecarError)
    async def sidecar_error(_: Request, exc: SidecarError) -> JSONResponse:
        return _error(exc)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        fields = sorted({str(err.get("loc", ["", "?"])[-1]) for err in exc.errors()})
        message = f"Invalid or missing parameter(s): {', '.join(fields)}"
        return JSONResponse(status_code=400, content={"code": "invalidRequest", "message": message})

    @app.exception_handler(StarletteHTTPException)
    async def http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = {404: "notFound", 405: "methodNotAllowed"}.get(exc.status_code, "httpError")
        return JSONResponse(status_code=exc.status_code, content={"code": code})

    @app.exception_handler(Exception)
    async def unexpected_error(_: Request, exc: Exception) -> JSONResponse:
        logger.error("Unhandled sidecar error (%s)", type(exc).__name__)
        return JSONResponse(status_code=500, content={"code": "internalError"})

    @app.get("/health")
    def health() -> dict[str, Any]:
        return market.health()

    @app.get("/v1/stock/candles")
    def stock_candles(
        symbol: str,
        timeframe: str,
        start: str | None = Query(default=None, alias="from"),
        end: str | None = Query(default=None, alias="to"),
    ) -> dict[str, Any]:
        return market.stock_candles(symbol, timeframe, start, end)

    @app.get("/v1/stock/intraday/candles")
    def stock_intraday_candles(symbol: str, timeframe: str = "1") -> dict[str, Any]:
        return market.stock_intraday_candles(symbol, timeframe)

    @app.get("/v1/stock/quote")
    def stock_quote(symbol: str) -> dict[str, Any]:
        return market.stock_quote(symbol)

    @app.get("/v1/stock/tickers")
    def stock_tickers(
        ticker_type: str = Query(alias="type"),
        exchange: str | None = None,
        board: str | None = Query(default=None, alias="market"),
    ) -> dict[str, Any]:
        return market.stock_tickers(ticker_type, exchange, board)

    @app.get("/v1/futopt/intraday/candles")
    def futopt_intraday_candles(
        symbol: str, timeframe: str = "1", session: str = "regular"
    ) -> dict[str, Any]:
        return market.futopt_intraday_candles(symbol, timeframe, session)

    @app.get("/v1/futopt/quote")
    def futopt_quote(symbol: str, session: str = "regular") -> dict[str, Any]:
        return market.futopt_quote(symbol, session)

    @app.get("/v1/futopt/tickers")
    def futopt_tickers(ticker_type: str = Query(alias="type")) -> dict[str, Any]:
        return market.futopt_tickers(ticker_type)

    return app
