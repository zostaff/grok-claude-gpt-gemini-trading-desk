# SPEC-004 — Consensus

## Responsibility

Turn N independent `AgentReport`s into exactly one of `buy`, `skip` or `conflict`.

Pure domain logic: a function of its config and its inputs. No clock, no I/O, no provider
types — which is why it is the cheapest component in the system to test exhaustively.

## Contract

```
1. any hard veto              -> skip, confidence 0.0
2. agreement >= 0.75 AND avg >= 0.60  -> buy, confidence = avg × agreement
3. spread > 0.40              -> conflict (names the dissenter and quotes it)
4. otherwise                  -> skip, low conviction
```

**The ordering is the whole design.** A veto short-circuits before averaging, because a rug
flagged by one specialist must not be outvoted by three generalists who liked the picture.
Reversing steps 1 and 2 would turn a veto into a mere low score.

Boundaries are pinned by test: `bull_threshold` is inclusive (a report exactly at 0.55 is a
bull), `conflict_threshold` is exclusive (spread exactly 0.40 is not a conflict).

A `conflict` is not a failure. It is the output this project exists to collect: it names
which seat dissented, on which side, and quotes its reasoning into `conflicts.jsonl`.

## Configuration

`consensus.min_score`, `min_agreement`, `conflict_threshold`, `bull_threshold`,
`bear_threshold`. A `bear_threshold` above `bull_threshold` is rejected at load time — it
would classify one seat as both.

## Verification

`tests/unit/domain/test_consensus.py` — 10 cases including veto-beats-unanimous,
veto-checked-before-averaging, threshold inclusivity, multiple vetoes all named, and the
empty-input safe skip.
