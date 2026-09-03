# grok-claude-gpt-gemini-trading-desk

A multi-model trading pipeline for pump.fun launches on Solana. It listens to the live
token-creation feed, filters new mints down to the few worth thinking about, and then asks
four different LLMs — Grok, Claude, GPT and Gemini — to examine the *same* launch from four
angles they do not share: social chatter, on-chain wallet forensics, narrative quality, and
the artwork itself. Their scores are combined by a consensus engine where any single
specialist holds a veto, and a surviving "buy" is then handed to a fifth call, an
adversarial checker, whose only job is to find contradictions between the other four and
talk the panel out of the trade. Everything — every buy, every skip, every disagreement —
is written to JSONL so you can go back afterwards and ask whether four models were actually
worth four API calls.

**The executor is a stub.** This is a research and analysis harness, not a bot that will
trade for you.

## Architecture

```
                    wss://pumpportal.fun/api/data
                                 |
                                 v
                    +------------------------+
                    |      CodeFilter        |   new mints -> watch buffer
                    |  metrics + dedup gate  |   metadata from token URI
                    +------------------------+   buyers/volume from WS or REST
                                 | ~1-3% of launches survive
                                 v
                    +------------------------+
                    |      DataFetcher       |   Solana Tracker: info/holders/trades
                    |  + RPC wallet enrich   |   Solana RPC: balances, wallet age
                    +------------------------+
                                 |
                                 v  (rug-risk score > 7 -> skip before spending tokens)
        +------------+-----------+-----------+------------+
        |            |                       |            |
        v            v                       v            v
   +---------+  +----------+          +-----------+  +----------+
   |  Grok   |  |  Claude  |          |    GPT    |  |  Gemini  |
   | social  |  |  wallet  |          | narrative |  |  image   |
   | sentinel|  | auditor  |          |  scorer   |  | analyst  |
   +---------+  +----------+          +-----------+  +----------+
        |            |                       |            |
        +------------+-----------+-----------+------------+
                                 |  4 x ModelVerdict (score, hard_veto)
                                 v
                    +------------------------+
                    |    ConsensusEngine     |   1. any hard veto -> skip
                    |  veto -> vote -> spread|   2. agreement + average -> buy
                    +------------------------+   3. wide spread -> conflict
                                 | action == "buy"
                                 v
                    +------------------------+
                    |   AdversarialChecker   |   cross-examines all four raw outputs
                    |   (Claude, 5th call)   |   looks for contradictions between them
                    +------------------------+
                                 | approved, confidence adjusted
                                 v
                    +------------------------+
                    |      RiskManager       |   sizing, daily loss limit, trade cap
                    +------------------------+
                                 |
                    +------------+------------+
                    |                         |
                    v                         v
             dry-run: log it            +-----------+
             "DRY_RUN BUY ..."          | Executor  |  <-- STUB. Does nothing.
                    |                   +-----------+
                    v                         |
              +-----------------------------------+
              |  TradeLog: trades.jsonl           |
              |            conflicts.jsonl        |
              +-----------------------------------+
```

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

If a key is missing or still contains an example placeholder, the pipeline refuses to
start and names the offending key. It never silently skips an agent.

Afterwards, see what the panel actually did:

```bash
python -m src.analysis --log logs/trades.jsonl --conflicts logs/conflicts.jsonl
pytest
```

## The agents

| Agent | Model | Looks at | Scores | Vetoes when |
|---|---|---|---|---|
| `GrokSocialSentinel` | `grok-4-fast` (xAI) | Live X chatter for the ticker, the name, the contract address and the creator account | `mention_velocity`, `whale_signal`, `sentiment_tone`, `source_quality`, `coordinated_shilling` | `coordinated_shilling > 0.7` |
| `ClaudeWalletAuditor` | `claude-sonnet-4-6` (Anthropic) | The first 40 trades and top 15 holders, with per-wallet SOL balance and age | `coordination_score`, `wash_trading`, `dump_risk`, `organic_score`, `fresh_wallet_pct` | `dump_risk > 0.8` or `coordination_score > 0.8` |
| `GPTNarrativeScorer` | `gpt-4o` (OpenAI) | Name, symbol, description, socials, and a market context refreshed every 15 min | `narrative_fit`, `virality`, `originality`, `community_signal`, `name_quality` | never — a weak meme is not a rug |
| `GeminiImageAnalyst` | `gemini-2.5-flash` (Google) | The token artwork itself, downloaded and resized to 1024px | `image_quality`, `meme_strength`, `effort_signal`, `originality_visual`, `red_flag_visual` | `red_flag_visual > 0.7` |
| `AdversarialChecker` | `claude-sonnet-4-6` (Anthropic) | All four agents' raw output, side by side | `approve`, `confidence_adjustment` | any contradiction it can name |

