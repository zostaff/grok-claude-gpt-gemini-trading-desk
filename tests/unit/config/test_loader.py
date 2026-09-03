"""Config loading: env overrides, placeholder detection, and the frozen result."""

from __future__ import annotations

import pytest
import yaml
from pydantic import ValidationError

from tests.conftest import make_settings
from trading_desk.config.loader import ConfigError, load_settings

MINIMAL = {
    "credentials": {
        "solana_tracker": "real-1", "grok": "real-2", "anthropic": "real-3",
        "openai": "real-4", "google": "real-5",
    }
}


def write(tmp_path, data) -> str:
    """Write a config file and return its path."""
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data))
    return str(path)


def test_missing_file_names_the_path(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_settings(tmp_path / "nope.yaml")


def test_placeholder_is_named_not_silently_accepted(tmp_path):
    data = {"credentials": dict(MINIMAL["credentials"], grok="YOUR_XAI_KEY")}
    with pytest.raises(ConfigError, match=r"credentials\.grok"):
        load_settings(write(tmp_path, data))


def test_every_unfilled_key_is_reported_at_once(tmp_path):
    """Naming one key at a time turns setup into a guessing game."""
    data = {"credentials": {"solana_tracker": "YOUR_X", "grok": ""}}
    with pytest.raises(ConfigError) as exc:
        load_settings(write(tmp_path, data))
    message = str(exc.value)
    for key in ("solana_tracker", "grok", "anthropic", "openai", "google"):
        assert f"credentials.{key}" in message


def test_environment_wins_over_the_file(tmp_path, monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", "from-env")
    settings = load_settings(write(tmp_path, MINIMAL))
    assert settings.credentials.grok == "from-env"


def test_environment_fills_a_placeholder(tmp_path, monkeypatch):
    """Exporting a key must rescue a config that still holds the example value."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "from-env")
    data = {"credentials": dict(MINIMAL["credentials"], anthropic="YOUR_ANTHROPIC_KEY")}
    assert load_settings(write(tmp_path, data)).credentials.anthropic == "from-env"


def test_optional_credentials_are_not_required(tmp_path):
    """Pumpportal is an optimisation and wallet is unused; neither may block startup."""
    settings = load_settings(write(tmp_path, MINIMAL))
    assert settings.credentials.pumpportal == ""
    assert settings.credentials.wallet == ""


def test_unknown_key_is_rejected(tmp_path):
    """A typo in a config key must fail loudly, not be silently ignored."""
    data = dict(MINIMAL, risk={"max_position_sol": 0.1, "max_postion_sol": 0.2})
    with pytest.raises(ConfigError, match=r"Extra inputs|extra"):
        load_settings(write(tmp_path, data))


def test_dry_run_override_is_applied_at_load_time(tmp_path):
    path = write(tmp_path, dict(MINIMAL, mode="live"))
    assert load_settings(path).mode == "live"
    assert load_settings(path, overrides={"mode": "dry-run"}).dry_run is True


def test_settings_are_frozen_after_load(tmp_path):
    """The run's most safety-critical flag must not be writable from anywhere."""
    settings = load_settings(write(tmp_path, MINIMAL))
    with pytest.raises(ValidationError):
        settings.mode = "live"


def test_contradictory_thresholds_are_rejected():
    """A bear threshold above the bull threshold would classify a seat as both."""
    with pytest.raises(ValidationError, match="bear_threshold"):
        make_settings(consensus={"bull_threshold": 0.4, "bear_threshold": 0.9})


def test_unfundable_position_bounds_are_rejected():
    with pytest.raises(ValidationError, match="min_position_sol"):
        make_settings(risk={"max_position_sol": 0.01, "min_position_sol": 0.5})


def test_defaults_name_current_models():
    """A stale model id is a silent 404 at runtime; pin the check to the config."""
    models = make_settings().models
    assert models.claude == "claude-opus-5"
    assert models.gpt == "gpt-5.6-sol"
    assert models.grok == "grok-4.6"
    assert models.gemini == "gemini-3.8-flash"
