from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from conftest import TOKEN

from fubon_sidecar import healthcheck, probe
from fubon_sidecar.config import TOKEN_HEADER


@pytest.fixture
def fake_sidecar():
    state = {"body": {"status": "ok", "logged_in": True, "realtime_ready": True}, "headers": []}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - stdlib naming
            state["headers"].append(self.headers.get(TOKEN_HEADER))
            ok = self.headers.get(TOKEN_HEADER) == TOKEN
            payload = json.dumps(state["body"] if ok else {"code": "unauthorized"}).encode()
            self.send_response(200 if ok else 401)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.server_address[1], state
    server.shutdown()
    server.server_close()


def test_healthcheck_passes_only_when_logged_in_and_realtime_ready(fake_sidecar):
    port, state = fake_sidecar
    url = f"http://127.0.0.1:{port}/health"

    assert healthcheck.check(url, TOKEN) is True
    state["body"] = {"status": "starting", "logged_in": True, "realtime_ready": False}
    assert healthcheck.check(url, TOKEN) is False
    assert healthcheck.check(url, "w" * 32) is False
    assert state["headers"][0] == TOKEN


def test_healthcheck_main_reads_token_and_port_from_env(fake_sidecar, monkeypatch):
    port, _ = fake_sidecar
    monkeypatch.setenv("FUBON_SIDECAR_PORT", str(port))
    monkeypatch.setenv("FUBON_SIDECAR_TOKEN", TOKEN)

    assert healthcheck.main() == 0


def test_probe_prints_status_and_body_without_token(fake_sidecar, monkeypatch, capsys):
    port, _ = fake_sidecar
    monkeypatch.setenv("FUBON_SIDECAR_PORT", str(port))
    monkeypatch.setenv("FUBON_SIDECAR_TOKEN", TOKEN)

    assert probe.main(["/health"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("200\n")
    assert TOKEN not in out

    monkeypatch.setenv("FUBON_SIDECAR_TOKEN", "w" * 32)
    assert probe.main(["/health"]) == 1
    assert probe.main(["health"]) == 2
