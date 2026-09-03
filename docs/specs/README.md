# Component specifications

One spec per component. Each names the port the component implements, the invariants it
must hold, the failure mode it is required to choose, and the tests that hold it to that.

They are written for a reviewer, not for a marketing page: where a decision is arguable,
the spec says what the alternative was and why it lost. Where something is unverified,
it says so.

| Spec | Component | Port | Code | Tests |
|---|---|---|---|---|
| [001](SPEC-001-ingestion.md) | Launch ingestion and the cheap gate | `TokenFeed` | `adapters/feed/pumpportal.py` | `unit/adapters/test_feed_gate.py` |
| [002](SPEC-002-enrichment.md) | Trade tape, holders, wallet enrichment | `MarketDataProvider` | `adapters/market/solana_tracker.py` | contract |
| [003](SPEC-003-panel.md) | The scoring panel | `ScoringAgent` | `adapters/agents/` | `unit/adapters/test_agent_base.py`, contract |
| [004](SPEC-004-consensus.md) | Turning N reports into one action | — (pure domain) | `domain/consensus.py` | `unit/domain/test_consensus.py` |
| [005](SPEC-005-adjudication.md) | The adversarial fifth call | `Adjudicator` | `adapters/agents/adjudicator.py` | `unit/app/test_pipeline.py` |
| [006](SPEC-006-risk.md) | Sizing and the daily brakes | — (pure domain) | `domain/risk.py` | `unit/domain/test_risk.py` |
| [007](SPEC-007-execution.md) | Chain-side effects | `TradeExecutor` | `adapters/execution/stub.py` | contract |
| [008](SPEC-008-journal.md) | The decision record | `DecisionJournal` | `adapters/journal/jsonl.py` | `unit/adapters/test_journal.py` |

## How to read these

Every spec has the same five sections:

- **Responsibility** — the one thing this component is for, and what it explicitly is not for.
- **Contract** — the port it satisfies and the invariants callers may rely on.
- **Failure policy** — what it does when its dependency is down. This is the section worth
  reading first: almost every interesting decision in this system is a choice about how to
  fail, not how to succeed.
- **Configuration** — the keys that change its behaviour.
- **Verification** — the tests that hold it to the above, and what is *not* covered.

## The one rule that shapes everything else

**Risk-direction scores are never averaged into a quality score.** `dump_risk`,
`coordinated_shilling`, `red_flag_visual`, `coordination_score`, `wash_trading` and
`fresh_wallet_pct` all mean "high is worse". Averaging them together with quality signals
would let good artwork cancel out a rug warning. They are excluded from the aggregate by
construction (`LLMAgent.quality_keys` vs `risk_keys`) and drive hard vetoes instead.

This is enforced in three places so it cannot rot: the base class computes the aggregate
from `quality_keys` alone, a contract test asserts the two key sets are disjoint, and a
unit test asserts a maxed-out risk key does not raise the quality score.
