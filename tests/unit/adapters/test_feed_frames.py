"""Frame handling: the WebSocket path, driven by recorded pumpportal payloads.

The socket itself is not opened here. `_handle_message` is the seam -- everything the
connection does is deliver bytes to it -- so feeding it real frame shapes covers the
routing, the create path, the trade folding and the malformed-input handling without a
network.
"""

from __future__ import annotations

import json

import pytest

from tests.conftest import BUY_FRAME as BUY
from tests.conftest import CREATE_FRAME as CREATE
from tests.conftest import make_settings
from trading_desk.adapters.feed.pumpportal import PumpPortalFeed


@pytest.fixture
def feed() -> PumpPortalFeed:
    """A feed with no socket and no metadata fetching."""
    f = PumpPortalFeed(make_settings(), data=None)
    # `_spawn` would create a task with no running loop in the sync tests below.
    f._spawn = lambda coro: coro.close()  # type: ignore[assignment,method-assign]
    return f


async def send(feed: PumpPortalFeed, frame) -> None:
    """Deliver one frame the way the socket would."""
    await feed._handle_message(json.dumps(frame) if not isinstance(frame, str) else frame)


# --- routing -----------------------------------------------------------------

@pytest.mark.parametrize(
    "raw",
    ["", "not json at all", "[1, 2, 3]", '"a bare string"', '{"no": "txType"}'],
    ids=["empty", "garbage", "array", "string", "no-txtype"],
)
async def test_unroutable_frames_are_ignored(feed, raw):
    """Every byte on this socket is untrusted; none of these may raise."""
    await feed._handle_message(raw)
    assert feed._pending == {}


async def test_subscription_ack_is_not_a_launch(feed):
    """Pumpportal acks a subscribe with a bare message frame; it must not register."""
    await send(feed, {"message": "Successfully subscribed to token creation events."})
    assert feed._pending == {}
    assert feed.stats["seen"] == 0


async def test_unknown_tx_type_is_ignored(feed):
    await send(feed, dict(CREATE, txType="migrate"))
    assert feed._pending == {}


# --- the create path ---------------------------------------------------------

async def test_create_registers_a_candidate(feed):
    await send(feed, CREATE)

    assert set(feed._pending) == {CREATE["mint"]}
    candidate = feed._pending[CREATE["mint"]]
    assert candidate.token.symbol == "TEST"
    assert feed.stats["seen"] == 1


async def test_creator_initial_buy_counts_as_volume_but_is_one_wallet(feed):
    """The creator's own buy is real volume, but it is not organic demand."""
    await send(feed, CREATE)
    candidate = feed._pending[CREATE["mint"]]

    assert candidate.volume_sol == pytest.approx(1.85)
    assert candidate.buyers == {CREATE["traderPublicKey"]}


async def test_duplicate_create_does_not_register_twice(feed):
    await send(feed, CREATE)
    await send(feed, CREATE)
    assert feed.stats["seen"] == 1


async def test_an_already_emitted_mint_is_not_re_registered(feed):
    """A reconnect replays; the dedup memory is what stops paying for a launch twice."""
    feed._remember(CREATE["mint"])
    await send(feed, CREATE)
    assert feed._pending == {}
    assert feed.stats["seen"] == 0


async def test_create_without_a_mint_is_dropped(feed):
    await send(feed, dict(CREATE, mint=None))
    assert feed._pending == {}


# --- the trade path ----------------------------------------------------------

async def test_buy_adds_a_buyer_and_volume(feed):
    await send(feed, CREATE)
    await send(feed, BUY)

    candidate = feed._pending[CREATE["mint"]]
    assert BUY["traderPublicKey"] in candidate.buyers
    assert candidate.volume_sol == pytest.approx(1.85 + 0.4)


async def test_sell_adds_volume_but_not_a_buyer(feed):
    await send(feed, CREATE)
    await send(feed, dict(BUY, txType="sell"))

    candidate = feed._pending[CREATE["mint"]]
    assert BUY["traderPublicKey"] not in candidate.buyers
    assert candidate.volume_sol == pytest.approx(1.85 + 0.4)


async def test_sell_volume_is_counted_as_magnitude(feed):
    """A negative solAmount must not subtract from traded volume."""
    await send(feed, CREATE)
    await send(feed, dict(BUY, txType="sell", solAmount=-0.4))

    assert feed._pending[CREATE["mint"]].volume_sol == pytest.approx(1.85 + 0.4)


async def test_trade_for_an_unwatched_mint_is_ignored(feed):
    await send(feed, dict(BUY, mint="SomeOtherMint1111111111111111111111111111111"))
    assert feed._pending == {}


async def test_malformed_amount_does_not_lose_the_rest_of_the_frame(feed):
    """One bad field must cost that field, not the whole update."""
    await send(feed, CREATE)
    await send(feed, dict(BUY, solAmount="not-a-number"))

    candidate = feed._pending[CREATE["mint"]]
    assert BUY["traderPublicKey"] in candidate.buyers, "the buyer still counts"
    assert candidate.volume_sol == pytest.approx(1.85), "the bad amount is skipped"


async def test_curve_progress_is_updated_from_trades(feed):
    await send(feed, CREATE)
    before = feed._pending[CREATE["mint"]].token.bonding_curve_pct
    await send(feed, BUY)
    after = feed._pending[CREATE["mint"]].token.bonding_curve_pct

    assert after > before, "the curve only ever moves forward"
    assert 0.0 <= after <= 100.0


async def test_malformed_curve_value_leaves_the_last_good_one(feed):
    await send(feed, CREATE)
    await send(feed, BUY)
    good = feed._pending[CREATE["mint"]].token.bonding_curve_pct

    await send(feed, dict(BUY, vTokensInBondingCurve={}))
    assert feed._pending[CREATE["mint"]].token.bonding_curve_pct == pytest.approx(good)


# --- connection URL ----------------------------------------------------------

def test_ws_url_is_unchanged_without_a_key():
    feed = PumpPortalFeed(make_settings(), data=None)
    assert feed._ws_url() == feed.settings.endpoints.pumpportal_ws
    assert feed.use_ws_trades is False


def test_ws_url_carries_the_key_when_one_is_configured():
    """A funded key is what unlocks live trade streams instead of REST polling."""
    settings = make_settings(
        credentials={
            "solana_tracker": "k", "grok": "k", "anthropic": "k",
            "openai": "k", "google": "k", "pumpportal": "funded-key",
        }
    )
    feed = PumpPortalFeed(settings, data=None)
    assert "api-key=funded-key" in feed._ws_url()
    assert feed.use_ws_trades is True
