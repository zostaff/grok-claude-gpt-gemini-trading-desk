"""The cheap pre-LLM gate and the dedup memory."""

from __future__ import annotations

import pytest

from tests.conftest import make_settings, make_token
from trading_desk.adapters.feed.pumpportal import DEDUP_MEMORY, PumpPortalFeed


@pytest.fixture
def feed() -> PumpPortalFeed:
    """A feed that has never connected to anything."""
    return PumpPortalFeed(make_settings(), data=None)


def test_healthy_token_passes(feed, token):
    passed, reason = feed._passes(token)
    assert passed, reason


def test_exactly_at_every_threshold_passes(feed):
    """Thresholds are inclusive on the passing side; the boundary must not be a rejection."""
    token = make_token(
        unique_buyers=5, volume_sol=0.5, age_minutes=2.0, bonding_curve_pct=40.0
    )
    passed, reason = feed._passes(token)
    assert passed, reason


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    [
        ("unique_buyers", 4, "few_buyers"),
        ("volume_sol", 0.49, "low_volume"),
        ("age_minutes", 1.9, "too_young"),
        ("bonding_curve_pct", 40.1, "curve_too_far"),
    ],
)
def test_one_under_each_threshold_fails(feed, field, value, expected):
    passed, reason = feed._passes(make_token(**{field: value}))
    assert not passed
    assert reason.startswith(expected)


def test_missing_metadata_is_rejected_when_required(feed):
    passed, reason = feed._passes(make_token(has_metadata=False))
    assert not passed and "metadata" in reason


def test_missing_metadata_is_allowed_when_not_required():
    settings = make_settings(filter={"require_metadata": False})
    feed = PumpPortalFeed(settings, data=None)
    passed, _ = feed._passes(make_token(has_metadata=False))
    assert passed


def test_empty_mint_is_rejected_first(feed):
    """A token with no address can never be traded; reject before spending any check."""
    passed, reason = feed._passes(make_token(address=""))
    assert not passed and "mint" in reason


def test_dedup_memory_is_bounded(feed):
    """A long run must not grow the seen-set without bound."""
    for i in range(DEDUP_MEMORY + 500):
        feed._remember(f"mint{i}")
    assert len(feed._seen) <= DEDUP_MEMORY
    # The most recent mint survives; the oldest is evicted.
    assert f"mint{DEDUP_MEMORY + 499}" in feed._seen
    assert "mint0" not in feed._seen
