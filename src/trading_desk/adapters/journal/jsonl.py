"""Append-only JSONL journal: the DecisionJournal port.

Every skip is recorded, not just the buys. The value of a four-model panel is in the
record of what it refused and why -- that record is what `trading_desk.analysis` reads
to answer the only question that matters about this design: did any given seat ever
change an outcome, or was it an expensive fifth opinion?
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ...domain.token import Token
from ...domain.verdict import AdjudicationReport, ConsensusResult


def _now() -> str:
    """UTC timestamp, second resolution, ISO-8601."""
    return datetime.now(UTC).isoformat(timespec="seconds")


class JsonlJournal:
    """Writes decisions to one file and panel disagreements to another."""

    def __init__(self, decisions_path: str, disagreements_path: str) -> None:
        self.decisions_path = Path(decisions_path)
        self.disagreements_path = Path(disagreements_path)
        for path in (self.decisions_path, self.disagreements_path):
            path.parent.mkdir(parents=True, exist_ok=True)
        # One lock so concurrent monitor tasks cannot interleave half-written lines.
        self._lock = asyncio.Lock()

    async def _write(self, path: Path, record: dict[str, Any]) -> None:
        """Append one JSON object as a line, off the event loop."""
        line = json.dumps(record, default=str, ensure_ascii=False) + "\n"

        def append() -> None:
            """Blocking append, run in a worker thread."""
            with path.open("a", encoding="utf-8") as fh:
                fh.write(line)

        async with self._lock:
            await asyncio.to_thread(append)

    async def record_entry(
        self,
        token: Token,
        result: ConsensusResult,
        amount_sol: float,
        final_confidence: float,
        adjudication: AdjudicationReport,
        risk_state: dict[str, Any],
        dry_run: bool,
        tx: dict[str, Any] | None = None,
    ) -> None:
        """Record a buy, real or simulated, with the full reasoning that produced it."""
        await self._write(
            self.decisions_path,
            {
                "ts": _now(),
                "event": "dry_run_entry" if dry_run else "entry",
                "dry_run": dry_run,
                "token": token.to_dict(),
                "amount_sol": round(amount_sol, 6),
                "consensus_confidence": round(result.confidence, 4),
                "final_confidence": round(final_confidence, 4),
                "adjudication": adjudication.to_dict(),
                "consensus": result.to_dict(),
                "risk_state": risk_state,
                "tx": tx or {},
            },
        )

    async def record_skip(
        self,
        token: Token,
        reason: str,
        detail: str = "",
        result: ConsensusResult | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        """Record a token we declined, with the machine-readable reason code."""
        record: dict[str, Any] = {
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
        await self._write(self.decisions_path, record)

    async def record_exit(
        self,
        address: str,
        symbol: str,
        pnl_sol: float,
        hold_seconds: float,
        exit_reason: str,
        tx: dict[str, Any] | None = None,
    ) -> None:
        """Record a position close with realised PnL and how long it was held."""
        await self._write(
            self.decisions_path,
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

    async def record_disagreement(self, token: Token, result: ConsensusResult) -> None:
        """Record every seat's score and summary when the panel split."""
        await self._write(
            self.disagreements_path,
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
                "spread": round(result.spread, 4),
                "bull_agents": list(result.bull_agents),
                "bear_agents": list(result.bear_agents),
                "conflict_detail": result.conflict_detail,
                "scores": {r.agent: round(r.quality_score, 4) for r in result.reports},
                "summaries": {r.agent: r.summary for r in result.reports},
                "latencies_ms": {r.agent: r.latency_ms for r in result.reports},
                "degraded": [r.agent for r in result.reports if r.degraded],
                "raw": {r.agent: dict(r.scores) for r in result.reports},
            },
        )
