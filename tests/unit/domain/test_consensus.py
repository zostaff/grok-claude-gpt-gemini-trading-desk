"""The consensus engine: veto first, then agreement, then spread."""

from __future__ import annotations

import pytest

from tests.conftest import make_report, make_settings
from trading_desk.domain.consensus import ConsensusEngine


@pytest.fixture
def engine() -> ConsensusEngine:
    """Engine on the shipped default thresholds."""
    return ConsensusEngine(make_settings().consensus)


def test_unanimous_bulls_buy(engine):
    reports = [make_report(n, 0.8) for n in ("grok", "claude", "gpt", "gemini")]
    result = engine.evaluate(reports)
    assert result.action == "buy"
    assert result.agreement_ratio == 1.0
    assert result.confidence == pytest.approx(0.8)


def test_three_of_four_still_buys(engine):
    reports = [make_report(n, 0.8) for n in ("grok", "claude", "gpt")]
    reports.append(make_report("gemini", 0.5))
    result = engine.evaluate(reports)
    assert result.action == "buy"
    assert result.agreement_ratio == pytest.approx(0.75)


def test_wide_spread_is_conflict(engine):
    reports = [
        make_report("grok", 0.9), make_report("claude", 0.1),
        make_report("gpt", 0.5), make_report("gemini", 0.5),
    ]
    result = engine.evaluate(reports)
    assert result.action == "conflict"
    assert "claude" in result.conflict_detail


def test_low_conviction_is_skip(engine):
    reports = [make_report(n, 0.5) for n in ("grok", "claude", "gpt", "gemini")]
    result = engine.evaluate(reports)
    assert result.action == "skip"
    assert "low conviction" in result.conflict_detail


def test_hard_veto_beats_unanimous_bulls(engine):
    """One specialist must outrank three generalists who liked the picture."""
    reports = [make_report(n, 0.95) for n in ("grok", "gpt", "gemini")]
    reports.append(make_report("claude", 0.95, vetoed=True, veto_reason="dump_risk 0.9 > 0.8"))
    result = engine.evaluate(reports)
    assert result.action == "skip"
    assert result.confidence == 0.0
    assert "dump_risk" in result.conflict_detail


def test_multiple_vetoes_are_all_named(engine):
    reports = [
        make_report("grok", 0.9, vetoed=True, veto_reason="shilling"),
        make_report("gemini", 0.9, vetoed=True, veto_reason="red flag"),
        make_report("claude", 0.9), make_report("gpt", 0.9),
    ]
    result = engine.evaluate(reports)
    assert set(result.bear_agents) == {"grok", "gemini"}
    assert "shilling" in result.conflict_detail and "red flag" in result.conflict_detail


def test_no_reports_is_a_safe_skip(engine):
    result = engine.evaluate([])
    assert result.action == "skip"
    assert result.confidence == 0.0


def test_bull_threshold_is_inclusive(engine):
    """A score exactly at the threshold counts as a bull, not against it."""
    reports = [make_report(n, 0.55) for n in ("grok", "claude", "gpt", "gemini")]
    result = engine.evaluate(reports)
    assert result.agreement_ratio == 1.0
    # avg 0.55 is still under min_score 0.60, so it does not buy.
    assert result.action == "skip"


def test_veto_is_checked_before_averaging(engine):
    """A vetoed low scorer must not merely drag the average down -- it must stop the round."""
    reports = [
        make_report("claude", 0.0, vetoed=True, veto_reason="coordination"),
        make_report("grok", 1.0), make_report("gpt", 1.0), make_report("gemini", 1.0),
    ]
    assert engine.evaluate(reports).action == "skip"


def test_spread_is_reported_on_the_result(engine):
    reports = [make_report("a", 0.9), make_report("b", 0.2)]
    assert engine.evaluate(reports).spread == pytest.approx(0.7)
