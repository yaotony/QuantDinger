"""Command-line entry point: ``python -m claude_cli_bridge``."""
from __future__ import annotations

import argparse
import logging
import sys

from .config import ConfigError, load_config
from .server import BridgeServer


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", help="Path to config.toml")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)
    logging.basicConfig(level=args.log_level.upper(), format="%(message)s")
    try:
        config = load_config(args.config).validate()
    except ConfigError as exc:
        print(f"claude-cli-bridge: configuration error: {exc}", file=sys.stderr)
        return 2
    try:
        server = BridgeServer(config)
    except OSError as exc:
        print(f"claude-cli-bridge: cannot listen on {config.host}:{config.port}: {exc}", file=sys.stderr)
        return 3
    host, port = server.server_address[:2]
    logging.getLogger("claude_cli_bridge").info("listening on http://%s:%s/v1", host, port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
