"""Wallet enrichment over a faked Solana RPC.

Balance and age are the two fields the wallet auditor leans on hardest -- a wallet with
almost no SOL and no history that just bought a large position is the single strongest
coordination signal. Getting them subtly wrong would not raise; it would quietly change
what the auditor concludes.
"""

from __future__ import annotations

import json
import time

import httpx
import pytest

from tests.conftest import make_settings, make_token
from trading_desk.adapters.market.solana_tracker import (
    LAMPORTS_PER_SOL,
    MAX_AGED_WALLETS,
    RPC_ACCOUNT_BATCH,
    SolanaTrackerProvider,
)
from trading_desk.domain.evaluation import EvaluationContext


class FakeRpc:
    """Scripts JSON-RPC replies by method and records every request."""

    def __init__(self, *, balances=None, block_time=None, status=200, error=None, body=None):
        self.balances = balances or {}
        self.block_time = block_time
        self.status, self.error, self.body = status, error, body
        self.calls: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        self.calls.append(payload)

        if self.status != 200:
            return httpx.Response(self.status, json={"error": "upstream"})
        if self.error is not None:
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "error": self.error})
        if self.body is not None:
            return httpx.Response(200, json=self.body)

        method, params = payload["method"], payload["params"]
        if method == "getMultipleAccounts":
            value = [
                {"lamports": self.balances[w]} if w in self.balances else None
                for w in params[0]
            ]
            return httpx.Response(200, json={"result": {"value": value}})
        if method == "getSignaturesForAddress":
            if self.block_time is None:
                return httpx.Response(200, json={"result": []})
            return httpx.Response(200, json={"result": [{"blockTime": self.block_time}]})
        return httpx.Response(200, json={"result": None})


def build(fake: FakeRpc) -> SolanaTrackerProvider:
    """A provider whose RPC client talks to the fake instead of the chain."""
    provider = SolanaTrackerProvider(make_settings())
    provider.rpc = httpx.AsyncClient(
        base_url="https://rpc.invalid", transport=httpx.MockTransport(fake.handler)
    )
    return provider


def context_with(wallets: list[str]) -> EvaluationContext:
    """A context whose holders and trades reference the given wallets."""
    return EvaluationContext(
        token=make_token(),
        holders=[
            {"address": w, "percentage": 1.0, "balance_sol": None, "age_days": None}
            for w in wallets
        ],
        trades=[
            {"wallet": w, "side": "buy", "amount_sol": 1.0, "balance_sol": None,
             "age_days": None}
            for w in wallets
        ],
    )


# --- the RPC call itself -----------------------------------------------------

async def test_a_transport_failure_disables_enrichment_for_the_run():
    """One dead RPC must not be retried per token for the rest of the session."""
    fake = FakeRpc(status=503)
    provider = build(fake)

    assert await provider._rpc("getMultipleAccounts", [[]]) is None
    assert provider._rpc_ok is False

    await provider._enrich_wallets(context_with(["w1"]))
    assert len(fake.calls) == 1, "the latch stops any further attempt"


async def test_a_json_rpc_error_body_also_disables_enrichment():
    """A 200 carrying an `error` member is a failure; only the body says so."""
    provider = build(FakeRpc(error={"code": -32602, "message": "Invalid params"}))

    assert await provider._rpc("getMultipleAccounts", [[]]) is None
    assert provider._rpc_ok is False


async def test_a_non_object_body_yields_no_result_without_latching():
    provider = build(FakeRpc(body=["unexpected"]))
    assert await provider._rpc("getMultipleAccounts", [[]]) is None


# --- balances ----------------------------------------------------------------

async def test_lamports_are_converted_to_sol():
    provider = build(FakeRpc(balances={"rich": 2 * LAMPORTS_PER_SOL}))
    balances = await provider._get_balances(["rich"])
    assert balances["rich"] == pytest.approx(2.0)


