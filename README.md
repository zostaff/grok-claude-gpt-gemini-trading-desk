<div align="center">

# grok-claude-gpt-gemini-trading-desk

**Four frontier LLMs look at the same pump.fun launch from four angles they don't share.
A fifth tries to talk them out of it.**

[![Python](https://img.shields.io/badge/python-3.14-3776AB?style=for-the-badge&logo=python&logoColor=white)](https://www.python.org/)
[![Tests](https://img.shields.io/badge/tests-47%20passing-16a34a?style=for-the-badge&logo=pytest&logoColor=white)](tests/)
[![Mode](https://img.shields.io/badge/default-dry--run-f59e0b?style=for-the-badge)](#quick-start)
[![Executor](https://img.shields.io/badge/executor-stub-dc2626?style=for-the-badge)](#implementing-the-executor)

[![Grok](https://img.shields.io/badge/Grok-social_sentinel-8b5cf6?style=flat-square)](#the-panel)
[![Claude](https://img.shields.io/badge/Claude-wallet_auditor-d97757?style=flat-square)](#the-panel)
[![GPT](https://img.shields.io/badge/GPT-narrative_scorer-10a37f?style=flat-square)](#the-panel)
[![Gemini](https://img.shields.io/badge/Gemini-image_analyst-4285f4?style=flat-square)](#the-panel)
[![Checker](https://img.shields.io/badge/Claude-adversarial_checker-b45309?style=flat-square)](#the-panel)

</div>

---

A multi-model trading pipeline for pump.fun launches on Solana. It listens to the live
token-creation feed, filters new mints down to the few worth thinking about, and then asks
four different LLMs — Grok, Claude, GPT and Gemini — to examine the *same* launch from four
angles they do not share: social chatter, on-chain wallet forensics, narrative quality, and
the artwork itself.

Their scores go to a consensus engine where any single specialist holds a veto. A surviving
"buy" is handed to a fifth call — an adversarial checker whose only job is to find
contradictions between the other four and talk the panel out of the trade. Every buy, every
skip, every disagreement is written to JSONL, so you can go back afterwards and ask whether
four models were actually worth four API calls.

`src/executor.py` is a stub. This is a research and analysis harness, not a bot that will
trade for you.

## Architecture

```mermaid
flowchart TD
    WS(["wss://pumpportal.fun/api/data<br/><i>token creations only</i>"]):::feed

    WS --> F["<b>CodeFilter</b><br/>metadata + curve gate, then metrics<br/>dedup · watch buffer · TTL expiry"]:::stage
    F -->|"~1-3% survive"| D["<b>DataFetcher</b><br/>Solana Tracker: info · holders · trades<br/>Solana RPC: wallet balance + age"]:::stage
    D --> RG{"third-party<br/>rug score &gt; 7?"}:::gate
    RG -->|yes| X1["skip — before a single<br/>LLM token is spent"]:::reject

    RG -->|no| G["<b>Grok</b><br/>social sentinel<br/><code>grok-4-fast</code>"]:::grok
    RG -->|no| C["<b>Claude</b><br/>wallet auditor<br/><code>claude-sonnet-4-6</code>"]:::claude
    RG -->|no| P["<b>GPT</b><br/>narrative scorer<br/><code>gpt-4o</code>"]:::gpt
    RG -->|no| M["<b>Gemini</b><br/>image analyst<br/><code>gemini-2.5-flash</code>"]:::gemini

    G --> CE
    C --> CE
    P --> CE
    M --> CE

    CE["<b>ConsensusEngine</b><br/>veto → vote → spread"]:::stage
    CE -->|"skip / conflict"| X2["logged to<br/>conflicts.jsonl"]:::reject
    CE -->|buy| AC["<b>AdversarialChecker</b><br/>cross-examines all four raw outputs<br/><i>absolute veto</i>"]:::checker
    AC -->|"vetoed / confidence &lt; 0.4"| X2
    AC -->|approved| RM["<b>RiskManager</b><br/>sizing · daily loss · trade cap"]:::stage
    RM -->|"size = 0"| X2
    RM --> EX{{"mode"}}:::gate
    EX -->|dry-run| LOG["<b>TradeLog</b><br/>trades.jsonl + conflicts.jsonl"]:::ok
    EX -->|live| ST["<b>Executor</b><br/>STUB — does nothing"]:::reject
    ST --> LOG

    classDef feed fill:#0f172a,stroke:#475569,stroke-width:2px,color:#e2e8f0
    classDef stage fill:#334155,stroke:#64748b,stroke-width:2px,color:#f1f5f9
    classDef gate fill:#1e293b,stroke:#94a3b8,stroke-width:2px,color:#e2e8f0
    classDef grok fill:#8b5cf6,stroke:#6d28d9,stroke-width:2px,color:#fff
    classDef claude fill:#d97757,stroke:#b45309,stroke-width:2px,color:#fff
    classDef gpt fill:#10a37f,stroke:#047857,stroke-width:2px,color:#fff
    classDef gemini fill:#4285f4,stroke:#1d4ed8,stroke-width:2px,color:#fff
    classDef checker fill:#b45309,stroke:#78350f,stroke-width:2px,color:#fff
    classDef reject fill:#dc2626,stroke:#991b1b,stroke-width:2px,color:#fff
    classDef ok fill:#16a34a,stroke:#15803d,stroke-width:2px,color:#fff
```

The four scoring agents run **concurrently** — they are independent, so the slowest one sets
the round's latency. The checker runs after, because it needs all four outputs to compare.

## Where launches die

Every gate below is ordered by cost: the free checks run first, so that the expensive ones
never see most of the traffic. The percentages are the shape of the funnel, not a measured
backtest — the thresholds are real, from `config.example.yaml`.

```mermaid
flowchart LR
    A["<b>every new mint</b><br/>live WebSocket"]:::s0
    B["<b>metadata + curve</b><br/>free — no API call<br/><code>require_metadata</code><br/><code>max_curve_pct 40</code>"]:::s1
    C["<b>traction metrics</b><br/>1 API call each<br/><code>min_buyers 5</code><br/><code>min_volume_sol 0.5</code><br/><code>min_age_minutes 2</code>"]:::s2
    D["<b>rug report</b><br/><code>risk_score ≤ 7</code>"]:::s3
    E["<b>the panel</b><br/>4 LLM calls"]:::s4
    F["<b>checker</b><br/>5th call"]:::s5
    G["<b>sized entry</b>"]:::s6

    A -->|"100%"| B
    B -->|"~25%"| C
    C -->|"~1-3%"| D
    D --> E
    E --> F
    F --> G

    classDef s0 fill:#0f172a,stroke:#475569,color:#e2e8f0
    classDef s1 fill:#1e3a5f,stroke:#3b82f6,color:#fff
    classDef s2 fill:#1e4620,stroke:#22c55e,color:#fff
    classDef s3 fill:#4a3410,stroke:#f59e0b,color:#fff
    classDef s4 fill:#4a1d5f,stroke:#a855f7,color:#fff
    classDef s5 fill:#5a2d0c,stroke:#f97316,color:#fff
    classDef s6 fill:#166534,stroke:#22c55e,stroke-width:3px,color:#fff
```

That ordering is the whole reason the project is affordable to run: `filter.gate_concurrency`
bounds the paid half, and roughly three quarters of launches are rejected before it.

## Quick start

```bash
git clone https://github.com/zostaff/grok-claude-gpt-gemini-trading-desk
cd grok-claude-gpt-gemini-trading-desk

python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp config.example.yaml config.yaml
$EDITOR config.yaml          # fill in the five API keys

python -m src.pipeline --config config.yaml --dry-run
```

Keys can also come from the environment, which overrides the file, so you never have to
write them to disk:

```bash
export SOLANA_TRACKER_KEY=... XAI_API_KEY=... ANTHROPIC_API_KEY=... \
       OPENAI_API_KEY=... GOOGLE_API_KEY=...
python -m src.pipeline --dry-run
```

If a key is missing or still contains a `YOUR_` placeholder, the pipeline refuses to start
and names the offending key. It never silently skips an agent.

Afterwards, see what the panel actually did:

```bash
python -m src.analysis --log logs/trades.jsonl --conflicts logs/conflicts.jsonl
pytest
```

## The panel

| | Agent | Model | Reads | Scores | Hard veto when |
|---|---|---|---|---|---|
| 🟣 | `GrokSocialSentinel` | `grok-4-fast` | Live X chatter for the ticker, name, contract address and creator account | `mention_velocity` · `whale_signal` · `sentiment_tone` · `source_quality` · `coordinated_shilling` | `coordinated_shilling > 0.7` |
| 🟠 | `ClaudeWalletAuditor` | `claude-sonnet-4-6` | First 40 trades and top 15 holders, each wallet enriched with SOL balance and age | `coordination_score` · `wash_trading` · `dump_risk` · `organic_score` · `fresh_wallet_pct` | `dump_risk > 0.8` **or** `coordination_score > 0.8` |
| 🟢 | `GPTNarrativeScorer` | `gpt-4o` | Name, symbol, description, socials, plus market context refreshed every 15 min | `narrative_fit` · `virality` · `originality` · `community_signal` · `name_quality` | never — a weak meme is not a rug |
| 🔵 | `GeminiImageAnalyst` | `gemini-2.5-flash` | The token artwork itself, downloaded and resized to 1024px | `image_quality` · `meme_strength` · `effort_signal` · `originality_visual` · `red_flag_visual` | `red_flag_visual > 0.7` |
| 🟤 | `AdversarialChecker` | `claude-sonnet-4-6` | All four agents' raw output, side by side | `approve` · `confidence_adjustment` · `missed_risk` | any contradiction it can name |

Grok is asked to search X because it is the only one of the four with live access to it.
Gemini gets the image because the others cannot see it. Claude gets the wallet tables twice
— once to audit them, once to cross-examine the panel — because the second pass is a
different question from the first.

## How the panel decides

Thresholds are live values from `consensus:` in `config.example.yaml`.

```mermaid
flowchart TD
    IN["4 × ModelVerdict<br/><i>score, hard_veto, summary</i>"]:::in

    IN --> V{"any<br/><b>hard_veto</b>?"}:::gate
    V -->|yes| SK1["<b>SKIP</b><br/>confidence 0.0<br/><i>one specialist outranks three generalists</i>"]:::bad

    V -->|no| CL["classify each verdict<br/><b>bull</b> ≥ 0.55 · <b>bear</b> &lt; 0.45"]:::step
    CL --> AGG["avg = mean of scores<br/>agreement = bulls / 4<br/>spread = max − min"]:::step

    AGG --> Q1{"agreement ≥ <b>0.75</b><br/>AND<br/>avg ≥ <b>0.60</b>?"}:::gate
    Q1 -->|yes| BUY["<b>BUY</b><br/>confidence = avg × agreement"]:::good
    Q1 -->|no| Q2{"spread &gt; <b>0.40</b>?"}:::gate
    Q2 -->|yes| CF["<b>CONFLICT</b><br/>names the dissenter and quotes it<br/>→ conflicts.jsonl"]:::warn
    Q2 -->|no| SK2["<b>SKIP</b><br/>low conviction"]:::bad

    classDef in fill:#0f172a,stroke:#475569,color:#e2e8f0
    classDef step fill:#334155,stroke:#64748b,color:#f1f5f9
    classDef gate fill:#1e293b,stroke:#94a3b8,stroke-width:2px,color:#e2e8f0
    classDef good fill:#16a34a,stroke:#15803d,stroke-width:3px,color:#fff
    classDef warn fill:#f59e0b,stroke:#b45309,stroke-width:2px,color:#1f2937
    classDef bad fill:#dc2626,stroke:#991b1b,stroke-width:2px,color:#fff
```

The ordering matters. A hard veto short-circuits **before** averaging, because a rug flagged
by one specialist must not be outvoted by three generalists who liked the picture.

## One token, end to end

```mermaid
sequenceDiagram
    autonumber
    participant F as CodeFilter
    participant D as DataFetcher
    participant G as 🟣 Grok
    participant C as 🟠 Claude
    participant P as 🟢 GPT
    participant M as 🔵 Gemini
    participant K as 🟤 Checker
    participant R as RiskManager
    participant L as TradeLog

    F->>D: token clears the gate
    D->>D: trades · holders · wallet age · rug report
    alt rug score > 7
        D-->>L: skip (no LLM call made)
    else clean enough to think about
        par four calls, concurrently
            D->>G: token + creator
        and
            D->>C: 40 trades + 15 holders
        and
            D->>P: metadata + market context
        and
            D->>M: artwork @ 1024px
        end
        G-->>K: verdict
        C-->>K: verdict
        P-->>K: verdict
        M-->>K: verdict
        Note over K: consensus first —<br/>the checker only sees a "buy"
        K->>K: hunt for contradictions
        alt approved and confidence ≥ 0.4
            K->>R: size it
            R-->>L: entry (or DRY_RUN BUY)
        else vetoed
            K-->>L: conflicts.jsonl
        end
    end
```

## Risk management

Defaults in `config.example.yaml`:

| Setting | Default | Meaning |
|---|---|---|
| `max_position_sol` | `0.1` | Hard ceiling on any single entry |
| `min_position_sol` | `0.005` | Below this, don't bother |
| `daily_loss_limit_sol` | `0.5` | Trading halts for the day when reached |
| `max_daily_trades` | `10` | Halts on count too, not just loss |
| `max_open_positions` | `3` | Concurrency cap |
| `stop_loss_pct` / `take_profit_pct` | `50` / `100` | Exit triggers (executor stub) |
| `max_hold_minutes` | `30` | Time-based exit |

Sizing is `max_position_sol × (avg_score × final_confidence)`, then capped at **30% of what
is left** of the daily loss budget.

### A losing session shrinks its own positions

This is the part worth seeing as a curve. Same conviction (`0.50`) at every point — only the
remaining daily budget changes:

```mermaid
xychart-beta
    title "Position size vs remaining daily budget (conviction fixed at 0.50)"
    x-axis "remaining daily budget, SOL" ["0.50", "0.40", "0.30", "0.20", "0.15", "0.10", "0.05", "0.03", "0.02", "0.01"]
    y-axis "position size, SOL" 0 --> 0.06
    bar [0.050, 0.050, 0.050, 0.050, 0.045, 0.030, 0.015, 0.009, 0.006, 0.000]
    line [0.050, 0.050, 0.050, 0.050, 0.045, 0.030, 0.015, 0.009, 0.006, 0.000]
```

| remaining budget | 30% cap | position size | |
|---:|---:|---:|---|
| 0.500 | 0.1500 | **0.0500** | conviction binds |
| 0.400 | 0.1200 | **0.0500** | conviction binds |
| 0.300 | 0.0900 | **0.0500** | conviction binds |
| 0.200 | 0.0600 | **0.0500** | conviction binds |
| 0.150 | 0.0450 | **0.0450** | ← budget starts binding |
| 0.100 | 0.0300 | **0.0300** | budget binds |
| 0.050 | 0.0150 | **0.0150** | budget binds |
| 0.030 | 0.0090 | **0.0090** | budget binds |
| 0.020 | 0.0060 | **0.0060** | budget binds |
| 0.010 | 0.0030 | **0.0000** | ← cap fell under `min_position_sol`, trade skipped |

The cliff at the bottom is deliberate: when the remaining budget cannot cover even the
minimum position, `position_size` returns `0.0` and the caller treats it as *do not trade*,
never as a rounding artefact. A bad day therefore ends by starving itself, not by
doubling down.

### Conviction, at a fresh budget

```mermaid
xychart-beta
    title "Position size vs conviction (score × confidence), full daily budget"
    x-axis "conviction" ["0.015", "0.04", "0.10", "0.18", "0.30", "0.42", "0.60", "0.81", "1.00"]
    y-axis "position size, SOL" 0 --> 0.11
    line [0.005, 0.005, 0.010, 0.018, 0.030, 0.042, 0.060, 0.081, 0.100]
```

Linear in conviction, with a floor at `min_position_sol` — below `0.05` conviction the size
flattens rather than dwindling to dust.

## Two design decisions worth knowing about

**Risk scores are never averaged into the aggregate.** `coordinated_shilling`, `dump_risk`,
`coordination_score`, `wash_trading`, `fresh_wallet_pct` and `red_flag_visual` all mean
"high is worse". Averaging them with quality scores would let a beautiful picture cancel out
a rug warning, so `INVERTED_KEYS` excludes them from the aggregate entirely and they drive
the hard vetoes instead. That is why `ClaudeWalletAuditor`'s aggregate is effectively
`organic_score` alone.

**Failure is pessimistic, but absence of data is not.** When an agent's API call or JSON
parse fails, it returns its worst-case scores — a blind analyst must not read as a clean
bill of health. The one exception is a token with no artwork: Gemini scores it all zeros
with `red_flag_visual = 0`, because a missing image is a fact about the launch, not a system
failure.

## Two things that will bite you

**1. pump.fun's public WebSocket only broadcasts token *creations*.** A create frame has no
trade history and no description or image — those live in the off-chain metadata JSON at
`uri`, which the filter fetches separately. Per-token trade streams (`subscribeTokenTrade`)
exist, but PumpPortal gates them behind an API key funded with at least 0.02 SOL:

```
'subscribeTokenTrade' and 'subscribeAccountTrade' methods are only available when
connecting with an API key funded with at least 0.02 SOL.
```

So the filter supports both real paths:

| | `pumpportal_api_key` set | left empty |
|---|---|---|
| **buyers / volume** | live off the same socket | one Solana Tracker poll per candidate |
| **extra cost** | none | 1 API call per launch watched |
| **bounded by** | — | `filter.gate_concurrency` |

Either path is correct. The cheap half of the gate runs first in both, so most launches are
rejected before any call is made.

**2. `google-generativeai` is end-of-life.** It still works and is what `requirements.txt`
pins, but it prints a `FutureWarning` on import and Google has stopped updating it. The
replacement is `google-genai`, a small change in `src/agents/gemini.py`:

```python
from google import genai
client = genai.Client(api_key=api_key)
resp = await client.aio.models.generate_content(model=self.model, contents=[prompt, img])
```

That version is natively async and would let you drop the `asyncio.to_thread` wrapper.

## Implementing the executor

`src/executor.py` is the only stubbed file in the project. To make it real you need:

- **`build_buy_tx`** — a pump.fun buy instruction against the bonding curve, with an
  associated token account created if absent, and slippage bounds from `solana.slippage_bps`.
- **`send_with_priority`** — sign with the wallet key, attach a Jito tip, submit, confirm.
- **`get_current_price`** — read the curve's virtual reserves for the mint.
- **`monitor_and_stop`** — poll price, exit on stop-loss, take-profit or the hold timeout.

Every stub already returns a correctly shaped dict, so the pipeline logs and the risk
bookkeeping work in dry-run without them.

## Layout

```
src/
  config.py      pydantic settings, YAML + env, loud validation
  models.py      Token / ModelVerdict / ConsensusResult
  filter.py      live WebSocket, watch buffer, metric gate, dedup
  data.py        Solana Tracker REST + RPC wallet enrichment + TTL cache
  agents/        base.py (parse, retry, latency) + the five agents
  consensus.py   veto -> vote -> spread
  risk.py        sizing and the daily brakes
  executor.py    STUBS ONLY
  log.py         JSONL writers
  pipeline.py    the orchestrator
  analysis.py    post-run: who dissents, who vetoes, who is dead weight
tests/           consensus, filter, parse, risk — 47 tests
```

## A note on four models

Four models agreeing is not four independent confirmations. They share training data, they
share biases, and they will confidently agree with each other about a token that is about to
go to zero. The adversarial checker exists precisely because consensus among language models
is much weaker evidence than it appears — and `analysis.py` exists so you can check whether
any given agent ever changed an outcome, or was just an expensive fifth opinion.
