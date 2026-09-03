"""The connection lifecycle: subscribe, reconnect, restore, back off.

`websockets.connect` is replaced with a scripted fake, so this covers the part of the feed
that only shows itself when the network misbehaves -- which is the part that is hardest to
notice is broken in production, because a feed that silently stops reconnecting looks
exactly like a quiet market.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from websockets.exceptions import ConnectionClosedError

from tests.conftest import BUY_FRAME as BUY
from tests.conftest import CREATE_FRAME as CREATE
from tests.conftest import make_settings
from trading_desk.adapters.feed import pumpportal
from trading_desk.adapters.feed.pumpportal import PumpPortalFeed


class FakeWebSocket:
    """Delivers a scripted list of frames, then ends the way the script says."""

    def __init__(self, frames: list[str], *, drop: bool):
        self.frames, self.drop = frames, drop
        self.sent: list[dict] = []

    async def send(self, payload: str) -> None:
        self.sent.append(json.loads(payload))

    async def close(self) -> None:
        return None

    def __aiter__(self):
        return self._iterate()

    async def _iterate(self):
        for frame in self.frames:
            yield frame
        if self.drop:
            raise ConnectionClosedError(None, None)


class FakeConnect:
    """Stands in for `websockets.connect`, handing out one scripted socket per call."""

    def __init__(self, sockets: list[FakeWebSocket]):
        self.sockets = sockets
        self.calls = 0
        self.urls: list[str] = []

    def __call__(self, url, **kwargs):
        self.urls.append(url)
        index = self.calls
        self.calls += 1
        if index >= len(self.sockets):
            raise asyncio.CancelledError  # ends the loop under test
        return _AsyncCM(self.sockets[index])


class _AsyncCM:
    """Minimal async context manager around a fake socket."""

    def __init__(self, ws: FakeWebSocket):
        self.ws = ws

    async def __aenter__(self) -> FakeWebSocket:
        return self.ws

    async def __aexit__(self, *exc) -> bool:
        return False


@pytest.fixture
def no_backoff_sleep(monkeypatch):
    """Record the backoff delays instead of waiting them out."""
    delays: list[float] = []
    real_sleep = asyncio.sleep

    async def fake_sleep(seconds, *args, **kwargs):
        delays.append(seconds)
        return await real_sleep(0)

    monkeypatch.setattr(pumpportal.asyncio, "sleep", fake_sleep)
    return delays


def build_feed(**overrides) -> PumpPortalFeed:
    """A feed whose metadata fetches are inert."""
    feed = PumpPortalFeed(make_settings(**overrides), data=None)
    feed._spawn = lambda coro: coro.close()  # type: ignore[assignment,method-assign]
    return feed


async def run_loop(feed: PumpPortalFeed) -> None:
    """Drive `_ws_loop` until the scripted connect ends it."""
    with pytest.raises(asyncio.CancelledError):
        await feed._ws_loop()


async def test_it_subscribes_to_new_tokens_on_connect(monkeypatch, no_backoff_sleep):
    ws = FakeWebSocket([], drop=False)
    connect = FakeConnect([ws])
    monkeypatch.setattr(pumpportal.websockets, "connect", connect)

    await run_loop(build_feed())

    assert ws.sent[0]["method"] == "subscribeNewToken"
    assert ws.sent[0]["params"] == {"launchpad": "pumpfun"}


async def test_frames_from_the_socket_reach_the_handler(monkeypatch, no_backoff_sleep):
    ws = FakeWebSocket([json.dumps(CREATE), json.dumps(BUY)], drop=False)
    monkeypatch.setattr(pumpportal.websockets, "connect", FakeConnect([ws]))
    feed = build_feed()

    await run_loop(feed)

    assert CREATE["mint"] in feed._pending
    assert BUY["traderPublicKey"] in feed._pending[CREATE["mint"]].buyers


async def test_a_dropped_connection_reconnects(monkeypatch, no_backoff_sleep):
    """A drop is normal operation, not a reason to stop consuming the feed."""
    first = FakeWebSocket([json.dumps(CREATE)], drop=True)
    second = FakeWebSocket([], drop=False)
    connect = FakeConnect([first, second])
    monkeypatch.setattr(pumpportal.websockets, "connect", connect)

    await run_loop(build_feed())

    assert connect.calls == 3, "two scripted sockets, then the cancel that ends the loop"
    assert second.sent[0]["method"] == "subscribeNewToken", "it re-subscribes after a drop"


async def test_per_token_subscriptions_are_restored_after_a_reconnect(
    monkeypatch, no_backoff_sleep
):
    """A reconnect loses trade subscriptions silently; losing them costs the gate its data."""
    first = FakeWebSocket([json.dumps(CREATE)], drop=True)
    second = FakeWebSocket([], drop=False)
    monkeypatch.setattr(pumpportal.websockets, "connect", FakeConnect([first, second]))

    creds = {
        "solana_tracker": "k", "grok": "k", "anthropic": "k",
        "openai": "k", "google": "k", "pumpportal": "funded",
    }
    feed = build_feed(credentials=creds)
    await run_loop(feed)

    methods = [m["method"] for m in second.sent]
    assert "subscribeTokenTrade" in methods
    resubscribe = next(m for m in second.sent if m["method"] == "subscribeTokenTrade")
    assert CREATE["mint"] in resubscribe["keys"]


async def test_backoff_grows_between_failures(monkeypatch, no_backoff_sleep):
    sockets = [FakeWebSocket([], drop=True) for _ in range(4)]
    monkeypatch.setattr(pumpportal.websockets, "connect", FakeConnect(sockets))

    await run_loop(build_feed())

    assert no_backoff_sleep[:4] == [1.0, 2.0, 4.0, 8.0]


async def test_backoff_is_capped(monkeypatch, no_backoff_sleep):
    """Unbounded backoff would eventually stop reconnecting for hours."""
    sockets = [FakeWebSocket([], drop=True) for _ in range(12)]
    monkeypatch.setattr(pumpportal.websockets, "connect", FakeConnect(sockets))

    await run_loop(build_feed())

    assert max(no_backoff_sleep) == 60.0


async def test_backoff_resets_after_frames_arrive(monkeypatch, no_backoff_sleep):
    """A connection that worked must not inherit the previous failure's delay."""
    sockets = [
        FakeWebSocket([], drop=True),                    # fail -> 1s
        FakeWebSocket([], drop=True),                    # fail -> 2s
        FakeWebSocket([json.dumps(CREATE)], drop=True),  # connected: reset -> 1s
        FakeWebSocket([], drop=True),
    ]
    monkeypatch.setattr(pumpportal.websockets, "connect", FakeConnect(sockets))

    await run_loop(build_feed())

    assert no_backoff_sleep[:4] == [1.0, 2.0, 1.0, 2.0]


async def test_the_socket_handle_is_cleared_between_connections(monkeypatch, no_backoff_sleep):
    """A stale handle would make `_subscribe_trades` write into a dead socket."""
    monkeypatch.setattr(
        pumpportal.websockets, "connect", FakeConnect([FakeWebSocket([], drop=True)])
    )
    feed = build_feed()

    await run_loop(feed)

    assert feed._ws is None


async def test_the_api_key_is_carried_on_every_reconnect(monkeypatch, no_backoff_sleep):
    """Reconnecting without the key would silently downgrade to the free feed."""
    sockets = [FakeWebSocket([], drop=True) for _ in range(3)]
    connect = FakeConnect(sockets)
    monkeypatch.setattr(pumpportal.websockets, "connect", connect)

    creds = {
        "solana_tracker": "k", "grok": "k", "anthropic": "k",
        "openai": "k", "google": "k", "pumpportal": "funded",
    }
    await run_loop(build_feed(credentials=creds))

    assert all("api-key=funded" in url for url in connect.urls)
