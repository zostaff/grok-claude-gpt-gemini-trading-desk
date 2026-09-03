# SPEC-003 — The scoring panel

## Responsibility

Produce one `AgentReport` per seat per launch. Each seat wraps one provider and judges one
axis the others cannot see.

It is **not** responsible for deciding anything. A seat scores and may veto; combining
seats is SPEC-004.

## Contract

Implements `ScoringAgent`. The base class `LLMAgent` owns four guarantees so no subclass
can lose them:

| Guarantee | Why it lives in the base |
|---|---|
| latency is always recorded | so `analyse` can name the slow seat |
| the aggregate uses `quality_keys` only | so a danger reading can never be averaged away |
| the veto is the agent's own | so the orchestrator needs no per-agent knowledge |
| `evaluate` never raises | so one dead provider cannot take down a round |

A subclass implements only `_score`, `_fallback_scores` and `_veto`.

### The two failure modes are not the same

```
provider unreachable / reply unparseable  ->  _fallback_scores()  ->  pessimistic, may veto
nothing to judge (neutral_exceptions)     ->  all zeros           ->  no veto
```

Gemini is the case that forces the distinction: a launch with no artwork is uninformative,
not suspicious, while an unreachable vision model leaves us blind to visual scams. Both
went through one code path in an earlier revision, which vetoed every launch that shipped
without a picture.

## The seats

| Seat | Model | Sees what the others cannot | Vetoes on |
|---|---|---|---|
| `GrokSocialSentinel` | `grok-4.6` | live X, via the `x_search` server-side tool | `coordinated_shilling > 0.7` |
| `ClaudeWalletAuditor` | `claude-opus-5` | wallet tables as forensics | `dump_risk > 0.8`, `coordination_score > 0.8` |
| `GPTNarrativeScorer` | `gpt-5.6-sol` | the idea, against live market context | never |
| `GeminiImageAnalyst` | `gemini-3.8-flash` | the artwork | `red_flag_visual > 0.7` |

**Grok's live access is explicit, not implied.** xAI moved live retrieval behind
server-side tools; a model merely asked in a prompt to "search X" answers from its weights.
The adapter declares `tools: [{"type": "x_search"}]`.

**GPT is the only seat with a schema guarantee.** It uses the Responses API's structured
outputs (`responses.parse` + a Pydantic model), so its reply shape is enforced server-side
rather than parsed out of prose. The other three parse JSON defensively — bare, fenced, or
surrounded by prose — and treat an unparseable reply exactly like an outage.

**GPT never vetoes, by design.** A weak meme is a reason not to buy, not evidence of fraud;
letting taste veto a trade the forensics cleared would be a category error.

## Configuration

`models.*` (one model id per seat, plus `claude_effort` / `gpt_effort`),
`vetoes.max_*`, `endpoints.xai`, `endpoints.ipfs_gateway`.

Model ids are config, never constants, precisely because they age.

## Verification

`tests/unit/adapters/test_agent_base.py` — JSON extraction across every real reply shape,
retry policy per HTTP status, risk keys excluded from the aggregate, never-raises, the
neutral path, latency.

`tests/contract/test_port_conformance.py` — port satisfaction, `quality_keys` and
`risk_keys` disjoint, fallback covers every declared key, `evaluate` not overridden away,
every declared risk key can actually trip a veto, seat names unique.

**Not covered:** the provider calls themselves. Prompts and request shapes are written from
current vendor documentation and have not been executed against funded keys.
