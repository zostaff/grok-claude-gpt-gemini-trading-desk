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

Contract test only (`tests/contract/`): the adapter satisfies the port.

**Not covered:** the response parsers. Solana Tracker returns bare lists on some routes and
wrapped objects on others, and `_as_list` / `_parse_risk` / `_parse_holders` encode that
by hand with no fixture behind them. Recorded payloads would be the highest-value tests to
add next.
