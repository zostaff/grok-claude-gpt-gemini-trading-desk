"""Parsing an untrusted create frame, and the bonding-curve maths."""

from __future__ import annotations

import pytest

from tests.conftest import CREATE_FRAME
from trading_desk.domain.token import (
    CURVE_INITIAL_TOKENS,
    Token,
    curve_pct_from_reserves,
)


def test_create_frame_is_parsed(token=None):
    parsed = Token.from_create_frame(CREATE_FRAME)
    assert parsed.address == CREATE_FRAME["mint"]
    assert parsed.symbol == "TEST"
    assert parsed.creator_address == CREATE_FRAME["traderPublicKey"]
    assert parsed.volume_sol == pytest.approx(1.85)
    # Off-chain metadata is not in the frame; it starts empty and is filled in later.
    assert parsed.description == "" and parsed.image_url == ""
    assert parsed.has_metadata is False


def test_junk_frame_does_not_raise():
    """Every field on the wire is untrusted; a bad frame must degrade, not crash."""
    parsed = Token.from_create_frame(
        {"mint": None, "solAmount": "not-a-number", "vTokensInBondingCurve": {}}
    )
    assert parsed.address == ""
    assert parsed.volume_sol == 0.0
    assert parsed.bonding_curve_pct == 0.0


def test_curve_pct_is_clamped():
    assert curve_pct_from_reserves(CURVE_INITIAL_TOKENS) == pytest.approx(0.0)
    assert curve_pct_from_reserves(-5) == 0.0
    assert curve_pct_from_reserves(1.0) == 100.0


def test_metadata_score_counts_filled_fields():
    token = Token.from_create_frame(CREATE_FRAME)
    assert token.metadata_score == 0.0
    token.description, token.image_url = "x", "y"
    assert token.metadata_score == pytest.approx(0.4)
