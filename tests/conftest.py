"""Shared fixtures. Nothing here touches a network or a provider."""

from __future__ import annotations

import pytest

from trading_desk.config.settings import Settings
from trading_desk.domain.evaluation import EvaluationContext, RiskReport
from trading_desk.domain.token import Token
from trading_desk.domain.verdict import AgentReport


def make_settings(**overrides) -> Settings:
    """A fully valid Settings object with throwaway credentials."""
    base = {
        "credentials": {
            "solana_tracker": "test-key",
            "grok": "test-key",
            "anthropic": "test-key",
            "openai": "test-key",
            "google": "test-key",
        }
    }
    base.update(overrides)
    return Settings(**base)


def make_token(**overrides) -> Token:
    """A launch that clears every gate unless a field is overridden."""
    fields = {
        "address": "So11111111111111111111111111111111111111112",
        "name": "Test Token",
        "symbol": "TEST",
        "description": "a test launch",
        "image_url": "https://example.invalid/art.png",
        "twitter": "@test",
        "website": "https://example.invalid",
        "telegram": "",
        "bonding_curve_pct": 12.0,
        "unique_buyers": 20,
        "volume_sol": 4.0,
        "age_minutes": 6.0,
        "has_metadata": True,
    }
    fields.update(overrides)
    return Token(**fields)


def make_report(agent: str, score: float, **overrides) -> AgentReport:
    """An AgentReport with a given quality score."""
    fields = {
        "agent": agent,
        "quality_score": score,
        "scores": {"quality": score},
        "summary": f"{agent} says {score}",
    }
    fields.update(overrides)
    return AgentReport(**fields)


@pytest.fixture
def settings() -> Settings:
    """Default settings for tests that need them."""
    return make_settings()


@pytest.fixture
def token() -> Token:
    """A healthy launch."""
    return make_token()


@pytest.fixture
def context(token) -> EvaluationContext:
    """A launch with a clean risk report and no enrichment."""
    return EvaluationContext(token=token, risk=RiskReport(risk_score=2.0))
