# SPEC-002 — Enrichment

## Responsibility

Give the panel the facts it cannot get from a create frame: the trade tape, the holder
table, and per-wallet balance and age.

## Contract

Implements `MarketDataProvider`: `fetch(token) -> EvaluationContext`, `aclose()`.

Invariants:

1. **Partial failure is partial, not total.** Info, holders and trades are fetched
   concurrently with `return_exceptions=True`; whichever arrive are used, and each failure
   is appended to `context.errors` rather than raising.
2. **Unknown is `None`, never `0`.** Wallet balance and age come from the Solana RPC and
   degrade to `None`, rendered `?` in the prompt. Rendering an unknown balance as `0.000`
   would tell the auditor "this wallet is empty", which is the wrong way to be wrong.
3. **Every authority flag is tri-state.** `True`/`False` is what the provider said; `None`
   is that it stayed silent. Collapsing `None` into `False` would read as "revoked, all
   clear".
4. **A short TTL cache makes the gate and the enrichment one call.** The feed polls the
   trade tape moments before the pipeline enriches; `FETCH_CACHE_TTL` makes that pair cost
   one request. The cache is pruned on every fetch so a long run cannot grow it unbounded.

## Failure policy

Solana Tracker down → empty lists and a default `RiskReport` (`risk_score=0.0`), with the
error recorded. Note the consequence: **a dead risk API does not gate anything**, so the
launch proceeds to the panel, where four models judge it with a thinner brief. That is a
deliberate choice — the alternative, skipping every launch while the provider is down,
turns one vendor outage into a full stop.

RPC down → `_rpc_ok` latches false and wallet enrichment is skipped for the rest of the
run rather than retried per token.

## Configuration

`endpoints.solana_tracker`, `endpoints.solana_rpc`, `credentials.solana_tracker`.
Module constants: `RPC_ACCOUNT_BATCH` (100, the `getMultipleAccounts` limit),
`MAX_AGED_WALLETS`, `AGE_CONCURRENCY`, `FETCH_CACHE_TTL`.

## Verification

`tests/unit/adapters/test_market_parsers.py` — every envelope shape the provider uses,
holder addresses under all three field names, descending sort, unattributable rows dropped,
nested and scalar `value`, enrichment fields starting `None` rather than `0.0`, millisecond
and second timestamps, chronological trade order, the `amountSol` fallback chain, clock skew
clamped at zero, gate metrics, and the tri-state authority flags.

Contract test: the adapter satisfies the port.

**Not covered:** the HTTP layer and `_enrich_wallets`. The RPC batching, the
`MAX_AGED_WALLETS` bound and the `_rpc_ok` latch are exercised by nothing; a fake transport
would close that.
