"""Entry point: ``python -m fubon_sidecar``."""

from __future__ import annotations

import logging
import sys

from .config import ConfigError, Settings


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        logging.getLogger("fubon_sidecar").error("Invalid configuration: %s", exc)
        return 2

    import uvicorn

    from .app import create_app

    uvicorn.run(create_app(settings), host=settings.host, port=settings.port, log_level="info")
    return 0


if __name__ == "__main__":
    sys.exit(main())
