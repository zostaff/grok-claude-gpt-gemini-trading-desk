"""Core data structures passed between the filter, the agents and the consensus engine."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

# pump.fun bonding-curve constants. A fresh curve holds 1_073_000_191 virtual tokens
# and completes when 793_100_000 of them have been bought out, leaving 206_900_000
# in reserve. Progress is therefore how far vTokensInBondingCurve has walked that span.
CURVE_INITIAL_TOKENS = 1_073_000_191.0
CURVE_RESERVE_TOKENS = 206_900_000.0
CURVE_SELLABLE_TOKENS = CURVE_INITIAL_TOKENS - CURVE_RESERVE_TOKENS


def _f(value: object, default: float = 0.0) -> float:
    """Coerce an untrusted WebSocket/REST value to float without raising."""
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _s(value: object, default: str = "") -> str:
    """Coerce an untrusted value to a stripped string without raising."""
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip()
    return str(value)


def curve_pct_from_reserves(v_tokens: float) -> float:
    """Convert vTokensInBondingCurve into a 0-100 completion percentage."""
    if v_tokens <= 0:
        return 0.0
    sold = CURVE_INITIAL_TOKENS - v_tokens
    pct = (sold / CURVE_SELLABLE_TOKENS) * 100.0
    return max(0.0, min(100.0, pct))


@dataclass
class Token:
    """A pump.fun launch, enriched with off-chain metadata and live curve metrics."""

    address: str
    name: str
    symbol: str
    description: str
    image_url: str
    twitter: str
    website: str
    telegram: str
    bonding_curve_pct: float
    unique_buyers: int
    volume_sol: float
    age_minutes: float
    has_metadata: bool
    creator_address: str = ""
    # Monotonic-independent wall clock of the first WS sighting, used to age the token.
    first_seen_ts: float = field(default_factory=time.time)

    @classmethod
    def from_ws(cls, data: dict) -> "Token":
        """Parse a pumpportal `txType: create` message into a Token.

        The WebSocket payload carries only on-chain facts (mint, name, symbol, curve
        reserves, creator). Description, image and socials live in the off-chain
        metadata JSON at `uri`; CodeFilter fetches those and fills them in afterwards,
        so they start empty and `has_metadata` starts False.
        """
        v_tokens = _f(data.get("vTokensInBondingCurve"))
        return cls(
            address=_s(data.get("mint")),
            name=_s(data.get("name")),
            symbol=_s(data.get("symbol")),
            description="",
            image_url="",
            twitter="",
            website="",
            telegram="",
            bonding_curve_pct=curve_pct_from_reserves(v_tokens),
            unique_buyers=0,
            volume_sol=_f(data.get("solAmount")),
            age_minutes=0.0,
            has_metadata=False,
            creator_address=_s(data.get("traderPublicKey")),
        )

    @property
    def metadata_score(self) -> float:
        """Fraction of the optional social/identity fields the launch bothered to fill."""
        fields = (self.description, self.image_url, self.twitter, self.website, self.telegram)
        return sum(1 for f_ in fields if f_) / len(fields)

    def refresh_age(self) -> float:
        """Recompute and return age in minutes from the first sighting."""
        self.age_minutes = (time.time() - self.first_seen_ts) / 60.0
        return self.age_minutes

    def to_dict(self) -> dict:
        """Flatten for JSONL logging."""
        return {
            "address": self.address,
            "name": self.name,
            "symbol": self.symbol,
            "description": self.description[:400],
            "image_url": self.image_url,
            "twitter": self.twitter,
            "website": self.website,
            "telegram": self.telegram,
            "bonding_curve_pct": round(self.bonding_curve_pct, 2),
            "unique_buyers": self.unique_buyers,
            "volume_sol": round(self.volume_sol, 4),
            "age_minutes": round(self.age_minutes, 2),
            "has_metadata": self.has_metadata,
            "creator_address": self.creator_address,
        }


@dataclass
class ModelVerdict:
    """One LLM's opinion on one token, normalised to a single 0-1 score plus a veto bit."""

    model: str
    score: float
    summary: str
    raw: dict
    hard_veto: bool = False
    latency_ms: int = 0

    def to_dict(self) -> dict:
        """Flatten for JSONL logging."""
        return {
            "model": self.model,
            "score": round(self.score, 4),
            "summary": self.summary,
            "raw": self.raw,
            "hard_veto": self.hard_veto,
            "latency_ms": self.latency_ms,
        }


@dataclass
class ConsensusResult:
    """The panel's aggregated decision, with enough detail to audit a disagreement."""

    action: str
    confidence: float
    agreement_ratio: float
    bull_models: list[str]
    bear_models: list[str]
    conflict_detail: str
    verdicts: list[ModelVerdict]
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")
    )

    @property
    def avg_score(self) -> float:
        """Mean of the member verdict scores (0.0 when there are no verdicts)."""
        if not self.verdicts:
            return 0.0
        return sum(v.score for v in self.verdicts) / len(self.verdicts)

    def to_dict(self) -> dict:
        """Flatten for JSONL logging."""
        return {
            "action": self.action,
            "confidence": round(self.confidence, 4),
            "agreement_ratio": round(self.agreement_ratio, 4),
            "avg_score": round(self.avg_score, 4),
            "bull_models": self.bull_models,
            "bear_models": self.bear_models,
            "conflict_detail": self.conflict_detail,
            "verdicts": [v.to_dict() for v in self.verdicts],
            "timestamp": self.timestamp,
        }
