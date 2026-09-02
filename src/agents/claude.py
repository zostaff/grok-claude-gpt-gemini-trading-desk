"""ClaudeWalletAuditor: reads the trade tape and holder table for coordination and dump risk."""

from __future__ import annotations

import time

import anthropic

from ..models import Token
from .base import AgentError, BaseAgent

PROMPT = """You are a forensic on-chain analyst auditing a brand-new pump.fun launch.
You are looking for the signature of an insider setup, not for reasons to be optimistic.

TOKEN
name: {name} (${symbol})
contract: {address}
creator: {creator}
age: {age:.1f} minutes | curve: {curve:.1f}% | unique buyers: {buyers} | volume: {volume:.2f} SOL

THIRD-PARTY RISK REPORT
{risk}

FIRST {n_trades} TRADES (oldest first; "?" means the field could not be resolved)
wallet   | side | SOL     | t+sec  | bal SOL  | wallet age (days)
{trades}

TOP {n_holders} HOLDERS
address  | pct    | sniper | wallet age (days)
{holders}

Notes on the data: "wallet age" is a LOWER BOUND derived from the oldest signature
reachable in one page of history, so a large number is trustworthy but a small one may
just mean a busy wallet. "?" means unavailable — treat it as unknown, never as zero.

Score each of these 0.0 to 1.0. For all five, HIGH means MORE of the named thing.
- coordination_score: how strongly the early buyers look like one operator. Same-block
  entries, near-identical sizes, wallets funded from a common source, a burst of fresh
  wallets in the first seconds.
- wash_trading: buy/sell ping-pong among the same wallets manufacturing fake volume.
- dump_risk: how likely a concentrated holder can and will exit into retail. Weigh top
  holder percentage, sniper flags, and whether early buyers are already selling.
- organic_score: how much of this looks like unrelated people finding the token.
  This is the ONLY one where high is good.
- fresh_wallet_pct: your estimate of the fraction of early buyers that are wallets
  created for this launch.

Reply with ONLY this JSON object, no prose, no markdown fence:
{{"coordination_score": 0.0, "wash_trading": 0.0, "dump_risk": 0.0,
"organic_score": 0.0, "fresh_wallet_pct": 0.0,
"summary": "one sentence, max 200 chars, citing the specific wallets or pattern"}}"""


def _fmt(value: float | None, spec: str, width: int) -> str:
    """Render a number, or a right-sized '?' when the value is unknown."""
    if value is None:
        return "?".ljust(width)
    return format(value, spec).ljust(width)


class ClaudeWalletAuditor(BaseAgent):
    """Audits early trading and holder concentration; vetoes on dump or coordination risk."""

    name = "claude"
    SCORE_KEYS = (
        "coordination_score",
        "wash_trading",
        "dump_risk",
        "organic_score",
        "fresh_wallet_pct",
    )
    MAX_TRADES = 40
    MAX_HOLDERS = 15

    def __init__(self, api_key: str, model: str = "claude-sonnet-4-6") -> None:
        super().__init__(model)
        self.client = anthropic.AsyncAnthropic(api_key=api_key)

    async def aclose(self) -> None:
        """Release the SDK's HTTP connection pool."""
        await self.client.close()

    def _get_fallback(self) -> dict:
        """A failed audit means we know nothing about the wallets: assume the worst of all."""
        return {
            "coordination_score": 1.0,
            "wash_trading": 1.0,
            "dump_risk": 1.0,
            "organic_score": 0.0,
            "fresh_wallet_pct": 1.0,
            "summary": "claude unavailable; assuming coordinated insider setup",
        }

    def _format_trades(self, trades: list[dict]) -> str:
        """Render the first N trades as a fixed-width table the model can scan."""
        if not trades:
            return "(no trades returned by the data API)"
        rows = []
        for trade in trades[: self.MAX_TRADES]:
            wallet = (trade.get("wallet") or "?")[:8].ljust(8)
            side = str(trade.get("side") or "?")[:4].ljust(4)
            amount = _fmt(trade.get("amount_sol"), ".4f", 7)
            after = _fmt(trade.get("seconds_after_launch"), ".0f", 6)
            balance = _fmt(trade.get("balance_sol"), ".3f", 8)
            age = _fmt(trade.get("age_days"), ".1f", 6)
            rows.append(f"{wallet} | {side} | {amount} | {after} | {balance} | {age}")
        return "\n".join(rows)

    def _format_holders(self, holders: list[dict]) -> str:
        """Render the top holders as a fixed-width table."""
        if not holders:
            return "(no holder data returned by the data API)"
        rows = []
        for holder in holders[: self.MAX_HOLDERS]:
            address = (holder.get("address") or "?")[:8].ljust(8)
            pct = _fmt(holder.get("percentage"), ".2f", 6)
            sniper = ("yes" if holder.get("is_sniper") else "no").ljust(6)
            age = _fmt(holder.get("age_days"), ".1f", 6)
            rows.append(f"{address} | {pct} | {sniper} | {age}")
        return "\n".join(rows)

    def _build_prompt(self, token: Token, trades: list[dict], holders: list[dict],
                      risk_summary: str) -> str:
        """Render the audit prompt with both tables and the third-party risk report."""
        return PROMPT.format(
            name=token.name or "(unnamed)",
            symbol=token.symbol or "?",
            address=token.address,
            creator=token.creator_address or "(unknown)",
            age=token.age_minutes,
            curve=token.bonding_curve_pct,
            buyers=token.unique_buyers,
            volume=token.volume_sol,
            risk=risk_summary,
            n_trades=min(len(trades), self.MAX_TRADES),
            n_holders=min(len(holders), self.MAX_HOLDERS),
            trades=self._format_trades(trades),
            holders=self._format_holders(holders),
        )

    async def audit(
        self, token: Token, trades: list[dict], holders: list[dict], risk_summary: str
    ) -> dict:
        """Audit the wallets behind a launch. Never raises; returns the fallback on failure."""
        start = time.monotonic()
        prompt = self._build_prompt(token, trades, holders, risk_summary)

        async def call() -> anthropic.types.Message:
            return await self.client.messages.create(
                model=self.model,
                max_tokens=600,
                messages=[{"role": "user", "content": prompt}],
            )

        try:
            resp = await self._call_with_retry(call)
            text = resp.content[0].text
        except (AgentError, IndexError, AttributeError) as exc:
            print(f"[claude] call failed: {type(exc).__name__}: {exc}")
            out = self._fallback_with("api_error")
            out["latency_ms"] = self._elapsed_ms(start)
            return out

        parsed = self._parse_json(text)
        out = {key: self._clamp01(parsed.get(key)) for key in self.SCORE_KEYS}
        if parsed.get("error"):
            # Unparsed output must not read as a clean bill of health.
            out.update(self._get_fallback())
            out["error"] = parsed["error"]
        out["summary"] = str(parsed.get("summary", ""))[:300]
        out["latency_ms"] = self._elapsed_ms(start)
        print(
            f"[claude] {token.symbol or '?'} coord={out['coordination_score']:.2f} "
            f"dump={out['dump_risk']:.2f} organic={out['organic_score']:.2f} "
            f"({out['latency_ms']}ms)"
        )
        return out
