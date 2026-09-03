"""Response parsing for Solana Tracker, against the shapes its routes actually return.

The provider returns bare lists on some routes and wrapped objects on others, and names
the same field three different ways across them. That variation is hand-encoded in the
parsers, which makes it exactly the code most likely to be wrong and least likely to
announce it -- a mis-parse degrades silently into "no holders", not into an exception.
"""

from __future__ import annotations

import pytest

from tests.conftest import make_settings, make_token
from trading_desk.adapters.market.solana_tracker import SolanaTrackerProvider, _as_list


@pytest.fixture
def provider() -> SolanaTrackerProvider:
    """A provider that never issues a request."""
    return SolanaTrackerProvider(make_settings())


# --- envelope handling -------------------------------------------------------

@pytest.mark.parametrize(
    "payload",
    [
        [{"a": 1}],
        {"accounts": [{"a": 1}]},
        {"holders": [{"a": 1}]},
        {"data": [{"a": 1}]},
    ],
    ids=["bare-list", "accounts", "holders", "data"],
)
def test_every_envelope_shape_unwraps(payload):
    assert _as_list(payload, "accounts", "holders", "data") == [{"a": 1}]


@pytest.mark.parametrize(
    "payload", [None, {}, "text", 42, {"unexpected": [{"a": 1}]}, [1, "two", None]],
    ids=["none", "empty", "string", "number", "wrong-key", "non-dict-items"],
)
def test_unusable_payloads_yield_no_rows(payload):
    assert _as_list(payload, "accounts", "holders", "data") == []


# --- holders -----------------------------------------------------------------

@pytest.mark.parametrize("key", ["address", "wallet", "owner"])
def test_holder_address_is_read_from_any_of_its_names(provider, key):
    parsed = provider._parse_holders([{key: "Wallet1", "percentage": 5.0}])
    assert parsed[0]["address"] == "Wallet1"


def test_holders_are_sorted_by_share_descending(provider):
    parsed = provider._parse_holders(
        [
            {"address": "small", "percentage": 1.0},
            {"address": "whale", "percentage": 30.0},
            {"address": "mid", "percentage": 12.5},
        ]
    )
    assert [h["address"] for h in parsed] == ["whale", "mid", "small"]


def test_a_row_with_no_address_is_dropped(provider):
    """An unattributable holding cannot be audited, so it is not offered to the auditor."""
    parsed = provider._parse_holders([{"percentage": 90.0}, {"address": "ok", "percentage": 1.0}])
    assert [h["address"] for h in parsed] == ["ok"]


def test_value_is_read_whether_nested_or_scalar(provider):
    nested = provider._parse_holders([{"address": "w", "value": {"usd": 1234.5}}])
    scalar = provider._parse_holders([{"address": "w", "value": 1234.5}])
    assert nested[0]["value_usd"] == pytest.approx(1234.5)
    assert scalar[0]["value_usd"] == pytest.approx(1234.5)


def test_enrichment_fields_start_unknown_not_zero(provider):
    """`None` renders as "?" to the auditor; 0.0 would claim the wallet is empty."""
    (holder,) = provider._parse_holders([{"address": "w", "percentage": 1.0}])
    assert holder["balance_sol"] is None
    assert holder["age_days"] is None


# --- trades ------------------------------------------------------------------

def test_millisecond_timestamps_are_normalised_to_seconds(provider):
    token = make_token()
    (trade,) = provider._parse_trades([{"wallet": "w", "time": 1_800_000_000_000}], token)
    assert trade["timestamp"] == pytest.approx(1_800_000_000.0)


def test_second_timestamps_are_left_alone(provider):
    token = make_token()
    (trade,) = provider._parse_trades([{"wallet": "w", "time": 1_800_000_000}], token)
    assert trade["timestamp"] == pytest.approx(1_800_000_000.0)


def test_trades_come_back_oldest_first(provider):
    """The auditor is asked to read the launch chronologically; order is the signal."""
    token = make_token()
    parsed = provider._parse_trades(
        [
            {"wallet": "third", "time": 300},
            {"wallet": "first", "time": 100},
            {"wallet": "second", "time": 200},
        ],
        token,
    )
    assert [t["wallet"] for t in parsed] == ["first", "second", "third"]


@pytest.mark.parametrize("key", ["amountSol", "volumeSol", "solAmount"])
def test_sol_amount_falls_back_through_every_known_name(provider, key):
    token = make_token()
    (trade,) = provider._parse_trades([{"wallet": "w", key: 2.5}], token)
    assert trade["amount_sol"] == pytest.approx(2.5)


def test_side_defaults_to_buy_when_absent(provider):
    token = make_token()
    (trade,) = provider._parse_trades([{"wallet": "w", "time": 1}], token)
    assert trade["side"] == "buy"


def test_seconds_after_launch_is_never_negative(provider):
    """A trade stamped before our first sighting is clock skew, not time travel."""
    token = make_token()
    (trade,) = provider._parse_trades(
        [{"wallet": "w", "time": token.first_seen_ts - 500}], token
    )
    assert trade["seconds_after_launch"] == 0.0


# --- gate metrics ------------------------------------------------------------

def test_metrics_count_unique_buyers_and_total_volume(provider):
    trades = [
        {"wallet": "a", "side": "buy", "amount_sol": 1.0},
        {"wallet": "a", "side": "buy", "amount_sol": 2.0},   # same wallet, once
        {"wallet": "b", "side": "buy", "amount_sol": 3.0},
        {"wallet": "c", "side": "sell", "amount_sol": -4.0},  # sellers are not buyers
    ]
    buyers, volume = SolanaTrackerProvider.metrics_from_trades(trades)
    assert buyers == 2
    assert volume == pytest.approx(10.0), "volume is magnitude, both sides counted"


# --- risk report -------------------------------------------------------------

def test_risk_flags_stay_tri_state(provider):
    """None means the provider stayed silent -- not that the authority was revoked."""
    report = provider._parse_risk({"risk": {"score": 3.0}, "pools": [{}]})
    assert report.mint_authority_revoked is None
    assert report.freeze_authority_revoked is None
    assert report.lp_burned is None


def test_risk_names_are_flattened_for_the_prompt(provider):
    report = provider._parse_risk(
        {
            "risk": {
                "score": 8.5,
                "rugged": True,
                "risks": [{"name": "Top 10 holders high"}, {"name": "No LP burn"}, "junk"],
            },
            "holders": 42,
        }
    )
    assert report.risk_score == pytest.approx(8.5)
    assert report.rugged is True
    assert report.risks == ("Top 10 holders high", "No LP burn")
    assert report.holder_count == 42
    assert "Top 10 holders high" in report.summary()


@pytest.mark.parametrize("payload", [None, {}, [], "text"], ids=["none", "empty", "list", "text"])
def test_unusable_info_yields_a_neutral_report(provider, payload):
    """A dead risk API must not fabricate a score in either direction."""
    report = provider._parse_risk(payload)
    assert report.risk_score == 0.0
    assert report.rugged is False
    assert report.risks == ()


def test_security_block_supplies_flags_when_the_pool_does_not(provider):
    report = provider._parse_risk(
        {"risk": {"score": 1.0}, "pools": [{"security": {"mintAuthority": False}}]}
    )
    assert report.mint_authority_revoked is True, "mintAuthority False means it is revoked"
