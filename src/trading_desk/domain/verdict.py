"""What an agent returns, and what the panel concludes from four of them."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any


def _utc_now() -> str:
    """UTC timestamp, second resolution, ISO-8601."""
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass(frozen=True)
class AgentReport:
    """One agent's finished opinion on one launch.

    `quality_score` is aggregated by the agent itself, not by the orchestrator, because
    only the agent knows which of its keys point which way. Risk-direction scores
    (`dump_risk`, `red_flag_visual`, ...) are deliberately absent from it: averaging a
    danger signal together with quality signals would let a beautiful picture cancel out
    a rug warning. They drive `vetoed` instead, and are kept in `scores` for the record.
    """

    agent: str
    quality_score: float
    scores: Mapping[str, float]
    summary: str
    vetoed: bool = False
    veto_reason: str = ""
    latency_ms: int = 0
    error: str | None = None

    @property
    def degraded(self) -> bool:
        """True when this report came from a fallback rather than a real answer."""
        return self.error is not None

    def to_dict(self) -> dict:
        """Flatten for the journal."""
        return {
            "agent": self.agent,
            "quality_score": round(self.quality_score, 4),
            "scores": {k: round(v, 4) for k, v in self.scores.items()},
            "summary": self.summary,
            "vetoed": self.vetoed,
            "veto_reason": self.veto_reason,
            "latency_ms": self.latency_ms,
            "error": self.error,
        }


@dataclass(frozen=True)
class AdjudicationReport:
    """The fifth call's ruling on a panel that already voted to buy."""

    approved: bool
    confidence_adjustment: float = 0.0
    veto_reason: str = ""
    missed_risk: str = ""
    reasoning: str = ""
    latency_ms: int = 0
    error: str | None = None

    def to_dict(self) -> dict:
        """Flatten for the journal."""
        return {
            "approved": self.approved,
            "confidence_adjustment": round(self.confidence_adjustment, 4),
            "veto_reason": self.veto_reason,
            "missed_risk": self.missed_risk,
            "reasoning": self.reasoning,
            "latency_ms": self.latency_ms,
            "error": self.error,
        }


@dataclass(frozen=True)
class ConsensusResult:
    """The panel's aggregated decision, with enough detail to audit a disagreement."""

    action: str  # "buy" | "skip" | "conflict"
    confidence: float
    agreement_ratio: float
    bull_agents: tuple[str, ...]
    bear_agents: tuple[str, ...]
    conflict_detail: str
    reports: tuple[AgentReport, ...]
    timestamp: str = field(default_factory=_utc_now)

    @property
    def avg_score(self) -> float:
        """Mean of the member quality scores (0.0 when there are none)."""
        if not self.reports:
            return 0.0
        return sum(r.quality_score for r in self.reports) / len(self.reports)

    @property
    def spread(self) -> float:
        """Distance between the most and least convinced agent."""
        if not self.reports:
            return 0.0
        scores = [r.quality_score for r in self.reports]
        return max(scores) - min(scores)

    def to_dict(self) -> dict[str, Any]:
        """Flatten for the journal."""
        return {
            "action": self.action,
            "confidence": round(self.confidence, 4),
            "agreement_ratio": round(self.agreement_ratio, 4),
            "avg_score": round(self.avg_score, 4),
            "spread": round(self.spread, 4),
            "bull_agents": list(self.bull_agents),
            "bear_agents": list(self.bear_agents),
            "conflict_detail": self.conflict_detail,
            "reports": [r.to_dict() for r in self.reports],
            "timestamp": self.timestamp,
        }
