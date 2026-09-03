"""Logging setup. The library never configures logging; only the CLI entry point does."""

from __future__ import annotations

import logging
import sys

FORMAT = "%(asctime)s %(levelname)-7s %(name)-38s %(message)s"
DATEFMT = "%H:%M:%S"


def configure_logging(level: str = "INFO", *, quiet_libraries: bool = True) -> None:
    """Send structured lines to stderr and mute the noisier third-party loggers."""
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format=FORMAT,
        datefmt=DATEFMT,
        stream=sys.stderr,
        force=True,
    )
    if quiet_libraries:
        for name in ("httpx", "httpcore", "websockets", "openai", "anthropic", "google"):
            logging.getLogger(name).setLevel(logging.WARNING)
