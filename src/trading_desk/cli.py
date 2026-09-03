"""Command line entry points: `trading-desk run` and `trading-desk analyse`."""

from __future__ import annotations

import argparse
import asyncio
import sys

from .analysis import analyse
from .app.composition import build_pipeline
from .config.loader import ConfigError, load_settings
from .observability import configure_logging


def _build_parser() -> argparse.ArgumentParser:
    """Two subcommands: run the pipeline, or read back what it decided."""
    parser = argparse.ArgumentParser(
        prog="trading-desk",
        description=(
            "Multi-model pump.fun trading pipeline: four LLMs vote, a fifth cross-examines."
        ),
    )
    parser.add_argument(
        "--log-level", default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="verbosity (default: INFO)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="run the live decision loop")
    run.add_argument("--config", default="config.yaml", help="path to the YAML config")
    run.add_argument(
        "--dry-run", action="store_true",
        help="force dry-run mode regardless of what the config says",
    )

    analyse_cmd = sub.add_parser("analyse", help="summarise a completed run")
    analyse_cmd.add_argument("--decisions", default="logs/trades.jsonl")
    analyse_cmd.add_argument("--disagreements", default="logs/conflicts.jsonl")

    return parser


def main(argv: list[str] | None = None) -> int:
    """Parse arguments, dispatch, and return a process exit code."""
    args = _build_parser().parse_args(argv)
    configure_logging(args.log_level)

    if args.command == "analyse":
        analyse(args.decisions, args.disagreements)
        return 0

    # `--dry-run` is applied at load time, so Settings stays frozen for the whole run.
    overrides = {"mode": "dry-run"} if args.dry_run else None
    try:
        settings = load_settings(args.config, overrides=overrides)
    except ConfigError as exc:
        print(f"\nCONFIG ERROR\n{exc}\n", file=sys.stderr)
        return 2

    pipeline = build_pipeline(settings)
    try:
        asyncio.run(pipeline.run())
    except KeyboardInterrupt:
        print("\nshutting down", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