async def test_a_nonexistent_account_is_zero_not_unknown():
    """A null account means the wallet has never been funded -- that is a fact, not a gap."""
    provider = build(FakeRpc(balances={}))
    balances = await provider._get_balances(["never-funded"])
    assert balances["never-funded"] == 0.0


async def test_balances_are_batched_at_the_rpc_limit():
    """GetMultipleAccounts caps at 100 pubkeys; a single oversized call would 400."""
    wallets = [f"w{i}" for i in range(RPC_ACCOUNT_BATCH + 30)]
    fake = FakeRpc(balances=dict.fromkeys(wallets, LAMPORTS_PER_SOL))
    provider = build(fake)

    balances = await provider._get_balances(wallets)

    assert len(fake.calls) == 2
    assert len(fake.calls[0]["params"][0]) == RPC_ACCOUNT_BATCH
    assert len(fake.calls[1]["params"][0]) == 30
    assert len(balances) == len(wallets)


async def test_a_truncated_reply_leaves_the_rest_unknown_rather_than_raising():
    """The auditor renders a missing balance as "?"; a crash would cost the whole round."""
    fake = FakeRpc()
    fake.handler = lambda request: httpx.Response(  # type: ignore[assignment]
        200, json={"result": {"value": [{"lamports": LAMPORTS_PER_SOL}]}}
    )
    provider = build(fake)

    balances = await provider._get_balances(["first", "second", "third"])

    assert balances == {"first": pytest.approx(1.0)}


# --- ages --------------------------------------------------------------------

async def test_age_comes_from_the_oldest_reachable_signature():
    ten_days_ago = time.time() - 10 * 86400
    provider = build(FakeRpc(block_time=ten_days_ago))

    ages = await provider._get_ages(["w1"])

    assert ages["w1"] == pytest.approx(10.0, abs=0.01)


async def test_a_wallet_with_no_signatures_has_no_age():
    """Unknown stays unknown: a brand-new wallet and an unreadable one are not the same."""
    provider = build(FakeRpc(block_time=None))
    assert await provider._get_ages(["w1"]) == {}


# --- the enrichment pass -----------------------------------------------------

async def test_enrichment_attaches_to_both_holders_and_trades():
    provider = build(FakeRpc(balances={"w1": LAMPORTS_PER_SOL}, block_time=time.time() - 86400))
    context = context_with(["w1"])

    await provider._enrich_wallets(context)

    assert context.holders[0]["balance_sol"] == pytest.approx(1.0)
    assert context.trades[0]["balance_sol"] == pytest.approx(1.0)
    assert context.holders[0]["age_days"] == pytest.approx(1.0, abs=0.01)


async def test_a_wallet_seen_twice_is_requested_once():
    """Holders and trades overlap heavily; asking twice doubles the RPC bill for nothing."""
    fake = FakeRpc(balances={"w1": 0})
    provider = build(fake)

    await provider._enrich_wallets(context_with(["w1"]))

    account_calls = [c for c in fake.calls if c["method"] == "getMultipleAccounts"]
    assert len(account_calls) == 1
    assert account_calls[0]["params"][0] == ["w1"]


async def test_ageing_is_bounded_even_when_many_wallets_are_seen():
    """Signature paging is the expensive call; only the wallets that matter get aged."""
    wallets = [f"w{i}" for i in range(40)]
    fake = FakeRpc(balances=dict.fromkeys(wallets, 0), block_time=time.time() - 86400)
    provider = build(fake)

    await provider._enrich_wallets(context_with(wallets))

    aged = [c for c in fake.calls if c["method"] == "getSignaturesForAddress"]
    assert len(aged) == MAX_AGED_WALLETS


async def test_enrichment_is_skipped_entirely_once_the_latch_is_off():
    fake = FakeRpc()
    provider = build(fake)
    provider._rpc_ok = False

    await provider._enrich_wallets(context_with(["w1"]))

    assert fake.calls == []


async def test_no_wallets_means_no_rpc_traffic():
    fake = FakeRpc()
    provider = build(fake)

    await provider._enrich_wallets(EvaluationContext(token=make_token()))

    assert fake.calls == []
