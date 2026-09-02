"""ConsensusEngine: turns four independent verdicts into buy, skip, or a logged conflict."""

from __future__ import annotations

from .config import ConsensusConfig
from .models import ConsensusResult, ModelVerdict


class ConsensusEngine:
    """Aggregates model verdicts under veto-first, then agreement, then spread.

    The ordering matters: a single hard veto ends the evaluation before averaging can
    dilute it, because a rug flagged by one specialist is not outvoted by three
    generalists who liked the picture.
    """

    def __init__(self, config: ConsensusConfig) -> None:
        self.config = config

    def evaluate(self, verdicts: list[ModelVerdict]) -> ConsensusResult:
        """Return the panel's decision over these verdicts."""
        if not verdicts:
            return ConsensusResult(
                action="skip",
                confidence=0.0,
                agreement_ratio=0.0,
                bull_models=[],
                bear_models=[],
                conflict_detail="no verdicts produced",
                verdicts=[],
            )

        # Step 1: hard vetoes short-circuit everything.
        vetoed = [v for v in verdicts if v.hard_veto]
        if vetoed:
            detail = "; ".join(f"{v.model}: {v.summary or 'hard veto'}" for v in vetoed)
            print(f"[consensus] HARD VETO by {', '.join(v.model for v in vetoed)}")
            return ConsensusResult(
                action="skip",
                confidence=0.0,
                agreement_ratio=0.0,
                bull_models=[],
                bear_models=[v.model for v in vetoed],
                conflict_detail=f"hard veto -> {detail}",
                verdicts=verdicts,
            )

        # Step 2: classify.
        bulls = [v.model for v in verdicts if v.score >= self.config.bull_threshold]
        bears = [v.model for v in verdicts if v.score < self.config.bear_threshold]

        # Step 3: aggregate.
        scores = [v.score for v in verdicts]
        avg = sum(scores) / len(scores)
        agreement = len(bulls) / len(verdicts)
        spread = max(scores) - min(scores)

        # Step 4: decide.
        if agreement >= self.config.min_agreement and avg >= self.config.min_score:
            confidence = avg * agreement
            print(
                f"[consensus] BUY avg={avg:.3f} agreement={agreement:.2f} "
                f"spread={spread:.2f} confidence={confidence:.3f}"
            )
            return ConsensusResult(
                action="buy",
                confidence=confidence,
                agreement_ratio=agreement,
                bull_models=bulls,
                bear_models=bears,
                conflict_detail="",
                verdicts=verdicts,
            )

        if spread > self.config.conflict_threshold:
            detail = self._describe_conflict(verdicts, avg, spread)
            print(f"[consensus] CONFLICT spread={spread:.2f} -> {detail}")
            return ConsensusResult(
                action="conflict",
                confidence=avg * agreement,
                agreement_ratio=agreement,
                bull_models=bulls,
                bear_models=bears,
                conflict_detail=detail,
                verdicts=verdicts,
            )

        print(
            f"[consensus] SKIP low conviction avg={avg:.3f} "
            f"agreement={agreement:.2f} spread={spread:.2f}"
        )
        return ConsensusResult(
            action="skip",
            confidence=avg * agreement,
            agreement_ratio=agreement,
            bull_models=bulls,
            bear_models=bears,
            conflict_detail=(
                f"low conviction: avg {avg:.3f} < {self.config.min_score} "
                f"or agreement {agreement:.2f} < {self.config.min_agreement}"
            ),
            verdicts=verdicts,
        )

    def _describe_conflict(
        self, verdicts: list[ModelVerdict], avg: float, spread: float
    ) -> str:
        """Name the dissenting model and quote its reasoning, so conflicts are reviewable."""
        ordered = sorted(verdicts, key=lambda v: v.score)
        low, high = ordered[0], ordered[-1]
        # The dissenter is whichever extreme sits further from the mean.
        dissenter = low if (avg - low.score) >= (high.score - avg) else high
        side = "bearish" if dissenter is low else "bullish"
        return (
            f"spread {spread:.2f} > {self.config.conflict_threshold}: "
            f"{high.model} {high.score:.2f} vs {low.model} {low.score:.2f}; "
            f"{dissenter.model} is the {side} dissenter -- "
            f"{dissenter.summary or 'no summary given'}"
        )
