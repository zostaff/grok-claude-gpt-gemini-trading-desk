"""Typed configuration: loads config.yaml, validates every key, fails loudly on placeholders."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Values shipped in config.example.yaml. If one survives into a real config the user
# forgot to fill it in, and we must say exactly which key rather than 401 later.
PLACEHOLDER_PREFIXES = ("YOUR_", "CHANGEME", "<", "xxx", "XXX")

# key -> environment variable that may supply it instead of the YAML file.
ENV_OVERRIDES = {
    "data_api_key": "SOLANA_TRACKER_KEY",
    "grok_key": "XAI_API_KEY",
    "claude_key": "ANTHROPIC_API_KEY",
    "openai_key": "OPENAI_API_KEY",
    "gemini_key": "GOOGLE_API_KEY",
}


class ConfigError(RuntimeError):
    """Raised when the config file is missing, malformed, or has an unfilled key."""


class _Section(BaseModel):
    """Base for the nested config blocks: strict about unknown keys, mutable for tests."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)


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
    """Thresholds that turn four independent scores into one action."""

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
    """Chain-side settings. Only read by the executor, which is a stub."""

    wallet_key: str = ""
    jito_tip_lamports: int = 10_000
    slippage_bps: int = 500


class Settings(BaseSettings):
    """Whole-run configuration, validated once at startup so nothing fails mid-flight."""

    model_config = SettingsConfigDict(extra="forbid", validate_assignment=True)

    mode: Literal["dry-run", "live"] = "dry-run"

    ws_url: str = "wss://pumpportal.fun/api/data"
    # Optional. With a funded PumpPortal key the filter gets live per-token trades over
    # the same socket for free; without one it polls Solana Tracker for the same metrics.
    pumpportal_api_key: str = ""
    data_api: str = "https://data.solanatracker.io"
    data_api_key: str
    rpc_url: str = "https://api.mainnet-beta.solana.com"

    grok_key: str
    claude_key: str
    openai_key: str
    gemini_key: str

    grok_model: str = "grok-4-fast"
    claude_model: str = "claude-sonnet-4-6"
    gpt_model: str = "gpt-4o"
    gemini_model: str = "gemini-2.5-flash"

    filter: FilterConfig = Field(default_factory=FilterConfig)
    consensus: ConsensusConfig = Field(default_factory=ConsensusConfig)
    vetoes: VetoConfig = Field(default_factory=VetoConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    solana: SolanaConfig = Field(default_factory=SolanaConfig)

    log_path: str = "logs/trades.jsonl"
    conflict_log_path: str = "logs/conflicts.jsonl"

    @field_validator("data_api_key", "grok_key", "claude_key", "openai_key", "gemini_key")
    @classmethod
    def _reject_blank(cls, v: str) -> str:
        """A blank key is always a config bug; catch it before the first HTTP 401."""
        if not v or not v.strip():
            raise ValueError("is empty")
        return v.strip()

    # --- mapping access -------------------------------------------------------
    # __main__ flips the mode with `pipeline.config["mode"] = "dry-run"`, so the
    # settings object has to behave like a dict for reads and writes.

    def __getitem__(self, key: str) -> Any:
        try:
            return getattr(self, key)
        except AttributeError as exc:
            raise KeyError(key) from exc

    def __setitem__(self, key: str, value: Any) -> None:
        if key not in type(self).model_fields:
            raise KeyError(key)
        setattr(self, key, value)

    def __contains__(self, key: str) -> bool:
        return key in type(self).model_fields

    def get(self, key: str, default: Any = None) -> Any:
        """Dict-style read with a default."""
        return getattr(self, key, default)

    @property
    def dry_run(self) -> bool:
        """True when the executor must not be called for real."""
        return self.mode == "dry-run"


def _check_placeholders(raw: dict) -> None:
    """Fail loudly, naming the key, if an example placeholder was never replaced."""
    secret_keys = ("data_api_key", "grok_key", "claude_key", "openai_key", "gemini_key")
    bad: list[str] = []
    for key in secret_keys:
        value = raw.get(key)
        if not isinstance(value, str) or not value.strip():
            bad.append(f"{key}: missing or empty")
            continue
        if value.strip().startswith(PLACEHOLDER_PREFIXES):
            bad.append(f"{key}: still the placeholder {value.strip()!r}")
    if bad:
        env_hint = ", ".join(f"{k}->{v}" for k, v in ENV_OVERRIDES.items())
        raise ConfigError(
            "Configuration is incomplete. Fix these keys in your config file "
            "or export the matching environment variable:\n  - "
            + "\n  - ".join(bad)
            + f"\n\nEnvironment overrides: {env_hint}"
        )


def load_config(path: str | Path) -> Settings:
    """Read a YAML config, apply env overrides, validate it, and return typed Settings."""
    cfg_path = Path(path).expanduser()
    if not cfg_path.is_file():
        raise ConfigError(
            f"Config file not found: {cfg_path}. "
            "Copy config.example.yaml to config.yaml and fill in your keys."
        )

    try:
        raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"Config file {cfg_path} is not valid YAML: {exc}") from exc

    if not isinstance(raw, dict):
        raise ConfigError(f"Config file {cfg_path} must contain a YAML mapping at the top level.")

    # Environment wins over the file, so keys never have to be written to disk.
    for key, env_name in ENV_OVERRIDES.items():
        env_value = os.environ.get(env_name)
        if env_value:
            raw[key] = env_value

    _check_placeholders(raw)

    try:
        return Settings(**raw)
    except ValidationError as exc:
        lines = []
        for err in exc.errors():
            loc = ".".join(str(p) for p in err["loc"]) or "<root>"
            lines.append(f"{loc}: {err['msg']}")
        raise ConfigError(
            f"Config file {cfg_path} failed validation:\n  - " + "\n  - ".join(lines)
        ) from exc
