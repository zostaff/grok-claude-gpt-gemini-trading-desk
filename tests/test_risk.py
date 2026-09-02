"""RiskManager: sizing curve, the daily budget cap, and the brakes."""

from __future__ import annotations

import pytest

from src.config import RiskConfig
from src.risk import RiskManager


@pytest.fixture
def risk() -> RiskManager:
    """A manager on the documented defaults."""
    return RiskManager(RiskConfig())


def test_max_conviction_gets_the_max_position(risk: RiskManager) -> None:
    # budget cap is 0.5 * 0.30 = 0.15, above max_position_sol, so the cap does not bind.
    assert risk.position_size(1.0, 1.0) == pytest.approx(0.1)


def test_size_scales_with_score_times_confidence(risk: RiskManager) -> None:
    assert risk.position_size(0.8, 0.5) == pytest.approx(0.04)
    assert risk.position_size(0.6, 0.6) == pytest.approx(0.036)


def test_size_never_exceeds_max_position(risk: RiskManager) -> None:
    for score in (0.7, 0.9, 1.0):
        for conf in (0.7, 0.9, 1.0):
            assert risk.position_size(score, conf) <= risk.config.max_position_sol + 1e-9


def test_low_conviction_is_lifted_to_the_floor(risk: RiskManager) -> None:
    # 0.1 * 0.02 = 0.002, below the 0.005 floor, but the budget can cover the floor.
    assert risk.position_size(0.2, 0.1) == pytest.approx(0.005)


def test_zero_conviction_gets_nothing(risk: RiskManager) -> None:
    assert risk.position_size(0.0, 0.9) == 0.0
    assert risk.position_size(0.9, 0.0) == 0.0


def test_budget_cap_binds_after_losses(risk: RiskManager) -> None:
    risk.daily_pnl_sol = -0.45          # 0.05 SOL of budget left
    # cap is 0.05 * 0.30 = 0.015, below the 0.1 the conviction would otherwise ask for.
    assert risk.position_size(1.0, 1.0) == pytest.approx(0.015)


def test_exhausted_budget_returns_zero(risk: RiskManager) -> None:
    risk.daily_pnl_sol = -0.49          # 0.01 left, cap 0.003, under the 0.005 floor
    assert risk.position_size(1.0, 1.0) == 0.0


def test_daily_loss_limit_stops_trading(risk: RiskManager) -> None:
    assert risk.can_trade()[0] is True
    risk.daily_pnl_sol = -0.5
    allowed, reason = risk.can_trade()
    assert allowed is False
    assert "daily loss limit" in reason


def test_daily_trade_cap_stops_trading(risk: RiskManager) -> None:
    risk.trades_today = 10
    allowed, reason = risk.can_trade()
    assert allowed is False
    assert "trade cap" in reason


def test_max_open_positions_stops_trading(risk: RiskManager) -> None:
    for i in range(3):
        risk.open_position(f"mint{i}", 0.01)
    allowed, reason = risk.can_trade()
    assert allowed is False
    assert "max open positions" in reason


def test_closing_a_position_frees_a_slot_and_books_pnl(risk: RiskManager) -> None:
    risk.open_position("mintA", 0.02)
    assert risk.trades_today == 1
    risk.close_position("mintA", -0.008)
    assert risk.open_positions == {}
    assert risk.daily_pnl_sol == pytest.approx(-0.008)
    assert risk.remaining_daily_sol == pytest.approx(0.492)


def test_profit_does_not_inflate_the_budget(risk: RiskManager) -> None:
    risk.open_position("mintA", 0.02)
    risk.close_position("mintA", 0.30)
    # The loss budget is a floor on losses, not a bankroll that wins expand.
    assert risk.remaining_daily_sol == pytest.approx(0.5)