Grok is asked to search X because it is the only one of the four with live access to it.
Gemini gets the image because the others cannot see it. Claude gets the wallet tables twice
— once to audit them, once to cross-examine the panel — because the second pass is a
different question from the first.

Two design decisions worth knowing about:

**Risk scores are never averaged into the aggregate.** `coordinated_shilling`, `dump_risk`,
`coordination_score`, `wash_trading`, `fresh_wallet_pct` and `red_flag_visual` all mean
"high is worse". Averaging them with quality scores would let a beautiful picture cancel out
a rug warning, so they are excluded from the aggregate entirely and drive the hard vetoes
instead. That is why `ClaudeWalletAuditor`'s aggregate is effectively `organic_score` alone.

**Failure is pessimistic, but absence of data is not.** When an agent's API call or JSON
parse fails, it returns its worst-case scores — a blind analyst must not read as a clean
bill of health. The one exception is a token with no artwork: Gemini scores it all zeros
with `red_flag_visual = 0`, because a missing image is a fact about the launch, not a system
failure.

## Risk management

Defaults in `config.example.yaml`:

| Setting | Default | Meaning |
|---|---|---|
| `max_position_sol` | 0.1 | Hard ceiling on any single entry |
| `min_position_sol` | 0.005 | Below this, don't bother |
| `daily_loss_limit_sol` | 0.5 | Trading halts for the day when reached |
| `max_daily_trades` | 10 | Halts on count too, not just loss |
| `max_open_positions` | 3 | Concurrency cap |
| `stop_loss_pct` | 50 | Exit trigger (executor stub) |

Sizing is `max_position_sol * (avg_score * final_confidence)`, then capped at 30% of what is
left of the daily loss budget. A losing session therefore shrinks its own position sizes
rather than doubling down, and when the remaining budget cannot cover the minimum position
the manager returns zero and the trade is skipped.

## Two things that will bite you

**1. pump.fun's public WebSocket only broadcasts token *creations*.** A create frame has no
trade history and no description or image — those live in the off-chain metadata JSON at
`uri`, which the filter fetches separately. Per-token trade streams (`subscribeTokenTrade`)
exist, but PumpPortal gates them behind an API key funded with at least 0.02 SOL:

```
'subscribeTokenTrade' and 'subscribeAccountTrade' methods are only available when
connecting with an API key funded with at least 0.02 SOL.
```

So the filter supports both real paths. Set `pumpportal_api_key` and buyer/volume metrics
come live off the same socket for free. Leave it empty and the filter instead polls Solana
Tracker's trade tape once per candidate — the same numbers, but one API call per launch
watched. To keep that affordable, the cheap half of the gate (metadata present, curve not
already too far along) runs *first* and rejects most launches before any call is made, and
`filter.gate_concurrency` bounds the rest. In a live run, roughly a quarter of launches
reached the paid half of the gate.

**2. `google-generativeai` is end-of-life.** It still works and is what `requirements.txt`
pins, but it prints a `FutureWarning` on import and Google has stopped updating it. The
replacement is `google-genai`, which is a small change in `src/agents/gemini.py`:

```python
from google import genai
client = genai.Client(api_key=api_key)
resp = await client.aio.models.generate_content(model=self.model, contents=[prompt, img])
```

That version is natively async and would let you drop the `asyncio.to_thread` wrapper.

## Implementing the executor

`src/executor.py` is the only stubbed file in the project. To make it real you need:

- `build_buy_tx` — a pump.fun buy instruction against the bonding curve, with an associated
  token account created if absent, and slippage bounds from `solana.slippage_bps`.
- `send_with_priority` — sign with the wallet key, attach a Jito tip, submit, confirm.
- `get_current_price` — read the curve's virtual reserves for the mint.
- `monitor_and_stop` — poll price, exit on stop-loss, take-profit or the hold timeout.

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
tests/           consensus, filter, parse, risk
```
