"""ConsensusEngine: buy, conflict, skip, and the veto short-circuit."""

from __future__ import annotations

import pytest

from src.config import ConsensusConfig
from src.consensus import ConsensusEngine
from src.models import ModelVerdict


def verdict(model: str, score: float, veto: bool = False) -> ModelVerdict:
    """Build a minimal verdict for the engine under test."""
    return ModelVerdict(model=model, score=score, summary=f"{model} says {score}", raw={},
                        hard_veto=veto)


@pytest.fixture
def engine() -> ConsensusEngine:
    """An engine on the documented defaults."""
    return ConsensusEngine(ConsensusConfig())


def test_unanimous_bulls_buy(engine: ConsensusEngine) -> None:
    verdicts = [verdict(m, s) for m, s in
                (("grok", 0.72), ("claude", 0.68), ("gpt", 0.75), ("gemini", 0.70))]
    result = engine.evaluate(verdicts)
    assert result.action == "buy"
    assert result.agreement_ratio == 1.0
    # confidence = avg * agreement
    assert result.confidence == pytest.approx(result.avg_score * 1.0)
    assert set(result.bull_models) == {"grok", "claude", "gpt", "gemini"}
    assert result.bear_models == []


def test_three_of_four_still_buys(engine: ConsensusEngine) -> None:
    # agreement 0.75 meets the 0.75 floor; avg 0.6275 clears 0.60.
    verdicts = [verdict(m, s) for m, s in
                (("grok", 0.80), ("claude", 0.62), ("gpt", 0.68), ("gemini", 0.41))]
    result = engine.evaluate(verdicts)
    assert result.action == "buy"
    assert result.agreement_ratio == pytest.approx(0.75)
    assert result.bear_models == ["gemini"]


def test_wide_spread_is_conflict(engine: ConsensusEngine) -> None:
    verdicts = [verdict(m, s) for m, s in
                (("grok", 0.90), ("claude", 0.20), ("gpt", 0.55), ("gemini", 0.50))]
    result = engine.evaluate(verdicts)
    assert result.action == "conflict"
    assert "claude" in result.conflict_detail
    assert "grok" in result.conflict_detail
    assert "dissenter" in result.conflict_detail


def test_low_conviction_is_skip(engine: ConsensusEngine) -> None:
    # Tight cluster, all under the bull threshold: no conflict, just nothing to trade.
    verdicts = [verdict(m, s) for m, s in
                (("grok", 0.48), ("claude", 0.50), ("gpt", 0.46), ("gemini", 0.52))]
    result = engine.evaluate(verdicts)
    assert result.action == "skip"
    assert "low conviction" in result.conflict_detail


def test_hard_veto_beats_unanimous_bulls(engine: ConsensusEngine) -> None:
    verdicts = [
        verdict("grok", 0.95),
        verdict("claude", 0.92, veto=True),
        verdict("gpt", 0.90),
        verdict("gemini", 0.94),
    ]
    result = engine.evaluate(verdicts)
    assert result.action == "skip"
    assert result.confidence == 0.0
    assert result.bear_models == ["claude"]
    assert result.conflict_detail.startswith("hard veto ->")


def test_multiple_vetoes_are_all_named(engine: ConsensusEngine) -> None:
    verdicts = [
        verdict("grok", 0.80, veto=True),
        verdict("claude", 0.80),
        verdict("gpt", 0.80),
        verdict("gemini", 0.80, veto=True),
    ]
    result = engine.evaluate(verdicts)
    assert result.action == "skip"
    assert set(result.bear_models) == {"grok", "gemini"}


def test_no_verdicts_is_a_safe_skip(engine: ConsensusEngine) -> None:
    result = engine.evaluate([])
    assert result.action == "skip"
    assert result.confidence == 0.0
    assert result.verdicts == []


def test_bull_threshold_is_inclusive(engine: ConsensusEngine) -> None:
    # Exactly 0.55 is a bull; exactly 0.45 is neutral, not a bear.
    verdicts = [verdict("grok", 0.55), verdict("claude", 0.45)]
    result = engine.evaluate(verdicts)
    assert result.bull_models == ["grok"]
    assert result.bear_models == []
