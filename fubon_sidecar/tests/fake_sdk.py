"""In-memory stand-in for ``fubon_neo.sdk.FubonSDK``; never touches the network."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable


class FakeFugleError(Exception):
    """Mimics ``fugle_marketdata.FugleAPIError`` (message + ``status_code``)."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass
class FakeLoginResult:
    is_success: bool
    message: str = ""
    data: list[Any] = field(default_factory=list)


class _Endpoint:
    def __init__(self, sdk: FakeSDK, name: str) -> None:
        self._sdk = sdk
        self._name = name

    def __call__(self, **params: Any) -> Any:
        self._sdk.calls.append((self._name, dict(params)))
        response = self._sdk.responses.get(self._name)
        if isinstance(response, list) and response and isinstance(response[0], _Sequence):
            response = response.pop(0).value
        if isinstance(response, BaseException):
            raise response
        if callable(response):
            return response(**params)
        return response


@dataclass
class _Sequence:
    value: Any


def sequence(*values: Any) -> list[_Sequence]:
    """Successive responses for one endpoint (e.g. first a 401, then data)."""
    return [_Sequence(value) for value in values]


class _Namespace:
    def __init__(self, sdk: FakeSDK, prefix: str) -> None:
        self._sdk = sdk
        self._prefix = prefix

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        path = f"{self._prefix}.{name}"
        if path.count(".") >= 2:
            return _Endpoint(self._sdk, path)
        return _Namespace(self._sdk, path)


class _RestClient:
    def __init__(self, sdk: FakeSDK) -> None:
        self.stock = _Namespace(sdk, "stock")
        self.futopt = _Namespace(sdk, "futopt")


class _MarketData:
    def __init__(self, sdk: FakeSDK) -> None:
        self.rest_client = _RestClient(sdk)


class FakeSDK:
    def __init__(
        self,
        *,
        login_result: FakeLoginResult | None = None,
        login_error: BaseException | None = None,
        realtime_error: BaseException | None = None,
    ) -> None:
        self.login_result = login_result or FakeLoginResult(is_success=True)
        self.login_error = login_error
        self.realtime_error = realtime_error
        self.login_calls: list[tuple[str, tuple[Any, ...]]] = []
        self.realtime_inits = 0
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.responses: dict[str, Any] = {}

    def login(self, *args: Any) -> FakeLoginResult:
        self.login_calls.append(("login", args))
        if self.login_error is not None:
            raise self.login_error
        return self.login_result

    def apikey_login(self, *args: Any) -> FakeLoginResult:
        self.login_calls.append(("apikey_login", args))
        if self.login_error is not None:
            raise self.login_error
        return self.login_result

    def init_realtime(self) -> None:
        self.realtime_inits += 1
        if self.realtime_error is not None:
            raise self.realtime_error
        self.marketdata = _MarketData(self)


def factory_for(sdk: FakeSDK) -> Callable[[], FakeSDK]:
    return lambda: sdk
