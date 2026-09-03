"""EvaluationContext: the single bundle every scoring agent is handed.

One context type is what makes the agents substitutable. Each agent reads the fields it
needs and ignores the rest, so the orchestrator can hold them in a plain list and call
them uniformly -- rather than knowing that Claude wants holders while Gemini wants a URL.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .token import Token


@dataclass(frozen=True)
class RiskReport:
    """Third-party rug analysis for a mint, used as a gate before any model call.

    Every authority flag is tri-state on purpose: True and False are what the provider
    said, None is that it stayed silent. Collapsing None into False would render an
    unknown mint authority as "revoked, all clear", which is the wrong way to be wrong.
    """

    risk_score: float = 0.0
    rugged: bool = False
    risks: tuple[str, ...] = ()
    liquidity_usd: float = 0.0
    market_cap_usd: float = 0.0
    holder_count: int = 0
    mint_authority_revoked: bool | None = None
    freeze_authority_revoked: bool | None = None
    lp_burned: bool | None = None

    def summary(self) -> str:
        """One-line description, embedded in the prompts that need it."""
        names = ", ".join(self.risks[:6]) or "none reported"
        return (
            f"risk_score={self.risk_score} rugged={self.rugged} "
            f"liquidity=${self.liquidity_usd:,.0f} mcap=${self.market_cap_usd:,.0f} "
            f"holders={self.holder_count} flags=[{names}]"
        )

    def to_dict(self) -> dict:
        """Flatten for the journal."""
        return {
            "risk_score": self.risk_score,
            "rugged": self.rugged,
            "risks": list(self.risks),
            "liquidity_usd": round(self.liquidity_usd, 2),
            "market_cap_usd": round(self.market_cap_usd, 2),
            "holder_count": self.holder_count,
            "mint_authority_revoked": self.mint_authority_revoked,
            "freeze_authority_revoked": self.freeze_authority_revoked,
            "lp_burned": self.lp_burned,
        }


@dataclass
class MarketContext:
    """Ambient market state, refreshed on a timer and shared across evaluations."""

    updated_at: str = "not yet fetched"
    sol_24h_pct: str = "unknown"
    sol_usd: float | None = None
    trending_memes: tuple[str, ...] = ()
    notes: str = "market context has not been refreshed yet"

    def as_prompt_block(self) -> str:
        """Render for inclusion in a prompt."""
        trending = ", ".join(self.trending_memes[:12]) or "unknown"
        return (
            f"SOL 24h change: {self.sol_24h_pct}\n"
            f"trending tickers: {trending}\n"
            f"context updated: {self.updated_at} ({self.notes})"
        )


@dataclass
class EvaluationContext:
    """Everything known about one launch at the moment the panel is asked to judge it."""

    token: Token
    trades: list[dict[str, Any]] = field(default_factory=list)
    holders: list[dict[str, Any]] = field(default_factory=list)
    risk: RiskReport = field(default_factory=RiskReport)
    market: MarketContext = field(default_factory=MarketContext)
    errors: list[str] = field(default_factory=list)

    @property
    def risk_summary(self) -> str:
        """Shorthand for the risk line the prompts embed."""
        return self.risk.summary()
