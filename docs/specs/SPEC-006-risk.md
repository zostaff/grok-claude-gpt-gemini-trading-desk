# SPEC-006 — Risk

## Responsibility

Own every number that can stop trading, and size what survives.

Pure domain logic with one injected dependency — the clock — so that midnight rollover is a
test case instead of something you wait for.

## Contract

```
conviction = clamp01(score × confidence)
size       = min(max_position_sol × conviction, remaining_budget × 0.30)
if size < min_position_sol:
    size = min_position_sol   if the 30% cap can cover it
    else 0.0                  -> caller MUST treat as "do not trade"
```

Invariants:

1. **A losing session shrinks its own positions.** The 30% cap is taken against what is
   left of the daily budget, so losses reduce subsequent size rather than inviting a
   martingale.
2. **Zero means do not trade, never "round it up".** When the cap falls under the floor,
   `position_size` returns `0.0`. Every caller path treats that as a skip.
3. **Profit does not inflate the budget.** `remaining_daily_sol` uses `min(0.0, pnl)`. The
   daily limit is a floor on losses, not a bankroll that wins top up.
4. **Counters reset on date change, not on a timer.** Driven by the injected `Clock`.

## Configuration

`risk.*`. `min_position_sol > max_position_sol` is rejected at load time — it would make
every position unfundable.

## Verification

`tests/unit/domain/test_risk.py` — 13 cases: max conviction, scaling, the floor lift, the
zero cliff, budget-cap binding after losses, all three halt conditions, PnL bookkeeping,
profit not inflating the budget, and day rollover via `FrozenClock`.
