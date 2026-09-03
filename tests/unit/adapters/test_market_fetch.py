"""The enrichment fetch: concurrency, partial failure, and the short TTL cache."""

from __future__ import annotations

import httpx
import pytest

from tests.conftest import make_settings, make_token
from trading_desk.adapters.market.solana_tracker import (
    FETCH_CACHE_TTL,
    SolanaTrackerProvider,
)

TOKEN_INFO = {
    "risk": {"score": 2.0, "rugged": False, "risks": [{"name": "Low liquidity"}]},
    "holders": 120,
    "pools": [{"liquidity": {"usd": 8000.0}, "marketCap": {"usd": 42000.0}}],
}
HOLDERS = {"accounts": [{"address": "whale", "percentage": 22.0}]}
TRADES = {"trades": [{"wallet": "buyer", "type": "buy", "amountSol": 1.5, "time": 1_800_000_000}]}


class FakeApi:
    """Serves the three enrichment routes, and fails whichever ones the test names."""

    def __init__(self, *, fail: set[str] | None = None):
        self.fail = fail or set()
        self.paths: list[str] = []

    @staticmethod
    def route_of(path: str) -> str:
        """Which of the three enrichment routes a path is."""
        if path.endswith("/holders/top"):
            return "holders"
        if path.endswith("/trades"):
            return "trades"
        return "info"

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.paths.append(path)
        route = self.route_of(path)
        if route in self.fail:
            return httpx.Response(500, json={"error": "boom"})
        return httpx.Response(200, json={"holders": HOLDERS, "trades": TRADES}.get(
            route, TOKEN_INFO
        ))


def build(api: FakeApi) -> SolanaTrackerProvider:
    """A provider wired to the fake API, with RPC enrichment switched off."""
    provider = SolanaTrackerProvider(make_settings())
    provider.client = httpx.AsyncClient(
        base_url="https://data.invalid", transport=httpx.MockTransport(api.handler)
    )
    # Wallet enrichment has its own suite; keep this one about the fetch orchestration.
    provider._rpc_ok = False
    return provider


async def test_all_three_routes_are_fetched_and_parsed():
    api = FakeApi()
    context = await build(api).fetch(make_token())

    assert context.risk.risk_score == pytest.approx(2.0)
    assert context.risk.risks == ("Low liquidity",)
    assert [h["address"] for h in context.holders] == ["whale"]
    assert [t["wallet"] for t in context.trades] == ["buyer"]
    assert context.errors == []


@pytest.mark.parametrize(
    ("failing", "still_present"),
    [("holders", "trades"), ("trades", "holders")],
)
async def test_one_dead_route_does_not_lose_the_others(failing, still_present):
    """Partial data beats no data: the panel can still judge what did arrive."""
    context = await build(FakeApi(fail={failing})).fetch(make_token())

    assert getattr(context, still_present), "the surviving route was used"
    assert context.errors, "the failure is recorded rather than swallowed"


async def test_a_dead_risk_route_yields_a_neutral_report_not_a_pass():
    """The rug gate must not be fabricated in either direction when its source is down."""
    context = await build(FakeApi(fail={"info"})).fetch(make_token())
    assert context.risk.risk_score == 0.0
    assert context.errors


async def test_a_second_fetch_within_the_ttl_is_served_from_cache():
    """The feed's gate poll and this call land seconds apart; they should cost one request."""
    api = FakeApi()
    provider = build(api)
    token = make_token()

    first = await provider.fetch(token)
    second = await provider.fetch(token)

    assert second is first
    assert len(api.paths) == 3, "three routes, fetched once"


async def test_an_expired_entry_is_refetched():
    api = FakeApi()
    provider = build(api)
    token = make_token()

    await provider.fetch(token)
    # Age the entry past the TTL without waiting for it.
    stamp, cached = provider._cache[token.address]
    provider._cache[token.address] = (stamp - FETCH_CACHE_TTL - 1, cached)
    await provider.fetch(token)

    assert len(api.paths) == 6


async def test_expired_entries_do_not_accumulate():
    """A run lasting days must not grow the cache without bound."""
    provider = build(FakeApi())
    provider._cache["stale"] = (0.0, None)  # type: ignore[assignment]

    await provider.fetch(make_token())

    assert "stale" not in provider._cache
