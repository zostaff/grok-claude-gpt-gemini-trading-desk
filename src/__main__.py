"""Entry point: `python -m src.pipeline --config config.yaml --dry-run`."""

from __future__ import annotations

import argparse
import asyncio
import sys

from .config import ConfigError
from .pipeline import MultiModelPipeline


def main() -> int:
    """Parse arguments, build the pipeline and run it. Returns a process exit code."""
    parser = argparse.ArgumentParser(
        prog="python -m src.pipeline",
        description="Multi-model pump.fun trading pipeline (four LLMs vote, a fifth checks).",
    )
    parser.add_argument("--config", default="config.yaml", help="path to the YAML config")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="force dry-run mode regardless of what the config says",
    )
    args = parser.parse_args()

    try:
        pipeline = MultiModelPipeline(args.config)
    except ConfigError as exc:
        print(f"\nCONFIG ERROR\n{exc}\n", file=sys.stderr)
        return 2

    if args.dry_run:
        pipeline.config["mode"] = "dry-run"

    try:
        asyncio.run(pipeline.run())
    except KeyboardInterrupt:
        print("\n[pipeline] shutting down")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
