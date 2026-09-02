"""TradeLog: append-only JSONL for every decision, with a second file for disagreements."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import ConsensusResult, Token


def _now() -> str:
    """UTC timestamp, second resolution, ISO-8601."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class TradeLog:
    """Writes decisions to trades.jsonl and model disagreements to conflicts.jsonl.

    Every skip is logged with its reason, not just the buys: the value of a four-model
    panel is in the record of what it refused and why, which is what analysis.py reads.
    """

    def __init__(self, log_path: str, conflict_log_path: str) -> None:
        self.log_path = Path(log_path)
        self.conflict_path = Path(conflict_log_path)
        for path in (self.log_path, self.conflict_path):
            path.parent.mkdir(parents=True, exist_ok=True)
        # One lock so concurrent monitor tasks cannot interleave half-written lines.
        self._lock = asyncio.Lock()

    async def _write(self, path: Path, record: dict) -> None:
        """Append one JSON object as a line, off the event loop."""
        line = json.dumps(record, default=str, ensure_ascii=False) + "\n"

        def append() -> None:
            """Blocking append, run in a worker thread."""
            with path.open("a", encoding="utf-8") as fh:
                fh.write(line)

        async with self._lock:
            await asyncio.to_thread(append)

    # --- entries --------------------------------------------------------------

    async def log_entry(
        self,
        token: Token,
        result: ConsensusResult,
        amount_sol: float,
        final_confidence: float,
        checker: dict,
        risk_state: dict,
        dry_run: bool,
        tx: dict | None = None,
    ) -> None:
        """Record a buy (real or simulated) with the full reasoning that produced it."""
        await self._write(
            self.log_path,
            {
                "ts": _now(),
                "event": "dry_run_entry" if dry_run else "entry",
                "dry_run": dry_run,
                "token": token.to_dict(),
                "amount_sol": round(amount_sol, 6),
                "consensus_confidence": round(result.confidence, 4),
                "final_confidence": round(final_confidence, 4),
                "checker": checker,
                "consensus": result.to_dict(),
                "risk_state": risk_state,
                "tx": tx or {},
            },
        )

    async def log_skip(
        self,
        token: Token,
        reason: str,
        detail: str = "",
        result: ConsensusResult | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        """Record a token we declined, with the machine-readable reason code."""
        record = {
            "ts": _now(),
            "event": "skip",
            "reason": reason,
            "detail": detail,
            "token": token.to_dict(),
        }
        if result is not None:
            record["consensus"] = result.to_dict()
        if extra:
            record["extra"] = extra
        await self._write(self.log_path, record)

    async def log_exit(
        self,
        address: str,
        symbol: str,
        pnl_sol: float,
        hold_seconds: float,
        exit_reason: str,
        tx: dict | None = None,
    ) -> None:
        """Record a position close with realised PnL and how long it was held."""
        await self._write(
            self.log_path,
            {
                "ts": _now(),
                "event": "exit",
                "address": address,
                "symbol": symbol,
                "pnl_sol": round(pnl_sol, 6),
                "hold_seconds": round(hold_seconds, 1),
                "exit_reason": exit_reason,
                "tx": tx or {},
            },
        )

    # --- conflicts ------------------------------------------------------------

    async def log_conflict(self, token: Token, result: ConsensusResult) -> None:
        """Record every model's score and summary when the panel disagreed."""
        await self._write(
            self.conflict_path,
            {
                "ts": _now(),
                "token": {
                    "address": token.address,
                    "name": token.name,
                    "symbol": token.symbol,
                },
                "action": result.action,
                "agreement_ratio": round(result.agreement_ratio, 4),
                "avg_score": round(result.avg_score, 4),
                "bull_models": result.bull_models,
                "bear_models": result.bear_models,
                "conflict_detail": result.conflict_detail,
                "scores": {v.model: round(v.score, 4) for v in result.verdicts},
                "summaries": {v.model: v.summary for v in result.verdicts},
                "latencies_ms": {v.model: v.latency_ms for v in result.verdicts},
                "raw": {v.model: v.raw for v in result.verdicts},
            },
        )
