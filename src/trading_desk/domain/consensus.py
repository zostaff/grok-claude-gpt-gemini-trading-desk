"""ConsensusEngine: turns N independent reports into buy, skip, or a logged conflict.

Pure domain logic. No I/O, no clock, no provider types -- the whole engine is a function
of its config and the reports it is handed, which is why it is the cheapest part of the
system to test exhaustively.
"""

from __future__ import annotations

import logging

from ..config.settings import ConsensusConfig
from .verdict import AgentReport, ConsensusResult

logger = logging.getLogger(__name__)


class ConsensusEngine:
    """Aggregates agent reports under veto-first, then agreement, then spread.

    The ordering is the design. A single hard veto ends the evaluation before averaging
    can dilute it, because a rug flagged by one specialist is not outvoted by three
    generalists who liked the picture.
    """

    def __init__(self, config: ConsensusConfig) -> None:
        self.config = config

    def evaluate(self, reports: list[AgentReport]) -> ConsensusResult:
        """Return the panel's decision over these reports."""
        if not reports:
            return ConsensusResult(
                action="skip",
                confidence=0.0,
                agreement_ratio=0.0,
                bull_agents=(),
                bear_agents=(),
                conflict_detail="no reports produced",
                reports=(),
            )

        vetoed = [r for r in reports if r.vetoed]
        if vetoed:
            detail = "; ".join(f"{r.agent}: {r.veto_reason or 'hard veto'}" for r in vetoed)
            logger.info("hard veto by %s", ", ".join(r.agent for r in vetoed))
            return ConsensusResult(
                action="skip",
                confidence=0.0,
                agreement_ratio=0.0,
                bull_agents=(),
                bear_agents=tuple(r.agent for r in vetoed),
                conflict_detail=f"hard veto -> {detail}",
                reports=tuple(reports),
            )

        bulls = tuple(r.agent for r in reports if r.quality_score >= self.config.bull_threshold)
        bears = tuple(r.agent for r in reports if r.quality_score < self.config.bear_threshold)

        scores = [r.quality_score for r in reports]
        avg = sum(scores) / len(scores)
        agreement = len(bulls) / len(reports)
        spread = max(scores) - min(scores)

        if agreement >= self.config.min_agreement and avg >= self.config.min_score:
            logger.info(
                "BUY avg=%.3f agreement=%.2f spread=%.2f", avg, agreement, spread
            )
            return ConsensusResult(
                action="buy",
                confidence=avg * agreement,
                agreement_ratio=agreement,
                bull_agents=bulls,
                bear_agents=bears,
                conflict_detail="",
                reports=tuple(reports),
            )

        if spread > self.config.conflict_threshold:
            detail = self._describe_conflict(reports, avg, spread)
            logger.info("CONFLICT spread=%.2f -> %s", spread, detail)
            return ConsensusResult(
                action="conflict",
                confidence=avg * agreement,
                agreement_ratio=agreement,
                bull_agents=bulls,
                bear_agents=bears,
                conflict_detail=detail,
                reports=tuple(reports),
            )

        logger.info(
            "SKIP low conviction avg=%.3f agreement=%.2f spread=%.2f", avg, agreement, spread
        )
        return ConsensusResult(
            action="skip",
            confidence=avg * agreement,
            agreement_ratio=agreement,
            bull_agents=bulls,
            bear_agents=bears,
            conflict_detail=(
                f"low conviction: avg {avg:.3f} < {self.config.min_score} "
                f"or agreement {agreement:.2f} < {self.config.min_agreement}"
            ),
            reports=tuple(reports),
        )

    def _describe_conflict(
        self, reports: list[AgentReport], avg: float, spread: float
    ) -> str:
        """Name the dissenting agent and quote its reasoning, so conflicts are reviewable."""
        ordered = sorted(reports, key=lambda r: r.quality_score)
        low, high = ordered[0], ordered[-1]
        # The dissenter is whichever extreme sits further from the mean.
        dissenter = low if (avg - low.quality_score) >= (high.quality_score - avg) else high
        side = "bearish" if dissenter is low else "bullish"
        return (
            f"spread {spread:.2f} > {self.config.conflict_threshold}: "
            f"{high.agent} {high.quality_score:.2f} vs {low.agent} {low.quality_score:.2f}; "
            f"{dissenter.agent} is the {side} dissenter -- "
            f"{dissenter.summary or 'no summary given'}"
        )
