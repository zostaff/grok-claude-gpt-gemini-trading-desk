"""CodeFilter._passes(): threshold edges, missing metadata, and permanent rejections."""

from __future__ import annotations

import time

import pytest

from src.config import FilterConfig, Settings
from src.filter import CodeFilter
from src.models import Token, curve_pct_from_reserves


def make_settings(**filter_overrides) -> Settings:
    """A Settings object with dummy keys and an optionally tweaked filter block."""
    return Settings(
        data_api_key="k",
        grok_key="k",
        claude_key="k",
        openai_key="k",
        gemini_key="k",
        filter=FilterConfig(**filter_overrides),
    )


def make_token(**overrides) -> Token:
    """A token that passes every default threshold, before overrides are applied."""
    base = dict(
        address="MintAddress1111111111111111111111111111111",
        name="Test Token",
        symbol="TEST",
        description="a token",
        image_url="https://example.invalid/i.png",
        twitter="https://x.com/test",
        website="",
        telegram="",
        bonding_curve_pct=12.0,
        unique_buyers=10,
        volume_sol=2.0,
        age_minutes=5.0,
        has_metadata=True,
        creator_address="Creator111",
        first_seen_ts=time.time() - 300,
    )
    base.update(overrides)
    return Token(**base)


@pytest.fixture
def cf() -> CodeFilter:
    """A filter on the documented defaults. No connection is opened by construction."""
    return CodeFilter(make_settings())


def test_healthy_token_passes(cf: CodeFilter) -> None:
    assert cf._passes(make_token()) == (True, "ok")


def test_exactly_at_every_threshold_passes(cf: CodeFilter) -> None:
    token = make_token(
        unique_buyers=5,          # == min_buyers
        volume_sol=0.5,           # == min_volume_sol
        age_minutes=2.0,          # == min_age_minutes
        bonding_curve_pct=40.0,   # == max_curve_pct
    )
    assert cf._passes(token) == (True, "ok")


def test_one_under_each_threshold_fails(cf: CodeFilter) -> None:
    assert cf._passes(make_token(unique_buyers=4))[1].startswith("few_buyers")
    assert cf._passes(make_token(volume_sol=0.49))[1].startswith("low_volume")
    assert cf._passes(make_token(age_minutes=1.99))[1].startswith("too_young")
    assert cf._passes(make_token(bonding_curve_pct=40.01))[1].startswith("curve_too_far")


def test_missing_metadata_is_rejected_when_required(cf: CodeFilter) -> None:
    passed, reason = cf._passes(make_token(has_metadata=False))
    assert passed is False
    assert reason == "no_metadata"


def test_missing_metadata_is_allowed_when_not_required() -> None:
    cf = CodeFilter(make_settings(require_metadata=False))
    assert cf._passes(make_token(has_metadata=False)) == (True, "ok")


def test_empty_mint_is_rejected_first(cf: CodeFilter) -> None:
    # Checked before everything else, even though this token also fails other gates.
    passed, reason = cf._passes(make_token(address="", unique_buyers=0))
    assert (passed, reason) == (False, "no_mint_address")


def test_age_is_checked_before_buyers(cf: CodeFilter) -> None:
    # A one-minute-old token with no buyers is young, not unpopular: keep watching it.
    passed, reason = cf._passes(make_token(age_minutes=1.0, unique_buyers=0))
    assert passed is False
    assert reason.startswith("too_young")


def test_dedup_memory_is_bounded(cf: CodeFilter) -> None:
    from src.filter import DEDUP_MEMORY

    for i in range(DEDUP_MEMORY + 50):
        cf._remember(f"mint{i}")
    assert len(cf._seen) == DEDUP_MEMORY
    assert "mint0" not in cf._seen           # oldest evicted
    assert f"mint{DEDUP_MEMORY + 49}" in cf._seen


def test_from_ws_parses_a_real_create_message() -> None:
    msg = {
        "signature": "sig",
        "mint": "MintAddress1111111111111111111111111111111",
        "traderPublicKey": "Creator111",
        "txType": "create",
        "initialBuy": 30000000.0,
        "solAmount": 1.5,
        "bondingCurveKey": "bck",
        "vTokensInBondingCurve": 1043000191.0,
        "vSolInBondingCurve": 31.5,
        "marketCapSol": 30.2,
        "name": "Test Token",
        "symbol": "TEST",
        "uri": "https://ipfs.io/ipfs/abc",
        "pool": "pump",
    }
    token = Token.from_ws(msg)
    assert token.address == msg["mint"]
    assert token.symbol == "TEST"
    assert token.creator_address == "Creator111"
    assert token.volume_sol == pytest.approx(1.5)
    # Socials live in the metadata JSON, not the WS frame.
    assert token.has_metadata is False
    assert token.image_url == ""
    assert 0.0 < token.bonding_curve_pct < 10.0


def test_from_ws_survives_a_junk_message() -> None:
    token = Token.from_ws({"txType": "create"})
    assert token.address == ""
    assert token.bonding_curve_pct == 0.0
    assert token.volume_sol == 0.0


def test_curve_pct_is_clamped() -> None:
    assert curve_pct_from_reserves(1_073_000_191.0) == pytest.approx(0.0)
    assert curve_pct_from_reserves(206_900_000.0) == pytest.approx(100.0)
    assert curve_pct_from_reserves(0.0) == 0.0            # no data, not "complete"
    assert curve_pct_from_reserves(1e12) == 0.0           # nonsense clamps low
