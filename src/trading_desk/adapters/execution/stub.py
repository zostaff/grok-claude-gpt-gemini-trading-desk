"""The only stubs in the project: the TradeExecutor port, deliberately unimplemented.

Everything upstream of this file is real -- real socket, real REST, real model calls, real
consensus. This is where the pipeline would sign and broadcast transactions with the
user's wallet key, so it does nothing and says so loudly on every call.

The return shapes are correct, which is what lets the journal and the risk bookkeeping be
exercised end to end in dry-run without a single line of chain code existing.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from ...config.settings import Settings
from ...domain.token import Token

logger = logging.getLogger(__name__)

STUB_WARNING = (
    "Executor is a stub. Implement real Solana TX logic before trading with real money."
)
STUB_HASH = "STUB_NOT_IMPLEMENTED"


class StubExecutor:
    """Stubbed Solana trade execution: correct return shapes, zero on-chain effect."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.solana = settings.solana

    async def buy(self, token: Token, amount_sol: float) -> dict[str, Any]:
        """STUB. Would build, sign and send a pump.fun buy with a Jito priority tip."""
        logger.warning("%s", STUB_WARNING)
        logger.warning(
            "STUB buy %.4f SOL of %s (%s) slippage=%dbps tip=%d lamports",
            amount_sol, token.symbol or "?", token.address,
            self.solana.slippage_bps, self.solana.jito_tip_lamports,
        )
        return {
            "ok": False,
            "stub": True,
            "tx_hash": STUB_HASH,
            "side": "buy",
            "address": token.address,
            "symbol": token.symbol,
            "amount_sol": amount_sol,
            "price": None,
            "slippage_bps": self.solana.slippage_bps,
            "jito_tip_lamports": self.solana.jito_tip_lamports,
            "ts": time.time(),
            "warning": STUB_WARNING,
        }

    async def sell(self, token_address: str, pct: float) -> dict[str, Any]:
        """STUB. Would sell `pct` percent of the held position back into the curve."""
        logger.warning("%s", STUB_WARNING)
        logger.warning("STUB sell %.1f%% of %s", pct, token_address)
        return {
            "ok": False,
            "stub": True,
            "tx_hash": STUB_HASH,
            "side": "sell",
            "address": token_address,
            "pct": pct,
            "amount_sol": 0.0,
            "price": None,
            "ts": time.time(),
            "warning": STUB_WARNING,
        }

    async def monitor_and_stop(
        self, token_address: str, stop_pct: float, take_profit_pct: float, max_hold_minutes: float
    ) -> dict[str, Any]:
        """STUB. Would poll price and exit on stop-loss, take-profit or the hold timeout."""
        logger.warning("%s", STUB_WARNING)
        logger.warning(
            "STUB monitor %s stop=-%.0f%% tp=+%.0f%% max_hold=%.0fmin",
            token_address, stop_pct, take_profit_pct, max_hold_minutes,
        )
        return {
            "ok": False,
            "stub": True,
            "tx_hash": STUB_HASH,
            "address": token_address,
            "exit_reason": "not_monitored_stub",
            "pnl_sol": 0.0,
            "hold_seconds": 0.0,
            "ts": time.time(),
            "warning": STUB_WARNING,
        }
