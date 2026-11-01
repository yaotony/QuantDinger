"""Tiny stdlib HTTP helpers for the tests."""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from conftest import API_KEY


def request(
    method: str,
    url: str,
    payload: Any = None,
    *,
    api_key: str | None = API_KEY,
    timeout: float = 30,
) -> tuple[int, dict[str, str], bytes]:
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if api_key is not None:
        req.add_header("Authorization", f"Bearer {api_key}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as err:
        return err.code, dict(err.headers), err.read()


def post_json(url: str, payload: Any, **kwargs: Any) -> tuple[int, dict[str, Any]]:
    status, _, body = request("POST", url, payload, **kwargs)
    return status, json.loads(body)
