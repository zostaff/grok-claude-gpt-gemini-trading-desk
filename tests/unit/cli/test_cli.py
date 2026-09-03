"""The command line: argument handling and the config failure path."""

from __future__ import annotations

import pytest
import yaml

from trading_desk import cli

MINIMAL = {
    "credentials": {
        "solana_tracker": "real-1", "grok": "real-2", "anthropic": "real-3",
        "openai": "real-4", "google": "real-5",
    }
}


@pytest.fixture
def config(tmp_path):
    """A valid config file on disk."""
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(dict(MINIMAL, mode="live")))
    return str(path)


def test_a_command_is_required(capsys):
    """Bare `trading-desk` must not silently start trading."""
    with pytest.raises(SystemExit):
        cli.main([])


def test_a_missing_config_exits_two_and_explains(capsys, tmp_path):
    code = cli.main(["run", "--config", str(tmp_path / "nope.yaml")])

    assert code == 2
    assert "CONFIG ERROR" in capsys.readouterr().err


class InertPipeline:
    """Stands in for the real pipeline so `main` can be driven without a socket."""

    def __init__(self, settings):
        self.settings = settings

    async def run(self) -> None:
        return None


@pytest.fixture
def captured(monkeypatch):
    """Replace the composition root and record the settings it was handed."""
    seen: dict = {}

    def build(settings, **kwargs):
        seen["settings"] = settings
        return InertPipeline(settings)

    monkeypatch.setattr(cli, "build_pipeline", build)
    return seen


def test_dry_run_flag_overrides_a_live_config(config, captured):
    """The safety flag must win over the file, and be applied before anything is built."""
    code = cli.main(["run", "--config", config, "--dry-run"])

    assert code == 0
    assert captured["settings"].mode == "dry-run"
    assert captured["settings"].dry_run is True


def test_without_the_flag_the_config_mode_is_respected(config, captured):
    cli.main(["run", "--config", config])
    assert captured["settings"].mode == "live"


def test_the_settings_handed_to_the_pipeline_are_frozen(config, captured):
    """Nothing downstream may flip the mode once the run has started."""
    from pydantic import ValidationError

    cli.main(["run", "--config", config, "--dry-run"])
    with pytest.raises(ValidationError):
        captured["settings"].mode = "live"


def test_analyse_does_not_need_a_config(tmp_path, capsys):
    """Reading a finished run must not require the keys that produced it."""
    code = cli.main([
        "analyse",
        "--decisions", str(tmp_path / "d.jsonl"),
        "--disagreements", str(tmp_path / "x.jsonl"),
    ])

    assert code == 0
    assert "Nothing to analyse yet" in capsys.readouterr().out


def test_log_level_is_accepted(tmp_path, capsys):
    code = cli.main(["--log-level", "DEBUG", "analyse", "--decisions", str(tmp_path / "d.jsonl")])
    assert code == 0
