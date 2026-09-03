"""Typed configuration. Frozen after load: nothing mutates settings at runtime.

The previous revision exposed `__getitem__`/`__setitem__` purely so the CLI could do
`config["mode"] = "dry-run"`. That made the run's most safety-critical flag writable from
anywhere. Overrides are now applied once, at load time, by `loader.load_settings`.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

Effort = Literal["low", "medium", "high", "xhigh", "max"]


class _Section(BaseModel):
    """Base for the nested config blocks: strict about unknown keys, frozen after load."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class CredentialsConfig(_Section):
    """Provider secrets. Every one of these can come from the environment instead."""

    solana_tracker: str
    grok: str
    anthropic: str
    openai: str
    google: str
    # Optional: unlocks live per-token trade streams instead of one REST poll per candidate.
    pumpportal: str = ""
    # Only read by a real executor. The shipped executor is a stub and ignores it.
    wallet: str = ""


class ModelsConfig(_Section):
    """Which model each seat on the panel uses, and how hard it is allowed to think.

    Defaults are the current top-tier model from each provider as of 2026-09-03. They are
    config, not constants, precisely because this list ages: check each provider's model
    page before assuming these are still current.
    """

    grok: str = "grok-4.6"
    claude: str = "claude-opus-5"
    gpt: str = "gpt-5.6-sol"
    # Gemini's Pro tier is preview-only right now; this is the newest STABLE model.
    # Swap to "gemini-3.1-pro-preview" to run the Pro tier and accept preview status.
    gemini: str = "gemini-3.8-flash"

    # Reasoning depth for the models that expose it. The adjudicator reasons hardest
    # because it is the one call whose whole job is finding what four others missed.
    claude_effort: Effort = "high"
    adjudicator_effort: Effort = "xhigh"
    gpt_effort: Effort = "high"


class FilterConfig(_Section):
    """Cheap pre-LLM gate: metrics a launch must clear before we spend tokens on it."""

    min_buyers: int = 5
    max_curve_pct: float = 40.0
    min_age_minutes: float = 2.0
    require_metadata: bool = True
    min_volume_sol: float = 0.5
    # Candidates older than this are dropped from the watch buffer entirely.
    max_age_minutes: float = 30.0
    # Concurrent metric polls when running without a funded PumpPortal key. Each poll is
    # one Solana Tracker request, so this is the knob that bounds gate cost.
    gate_concurrency: int = 4


class ConsensusConfig(_Section):
    """Thresholds that turn N independent scores into one action."""

    min_score: float = 0.60
    min_agreement: float = 0.75
    conflict_threshold: float = 0.40
    bull_threshold: float = 0.55
    bear_threshold: float = 0.45


class VetoConfig(_Section):
    """Per-agent kill switches. Any one of these ends the evaluation immediately."""

    max_coordinated_shilling: float = 0.7
    max_dump_risk: float = 0.8
    max_red_flag_visual: float = 0.7
    max_coordination_score: float = 0.8
    # Third-party rug score, checked before any model call is made.
    max_rug_score: float = 7.0
    # Floor the adjudicator-adjusted confidence must still clear.
    min_final_confidence: float = 0.4


class RiskConfig(_Section):
    """Bankroll brakes. These bound the damage a bad consensus can do."""

    max_position_sol: float = 0.1
    daily_loss_limit_sol: float = 0.5
    max_daily_trades: int = 10
    max_open_positions: int = 3
    stop_loss_pct: float = 50.0
    take_profit_pct: float = 100.0
    max_hold_minutes: float = 30.0
    min_position_sol: float = 0.005


class SolanaConfig(_Section):
    """Chain-side settings. Only read by a real executor; the shipped one is a stub."""

    jito_tip_lamports: int = 10_000
    slippage_bps: int = 500


class EndpointsConfig(_Section):
    """Every network address the system talks to, in one place."""

    pumpportal_ws: str = "wss://pumpportal.fun/api/data"
    solana_tracker: str = "https://data.solanatracker.io"
    solana_rpc: str = "https://api.mainnet-beta.solana.com"
    xai: str = "https://api.x.ai/v1"
    coingecko: str = "https://api.coingecko.com/api/v3"
    ipfs_gateway: str = "https://ipfs.io/ipfs/"


class JournalConfig(_Section):
    """Where the append-only decision record is written."""

    decisions_path: str = "logs/trades.jsonl"
    disagreements_path: str = "logs/conflicts.jsonl"


class Settings(_Section):
    """Whole-run configuration, validated once at startup so nothing fails mid-flight."""

    mode: Literal["dry-run", "live"] = "dry-run"

    credentials: CredentialsConfig
    models: ModelsConfig = Field(default_factory=ModelsConfig)
    endpoints: EndpointsConfig = Field(default_factory=EndpointsConfig)
    filter: FilterConfig = Field(default_factory=FilterConfig)
    consensus: ConsensusConfig = Field(default_factory=ConsensusConfig)
    vetoes: VetoConfig = Field(default_factory=VetoConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    solana: SolanaConfig = Field(default_factory=SolanaConfig)
    journal: JournalConfig = Field(default_factory=JournalConfig)

    @field_validator("consensus")
    @classmethod
    def _check_thresholds(cls, v: ConsensusConfig) -> ConsensusConfig:
        """A bear threshold above the bull threshold would classify an agent as both."""
        if v.bear_threshold > v.bull_threshold:
            raise ValueError(
                f"bear_threshold ({v.bear_threshold}) must not exceed "
                f"bull_threshold ({v.bull_threshold})"
            )
        return v

    @field_validator("risk")
    @classmethod
    def _check_position_bounds(cls, v: RiskConfig) -> RiskConfig:
        """A minimum above the maximum makes every position unfundable."""
        if v.min_position_sol > v.max_position_sol:
            raise ValueError(
                f"min_position_sol ({v.min_position_sol}) exceeds "
                f"max_position_sol ({v.max_position_sol})"
            )
        return v

    @property
    def dry_run(self) -> bool:
        """True when the executor must not be called for real."""
        return self.mode == "dry-run"
