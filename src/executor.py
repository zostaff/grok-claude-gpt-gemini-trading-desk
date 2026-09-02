"""Executor: THE ONLY STUBS IN THIS PROJECT. Solana transactions are left unimplemented.

Everything upstream of this file is real: real WebSocket, real REST, real LLM calls, real
consensus. This file is where the pipeline would sign and broadcast transactions with the
user's wallet key, so it deliberately does nothing and says so loudly. Implement and audit
it yourself before switching `mode: live`.
"""

from __future__ import annotations

import time

from .config import Settings
from .models import Token

STUB_WARNING = (
    "Executor is a stub. Implement real Solana TX logic before trading with real money."
)
STUB_HASH = "STUB_NOT_IMPLEMENTED"


class Executor:
    """Stubbed Solana trade execution: correct return shapes, zero on-chain effect."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.solana = settings.solana

    # --- stubs ----------------------------------------------------------------

    async def buy(self, token: Token, amount_sol: float) -> dict:
        """STUB. Would build, sign and send a pump.fun buy with a Jito priority tip."""
        print(f"[executor] WARNING: {STUB_WARNING}")
        print(
            f"[executor] STUB buy {amount_sol:.4f} SOL of {token.symbol or '?'} "
            f"({token.address}) slippage={self.solana.slippage_bps}bps "
            f"tip={self.solana.jito_tip_lamports} lamports"
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

    async def sell(self, token_address: str, pct: float) -> dict:
        """STUB. Would sell `pct` percent of the held position back into the curve."""
        print(f"[executor] WARNING: {STUB_WARNING}")
        print(f"[executor] STUB sell {pct:.1f}% of {token_address}")
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
        self,
        token_address: str,
        stop_pct: float,
        tp_pct: float,
        max_hold: float,
    ) -> dict:
        """STUB. Would poll price and exit on stop-loss, take-profit or the hold timeout."""
        print(f"[executor] WARNING: {STUB_WARNING}")
        print(
            f"[executor] STUB monitor {token_address} stop=-{stop_pct:.0f}% "
            f"tp=+{tp_pct:.0f}% max_hold={max_hold:.0f}min"
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


# --- module-level stubs referenced by the README's implementation checklist --------


async def build_buy_tx(token_address: str, amount_sol: float, slippage_bps: int) -> dict:
    """STUB. Would return a serialised, unsigned pump.fun buy transaction."""
    print(f"[executor] WARNING: {STUB_WARNING}")
    return {"stub": True, "tx": None, "address": token_address,
            "amount_sol": amount_sol, "slippage_bps": slippage_bps}


async def send_with_priority(tx: dict, jito_tip_lamports: int) -> dict:
    """STUB. Would sign, attach a Jito tip and broadcast the transaction."""
    print(f"[executor] WARNING: {STUB_WARNING}")
    return {"stub": True, "tx_hash": STUB_HASH, "jito_tip_lamports": jito_tip_lamports}


async def get_current_price(token_address: str) -> float | None:
    """STUB. Would read the current bonding-curve price for the mint."""
    print(f"[executor] WARNING: {STUB_WARNING}")
    return None
