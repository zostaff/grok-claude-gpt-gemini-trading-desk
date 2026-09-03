# SPEC-001 — Launch ingestion and the cheap gate

## Responsibility

Turn the firehose of pump.fun launches into the few worth spending model calls on.

It is **not** responsible for judging quality. Every check here is a cheap, mechanical
threshold; the moment a decision needs judgement it belongs to the panel (SPEC-003).

## Contract

Implements `TokenFeed`:

```python
def stream(self) -> AsyncIterator[Token]      # only tokens that cleared the gate
async def aclose(self) -> None
```

Invariants:

1. **A mint is emitted at most once.** A reconnect re-subscribes and replays; the dedup
   memory is what stops the same launch being paid for twice.
2. **The dedup memory is bounded.** `DEDUP_MEMORY` entries, evicted oldest-first. An
   unbounded set is a slow leak in a process meant to run for days.
3. **The cheap half of the gate runs first.** Metadata presence and curve position cost
   nothing; buyer and volume metrics cost an API call. Ordering them the other way round
   would multiply the bill by roughly four.
4. **A rejection may be terminal or provisional.** The curve only ever moves forward, so
   `curve_too_far` is permanent and the candidate is dropped. `few_buyers` is not — the
   launch stays in the watch buffer until it qualifies or ages out.

## Two real paths, both supported

pump.fun's public socket broadcasts token **creations** only. A create frame carries no
trade history, and per-token trade streams are gated:

```
'subscribeTokenTrade' and 'subscribeAccountTrade' methods are only available when
connecting with an API key funded with at least 0.02 SOL.
```

| | `credentials.pumpportal` set | left empty |
|---|---|---|
| buyers / volume | live off the same socket | one Solana Tracker poll per candidate |
| marginal cost | none | 1 API call per launch watched |
| bounded by | — | `filter.gate_concurrency` |

Both are correct. The second is the reason the cheap-first ordering matters.

Off-chain metadata (description, image, socials) lives at the `uri` in the create frame
and is fetched separately, behind a semaphore: public IPFS gateways rate-limit hard
enough that unbounded concurrent fetches fail outright.

## Failure policy

- **Socket drops** → reconnect with exponential backoff; the dedup memory survives, so
  the replay after re-subscribe emits nothing twice.
- **Metadata fetch fails** → the candidate keeps `has_metadata=False` and is rejected by
  the gate while `filter.require_metadata` is true. It is not treated as suspicious.
- **Downstream saturated** → the queue is bounded (`QUEUE_MAXSIZE`) and a passing token
  is dropped with a warning rather than growing memory without limit. Dropping a launch
  is cheaper than dying.

## Configuration

`filter.min_buyers`, `max_curve_pct`, `min_age_minutes`, `require_metadata`,
`min_volume_sol`, `max_age_minutes`, `gate_concurrency`;
`endpoints.pumpportal_ws`; `credentials.pumpportal`.

## Verification

`tests/unit/adapters/test_feed_gate.py` — every threshold at, above and below the
boundary, the metadata switch, empty-mint rejection, the dedup bound.

`tests/unit/adapters/test_feed_frames.py` — the frame path, driven by recorded pumpportal
payloads through `_handle_message`: routing, subscription acks, unroutable input (empty,
garbage, arrays, bare strings, missing `txType`), the create path including duplicate and
already-emitted mints, trade folding for buys and sells, sell magnitude, a malformed
amount costing only that field, curve progress, and the api-key URL.

`tests/unit/adapters/test_feed_connection.py` — the lifecycle against a scripted fake
socket: the initial subscribe, frames reaching the handler, reconnect after a drop,
per-token subscriptions restored on reconnect, backoff growth and its 60s cap, backoff
reset, the socket handle cleared between connections, and the api key carried on every
reconnect.

**Still not covered:** nothing in this component. The next gap is one layer down, in
SPEC-002's RPC enrichment.

### A defect these tests found

`delay = 1.0` sat immediately after the connection opened, so the exponential backoff only
applied when `websockets.connect` itself failed. A server that accepts a connection and
then drops it — which is what rate limiting usually looks like — would have been retried
once per second indefinitely. The reset now happens when a frame actually arrives, so
"healthy" means "delivered data", not "answered the handshake".
