"""RiskManager: position sizing and the daily brakes that bound a bad day.

Pure domain logic with one injected dependency, the clock, so that day rollover is a
test case rather than something you wait for.
"""

from __future__ import annotations

import logging

from ..config.settings import RiskConfig
from .clock import SystemClock

logger = logging.getLogger(__name__)

# No single entry may consume more than this share of what is left to lose today.
BUDGET_SHARE_PER_TRADE = 0.30


class RiskManager:
    """Owns every number that can stop trading: daily loss, trade count, open positions.

    Sizing is deliberately conservative -- the product of score and confidence, then
    capped against what is left of the daily loss budget, so a losing session shrinks its
    own positions instead of doubling down.
    """

    def __init__(self, config: RiskConfig, clock: SystemClock | None = None) -> None:
        self.config = config
        self.clock = clock or SystemClock()
        self.daily_pnl_sol = 0.0
        self.trades_today = 0
        self.open_positions: dict[str, float] = {}
        self._day = self.clock.today()

    # --- state ----------------------------------------------------------------

    def _roll_day(self) -> None:
        """Reset the daily counters when the calendar date changes."""
        today = self.clock.today()
        if today != self._day:
            logger.info(
                "new day %s: resetting counters (previous pnl %+.4f SOL over %d trades)",
                today, self.daily_pnl_sol, self.trades_today,
            )
            self._day = today
            self.daily_pnl_sol = 0.0
            self.trades_today = 0

    @property
    def remaining_daily_sol(self) -> float:
        """How much of the daily loss budget is still unspent."""
        return max(0.0, self.config.daily_loss_limit_sol + min(0.0, self.daily_pnl_sol))

    def can_trade(self) -> tuple[bool, str]:
        """Return (allowed, reason). The orchestrator stops the loop when this is False."""
        self._roll_day()
        if self.daily_pnl_sol <= -self.config.daily_loss_limit_sol:
            return False, (
                f"daily loss limit hit: {self.daily_pnl_sol:+.4f} SOL "
                f"<= -{self.config.daily_loss_limit_sol}"
            )
        if self.trades_today >= self.config.max_daily_trades:
            return False, f"daily trade cap hit: {self.trades_today}/{self.config.max_daily_trades}"
        if len(self.open_positions) >= self.config.max_open_positions:
            return False, (
                f"max open positions: {len(self.open_positions)}"
                f"/{self.config.max_open_positions}"
            )
        return True, "ok"

    # --- sizing ---------------------------------------------------------------

    def position_size(self, score: float, confidence: float) -> float:
        """Size a position from conviction, capped by the remaining daily budget.

        Returns 0.0 when the remaining budget cannot fund even the minimum position,
        which the caller must treat as "do not trade" rather than a rounding artefact.
        """
        self._roll_day()
        conviction = max(0.0, min(1.0, score * confidence))
        size = self.config.max_position_sol * conviction

        budget_cap = self.remaining_daily_sol * BUDGET_SHARE_PER_TRADE
        size = min(size, budget_cap)

        if size < self.config.min_position_sol:
            # Round up to the floor only if the budget can actually cover it.
            if budget_cap >= self.config.min_position_sol and conviction > 0:
                size = self.config.min_position_sol
            else:
                logger.info(
                    "position below floor and budget cannot cover it "
                    "(conviction=%.3f, budget_cap=%.4f SOL)", conviction, budget_cap,
                )
                return 0.0

        size = min(size, self.config.max_position_sol)
        logger.info(
            "size=%.4f SOL (score=%.3f conf=%.3f conviction=%.3f remaining_budget=%.4f)",
            size, score, confidence, conviction, self.remaining_daily_sol,
        )
        return round(size, 6)

    # --- bookkeeping ----------------------------------------------------------

    def open_position(self, address: str, amount_sol: float) -> None:
        """Record an entry against the daily counters."""
        self._roll_day()
        self.open_positions[address] = amount_sol
        self.trades_today += 1
        logger.info(
            "opened %s for %.4f SOL (%d/%d today, %d/%d open)",
            address[:8], amount_sol, self.trades_today, self.config.max_daily_trades,
            len(self.open_positions), self.config.max_open_positions,
        )

    def close_position(self, address: str, pnl_sol: float) -> None:
        """Record an exit and fold its PnL into the daily total."""
        self.open_positions.pop(address, None)
        self.daily_pnl_sol += pnl_sol
        logger.info(
            "closed %s pnl=%+.4f SOL (daily %+.4f, budget left %.4f)",
            address[:8], pnl_sol, self.daily_pnl_sol, self.remaining_daily_sol,
        )

    def snapshot(self) -> dict:
        """Current risk state, for logging alongside a decision."""
        return {
            "daily_pnl_sol": round(self.daily_pnl_sol, 6),
            "trades_today": self.trades_today,
            "open_positions": len(self.open_positions),
            "remaining_daily_sol": round(self.remaining_daily_sol, 6),
        }
