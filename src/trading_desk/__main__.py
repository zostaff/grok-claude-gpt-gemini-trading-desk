"""Allow `python -m trading_desk` alongside the installed `trading-desk` script."""

from __future__ import annotations

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
