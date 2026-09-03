"""Loading, env overrides and the loud validation that fails before the first HTTP call."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from .settings import Settings

# Values shipped in config.example.yaml. If one survives into a real config the user
# forgot to fill it in, and we must say exactly which key rather than 401 later.
PLACEHOLDER_PREFIXES = ("YOUR_", "CHANGEME", "<", "xxx", "XXX")

# credentials.<key> -> environment variable that may supply it instead of the YAML file.
ENV_OVERRIDES: Mapping[str, str] = {
    "solana_tracker": "SOLANA_TRACKER_KEY",
    "grok": "XAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "google": "GOOGLE_API_KEY",
    "pumpportal": "PUMPPORTAL_API_KEY",
    "wallet": "SOLANA_WALLET_KEY",
}

# Credentials without which the panel cannot run at all. `pumpportal` and `wallet` are
# deliberately absent: the first is an optional optimisation, the second is only read by
# an executor that does not exist yet.
REQUIRED_CREDENTIALS = ("solana_tracker", "grok", "anthropic", "openai", "google")


class ConfigError(RuntimeError):
    """Raised when the config file is missing, malformed, or has an unfilled key."""


def _apply_env_overrides(raw: dict[str, Any]) -> None:
    """Let the environment win over the file, so keys never have to be written to disk."""
    credentials = raw.setdefault("credentials", {})
    if not isinstance(credentials, dict):
        raise ConfigError("`credentials` must be a mapping of name to secret.")
    for key, env_name in ENV_OVERRIDES.items():
        value = os.environ.get(env_name)
        if value:
            credentials[key] = value


def _check_placeholders(raw: Mapping[str, Any]) -> None:
    """Fail loudly, naming the key, if an example placeholder was never replaced."""
    credentials = raw.get("credentials")
    if not isinstance(credentials, Mapping):
        raise ConfigError(
            "Configuration has no `credentials:` block. Copy config.example.yaml "
            "to config.yaml and fill it in."
        )

    problems: list[str] = []
    for key in REQUIRED_CREDENTIALS:
        value = credentials.get(key)
        if not isinstance(value, str) or not value.strip():
            problems.append(f"credentials.{key}: missing or empty")
        elif value.strip().startswith(PLACEHOLDER_PREFIXES):
            problems.append(f"credentials.{key}: still the placeholder {value.strip()!r}")

    if problems:
        hint = "\n  ".join(f"credentials.{k} <- ${v}" for k, v in ENV_OVERRIDES.items())
        raise ConfigError(
            "Configuration is incomplete. Fix these keys in your config file "
            "or export the matching environment variable:\n  - "
            + "\n  - ".join(problems)
            + f"\n\nEnvironment overrides:\n  {hint}"
        )


def load_settings(
    path: str | Path, *, overrides: Mapping[str, Any] | None = None
) -> Settings:
    """Read a YAML config, apply env and explicit overrides, validate, and freeze.

    `overrides` is how the CLI forces dry-run: applied here, once, before validation,
    so the resulting Settings object is immutable for the rest of the process.
    """
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

    _apply_env_overrides(raw)
    if overrides:
        raw.update(overrides)
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
