"""Position sizing and the daily brakes."""

from __future__ import annotations

import pytest

from tests.conftest import make_settings
from trading_desk.domain.clock import FrozenClock
from trading_desk.domain.risk import RiskManager, TradingStatus


@pytest.fixture
def risk() -> RiskManager:
    """Manager on shipped defaults with controllable time."""
    return RiskManager(make_settings().risk, FrozenClock())


def test_max_conviction_gets_the_max_position(risk):
    assert risk.position_size(1.0, 1.0) == pytest.approx(0.1)


def test_size_scales_with_score_times_confidence(risk):
    assert risk.position_size(0.8, 0.75) == pytest.approx(0.06)


def test_size_never_exceeds_max_position(risk):
    assert risk.position_size(2.0, 2.0) == pytest.approx(0.1)


def test_low_conviction_is_lifted_to_the_floor(risk):
    """Below the floor but fundable: round up rather than send dust."""
    assert risk.position_size(0.4, 0.1) == pytest.approx(0.005)


def test_zero_conviction_gets_nothing(risk):
    assert risk.position_size(0.0, 0.9) == 0.0


def test_budget_cap_binds_after_losses(risk):
    """Once 30% of the remaining budget is under the conviction size, the budget wins."""
    risk.daily_pnl_sol = -0.4          # 0.1 SOL of budget left -> cap 0.03
    assert risk.position_size(0.8, 0.625) == pytest.approx(0.03)


def test_exhausted_budget_returns_zero_not_dust(risk):
    """The documented cliff: when 30% of what is left cannot fund the floor, do not trade."""
    risk.daily_pnl_sol = -0.49         # 0.01 left -> cap 0.003 < min 0.005
    assert risk.position_size(0.8, 0.625) == 0.0


def test_a_clean_desk_may_trade(risk):
    check = risk.check()
    assert check.ok and check.status is TradingStatus.OK


def test_daily_loss_limit_halts_for_the_day(risk):
    risk.daily_pnl_sol = -0.5
    check = risk.check()
    assert check.status is TradingStatus.HALTED
    assert "daily loss limit" in check.reason


def test_daily_trade_cap_halts_for_the_day(risk):
    risk.trades_today = 10
    check = risk.check()
    assert check.status is TradingStatus.HALTED
    assert "trade cap" in check.reason


def test_running_out_of_slots_pauses_rather_than_halts(risk):
    """Capacity clears the moment a position closes; ending the run over it is wrong."""
    for i in range(3):
        risk.open_position(f"addr{i}", 0.01)

    check = risk.check()
    assert check.status is TradingStatus.PAUSED
    assert not check.ok
    assert "capacity" in check.reason


def test_capacity_clears_itself_when_a_position_closes(risk):
    for i in range(3):
        risk.open_position(f"addr{i}", 0.01)
    assert risk.check().status is TradingStatus.PAUSED

    risk.close_position("addr0", 0.0)

    assert risk.check().status is TradingStatus.OK


def test_a_halt_outranks_a_pause(risk):
    """At capacity AND out of budget is a halt: the terminal condition must win."""
    for i in range(3):
        risk.open_position(f"addr{i}", 0.01)
    risk.daily_pnl_sol = -0.5

    assert risk.check().status is TradingStatus.HALTED


def test_closing_a_position_frees_a_slot_and_books_pnl(risk):
    risk.open_position("addr", 0.05)
    risk.close_position("addr", -0.02)
    assert risk.open_positions == {}
    assert risk.daily_pnl_sol == pytest.approx(-0.02)
    assert risk.remaining_daily_sol == pytest.approx(0.48)


def test_profit_does_not_inflate_the_budget(risk):
    """The loss budget is a floor on losses, not a bankroll that wins top up."""
    risk.close_position("addr", +5.0)
    assert risk.remaining_daily_sol == pytest.approx(0.5)


def test_counters_reset_when_the_day_rolls_over(risk):
    """The injected clock is what makes midnight a test case instead of a wait."""
    risk.open_position("addr", 0.05)
    risk.close_position("addr", -0.3)
    assert risk.trades_today == 1

    risk.clock.advance_days(1)

    assert risk.check().ok
    assert risk.trades_today == 0
    assert risk.daily_pnl_sol == 0.0
    assert risk.remaining_daily_sol == pytest.approx(0.5)
