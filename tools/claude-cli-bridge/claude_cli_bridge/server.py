"""HTTP server exposing an OpenAI-compatible chat completions endpoint."""
from __future__ import annotations

import hmac
import json
import logging
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from . import cli, json_mode
from .config import BridgeConfig
from .errors import BridgeError, bad_request, unauthorized
from .messages import build_prompt

logger = logging.getLogger("claude_cli_bridge")


class BridgeServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, config: BridgeConfig) -> None:
        self.config = config
        super().__init__((config.host, config.port), BridgeHandler)


def completion_payload(result: cli.CliResult, completion_id: str) -> dict[str, Any]:
    return {
        "id": completion_id,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": result.model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": result.text},
                "finish_reason": "stop",
            }
        ],
        "usage": result.usage,
    }


class BridgeHandler(BaseHTTPRequestHandler):
    server: BridgeServer
    server_version = "claude-cli-bridge"

    # ---- plumbing -------------------------------------------------------

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        logger.debug("%s - %s", self.address_string(), format % args)

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, error: BridgeError) -> None:
        self._send_json(error.status, error.to_payload())

    def _require_auth(self) -> None:
        header = self.headers.get("Authorization", "")
        scheme, _, token = header.partition(" ")
        expected = self.server.config.api_key
        if scheme.lower() != "bearer" or not hmac.compare_digest(token.strip(), expected):
            raise unauthorized()

    def _read_json(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise bad_request("Invalid Content-Length.") from None
        if length <= 0:
            raise bad_request("Request body is required.")
        if length > self.server.config.max_body_bytes:
            raise BridgeError("Request body too large.", status=413, error_type="invalid_request_error")
        try:
            payload = json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise bad_request("Request body must be valid JSON.") from None
        if not isinstance(payload, dict):
            raise bad_request("Request body must be a JSON object.")
        return payload

    # ---- routing --------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch({}.get(self.path.split("?", 1)[0]))

    def do_POST(self) -> None:  # noqa: N802
        routes = {"/v1/chat/completions": self.chat_completions}
        self._dispatch(routes.get(self.path.split("?", 1)[0]))

    def _dispatch(self, handler: Any) -> None:
        try:
            if handler is None:
                raise BridgeError("Not found.", status=404, error_type="invalid_request_error")
            handler()
        except BridgeError as error:
            self._send_error(error)
        except (BrokenPipeError, ConnectionResetError):
            logger.info("client disconnected")
        except Exception:  # pragma: no cover - defensive
            logger.exception("unhandled bridge error")
            self._send_error(BridgeError("Internal bridge error."))

    # ---- endpoints ------------------------------------------------------

    def chat_completions(self) -> None:
        self._require_auth()
        body = self._read_json()
        config = self.server.config
        model = str(body.get("model") or config.default_model).strip()
        prompt, system_prompt = build_prompt(body.get("messages"))
        completion_id = f"chatcmpl-{uuid.uuid4().hex}"
        if json_mode.requested(body.get("response_format")):
            result = json_mode.run(config, prompt=prompt, model=model, system_prompt=system_prompt)
            if body.get("stream"):
                self._stream_completion(source=iter([result.text]), result=result, model=model,
                                        completion_id=completion_id)
            else:
                self._send_json(200, completion_payload(result, completion_id))
            return
        if body.get("stream"):
            stream = cli.CliStream(config, prompt=prompt, model=model, system_prompt=system_prompt)
            self._stream_completion(source=stream, result=None, model=model, completion_id=completion_id)
            return
        result = cli.run_once(config, prompt=prompt, model=model, system_prompt=system_prompt)
        self._send_json(200, completion_payload(result, completion_id))

    def _stream_completion(
        self,
        *,
        source: Any,
        result: cli.CliResult | None,
        model: str,
        completion_id: str,
    ) -> None:
        """Send text from ``source`` as SSE chunks.

        ``source`` is either a live ``cli.CliStream`` or an iterator over text that
        was already produced, in which case ``result`` carries its metadata.
        """
        created = int(time.time())

        def final() -> cli.CliResult | None:
            return result or getattr(source, "result", None)
        headers_sent = False

        def chunk(delta: dict[str, Any], finish_reason: str | None = None, **extra: Any) -> dict[str, Any]:
            return {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": final().model if final() else model,
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
                **extra,
            }

        def send_headers() -> None:
            nonlocal headers_sent
            if headers_sent:
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            headers_sent = True
            self._sse(chunk({"role": "assistant", "content": ""}))

        try:
            for text in source:
                send_headers()
                self._sse(chunk({"content": text}))
            send_headers()
            usage = final().usage if final() else None
            self._sse(chunk({}, "stop", usage=usage))
            self._sse_raw("[DONE]")
        except BridgeError as error:
            if not headers_sent:
                raise
            # Headers already went out: report the failure in-band, as OpenAI does.
            self._sse(error.to_payload())
            self._sse_raw("[DONE]")
        finally:
            close = getattr(source, "close", None)
            if close is not None:
                close()
        self.close_connection = True

    def _sse(self, payload: dict[str, Any]) -> None:
        self._sse_raw(json.dumps(payload, ensure_ascii=False))

    def _sse_raw(self, data: str) -> None:
        self.wfile.write(f"data: {data}\n\n".encode("utf-8"))
        self.wfile.flush()
