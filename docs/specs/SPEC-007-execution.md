# SPEC-007 — Execution

## Responsibility

Sign and broadcast Solana transactions.

**Nothing here is implemented.** This is the only stubbed component in the project, and it
is stubbed deliberately: it is the one place that would move real money with the user's
wallet key.

## Contract

Implements `TradeExecutor`: `buy`, `sell`, `monitor_and_stop`.

Every stub returns a correctly-shaped dict with `"stub": True` and logs a warning. That
shape is what lets the journal, the risk bookkeeping and the whole decision path be
exercised end to end in dry-run without one line of chain code existing.

In `mode: live` the pipeline logs a warning at startup and still calls the stub, so "live"
currently means "log as if it were live". That is intentional: a `mode: live` that silently
did nothing *without saying so* would be worse.

## To implement it

| Function | Must do |
|---|---|
| `build_buy_tx` | a pump.fun buy against the bonding curve, associated token account created if absent, slippage from `solana.slippage_bps` |
| `send_with_priority` | sign with the wallet key, attach a Jito tip, submit, confirm |
| `get_current_price` | read the curve's virtual reserves for the mint |
| `monitor_and_stop` | poll price; exit on stop-loss, take-profit or the hold timeout |

A real implementation goes in `adapters/execution/` as a new class and is selected in
`app/composition.py`. **No other file changes** — that is what the port is for.

## Configuration

`solana.jito_tip_lamports`, `solana.slippage_bps`, `credentials.wallet` (read by nothing
today).

## Verification

Contract test only. There is nothing else to verify: audit your own implementation before
`mode: live` means anything.
