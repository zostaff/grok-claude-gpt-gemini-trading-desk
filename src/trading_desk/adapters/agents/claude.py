"""Claude, the wallet auditor: reads the trade tape and holder table as forensics."""

from __future__ import annotations

from collections.abc import Mapping

import anthropic

from ...config.settings import Effort, VetoConfig
from ...domain.evaluation import EvaluationContext
from .base import AgentError, LLMAgent, anthropic_text

PROMPT = """You are a forensic on-chain analyst auditing a brand-new pump.fun launch.

TOKEN
name: {name} (${symbol})
contract: {address}
creator: {creator}
age: {age:.1f} minutes | curve: {curve:.1f}% | buyers: {buyers} | volume: {volume:.2f} SOL

THIRD-PARTY RISK REPORT
{risk_summary}

FIRST TRADES (chronological)
{trades}

TOP HOLDERS
{holders}

Judge the wallet behaviour, not the idea. What matters:
- coordination_score: do the buys look like one entity through many wallets? Same size,
  same interval, wallets funded from a common source, wallets created together.
- wash_trading: buy/sell churn that manufactures volume without changing net position.
- dump_risk: how exposed is this to a single holder exiting? Concentration in the top
  wallets, the creator still holding a large share, fresh wallets sitting on big bags.
- organic_score: does this look like real independent people finding a token? This is
  the ONLY positive signal you produce; the other three are danger readings.
- fresh_wallet_pct: share of participants whose wallets are new and thinly funded.

A wallet with almost no SOL and no history that just bought a large position is the
strongest single signal of a coordinated launch. Say so in the summary if you see it.

Reply with ONLY this JSON object, no prose, no markdown fence:
{{"coordination_score": 0.0, "wash_trading": 0.0, "dump_risk": 0.0,
"organic_score": 0.0, "fresh_wallet_pct": 0.0,
"summary": "one sentence, max 200 chars, citing the specific wallets or pattern"}}"""


def _fmt(value: float | None, spec: str, width: int) -> str:
    """Format a possibly-missing number into a fixed-width table cell."""
    if value is None:
        return "?".rjust(width)
    return format(value, spec).rjust(width)


class ClaudeWalletAuditor(LLMAgent):
    """Audits trades and holders for coordination; vetoes on dump or coordination risk."""

    name = "claude"
    quality_keys = ("organic_score",)
    risk_keys = ("coordination_score", "wash_trading", "dump_risk", "fresh_wallet_pct")

    MAX_TRADES = 40
    MAX_HOLDERS = 15

    def __init__(
        self,
        api_key: str,
        model: str,
        vetoes: VetoConfig,
        *,
        effort: Effort = "high",
        max_tokens: int = 4096,
    ) -> None:
        super().__init__(model)
        self.vetoes = vetoes
        self.effort = effort
        self.max_tokens = max_tokens
        self.client = anthropic.AsyncAnthropic(api_key=api_key)

    async def aclose(self) -> None:
        """The Anthropic async client holds a connection pool worth closing."""
        await self.client.close()

    def _fallback_scores(self) -> dict[str, float]:
        """A failed audit means we are blind to coordination: assume the worst."""
        return {
            "coordination_score": 1.0,
            "wash_trading": 1.0,
            "dump_risk": 1.0,
            "organic_score": 0.0,
            "fresh_wallet_pct": 1.0,
        }

    def _veto(self, scores: Mapping[str, float]) -> tuple[bool, str]:
        """Kill the trade on either concentration risk or coordinated buying."""
        dump = scores.get("dump_risk", 0.0)
        coordination = scores.get("coordination_score", 0.0)
        if dump > self.vetoes.max_dump_risk:
            return True, f"dump_risk {dump:.2f} > {self.vetoes.max_dump_risk}"
        if coordination > self.vetoes.max_coordination_score:
            return True, (
                f"coordination_score {coordination:.2f} > {self.vetoes.max_coordination_score}"
            )
        return False, ""

    def _format_trades(self, trades: list[dict]) -> str:
        """Render the trade tape as a fixed-width table the model can scan."""
        if not trades:
            return "(no trades returned)"
        lines = ["   # side    SOL      wallet        wallet_SOL  age_days"]
        for i, t in enumerate(trades[: self.MAX_TRADES], 1):
            wallet = str(t.get("wallet", "?"))
            lines.append(
                f"{i:>4} {t.get('side', '?')!s:<6} "
                f"{_fmt(t.get('amount_sol'), '.3f', 8)} "
                f"{wallet[:12]:<13} "
                f"{_fmt(t.get('wallet_sol'), '.3f', 10)} "
                f"{_fmt(t.get('wallet_age_days'), '.1f', 9)}"
            )
        return "\n".join(lines)

    def _format_holders(self, holders: list[dict]) -> str:
        """Render the holder table as a fixed-width table the model can scan."""
        if not holders:
            return "(no holder data returned)"
        lines = ["rank  pct     wallet        wallet_SOL"]
        for i, h in enumerate(holders[: self.MAX_HOLDERS], 1):
            wallet = str(h.get("wallet", "?"))
            lines.append(
                f"{i:>4} {_fmt(h.get('pct'), '.2f', 7)} "
                f"{wallet[:12]:<13} {_fmt(h.get('wallet_sol'), '.3f', 10)}"
            )
        return "\n".join(lines)

    def _build_prompt(self, context: EvaluationContext) -> str:
        """Render the forensic prompt for one token."""
        token = context.token
        return PROMPT.format(
            name=token.name or "(unnamed)",
            symbol=token.symbol or "?",
            address=token.address,
            creator=token.creator_address or "(unknown)",
            age=token.age_minutes,
            curve=token.bonding_curve_pct,
            buyers=token.unique_buyers,
            volume=token.volume_sol,
            risk_summary=context.risk_summary,
            trades=self._format_trades(context.trades),
            holders=self._format_holders(context.holders),
        )

    async def _score(self, context: EvaluationContext) -> tuple[dict[str, float], str]:
        """Ask Claude to audit the wallets behind the launch."""
        prompt = self._build_prompt(context)

        async def call() -> anthropic.types.Message:
            """One request with adaptive thinking; depth is set by `effort`."""
            return await self.client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                thinking={"type": "adaptive"},
                output_config={"effort": self.effort},
                messages=[{"role": "user", "content": prompt}],
            )

        message = await self._with_retry(call)

        # A safety decline is not an audit. Treat it like any other missing answer so it
        # lands in the pessimistic fallback instead of being read as a clean bill.
        if getattr(message, "stop_reason", None) == "refusal":
            raise AgentError(f"{self.name}: provider declined the request")

        text = anthropic_text(message)
        return self._parse_scores(text)
