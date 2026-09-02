"""AdversarialChecker: a second Claude call whose only job is to talk the panel out of it."""

from __future__ import annotations

import json
import time

import anthropic

from ..models import ConsensusResult, Token
from .base import AgentError, BaseAgent

PROMPT = """Four independent models just voted to BUY this pump.fun launch. Your job is to
argue against them. You are the last gate before real money moves. Approving a bad trade
costs money; rejecting a good one costs nothing but an opportunity.

TOKEN
name: {name} (${symbol})
contract: {address}
creator: {creator}
age: {age:.1f} min | curve: {curve:.1f}% | unique buyers: {buyers} | volume: {volume:.2f} SOL
description: {description}
links: twitter={twitter} website={website} telegram={telegram}

THIRD-PARTY RISK REPORT
{risk}

PANEL DECISION
action={action} confidence={confidence:.3f} agreement={agreement:.2f}
bulls: {bulls}
bears: {bears}
conflict note: {conflict}

RAW AGENT OUTPUT (verbatim, including each agent's own summary)
{verdicts}

Do this, in order:
1. Cross-reference the agents against each other. Find CONTRADICTIONS: places where one
   agent's evidence makes another's conclusion impossible. Grok reporting heavy organic
   chatter while Claude sees six fresh wallets and no real volume is a contradiction.
   Gemini praising high-effort custom art on a token whose description is empty and whose
   links are dead is a contradiction. A high organic_score next to a high fresh_wallet_pct
   is a contradiction inside a single agent.
2. Check whether any agent scored well on ABSENCE of data rather than presence of good
   data. "Nothing suspicious found" is not the same as "verified clean", and a low
   coordinated_shilling on a token with no chatter at all means nothing.
3. Name the single largest risk the panel did not price in.
4. Decide. Reject if you find a real contradiction, if the bullish case rests on missing
   data, or if the third-party risk report contains something the panel ignored.

confidence_adjustment is added to the panel's confidence: use -1.0 to kill the trade
outright, a negative fraction to dampen it, 0.0 to leave it alone, and at most +0.1 if the
agents genuinely corroborate each other on independent evidence.

Reply with ONLY this JSON object, no prose, no markdown fence:
{{"approve": true, "veto_reason": "empty string if approving, else the specific reason",
"missed_risk": "the largest unpriced risk, one sentence",
"confidence_adjustment": 0.0}}"""


class AdversarialChecker(BaseAgent):
    """Cross-examines the four verdicts for contradictions before any capital is committed."""

    name = "checker"

    def __init__(self, api_key: str, model: str = "claude-sonnet-4-6") -> None:
        super().__init__(model)
        self.client = anthropic.AsyncAnthropic(api_key=api_key)

    async def aclose(self) -> None:
        """Release the SDK's HTTP connection pool."""
        await self.client.close()

    def _get_fallback(self) -> dict:
        """If the last gate cannot run, the gate is closed."""
        return {
            "approve": False,
            "veto_reason": "adversarial checker unavailable",
            "missed_risk": "unknown; the check did not run",
            "confidence_adjustment": -1.0,
        }

    @staticmethod
    def _format_verdicts(result: ConsensusResult) -> str:
        """Dump every agent's full raw output so contradictions are visible side by side."""
        blocks = []
        for verdict in result.verdicts:
            raw = json.dumps(verdict.raw, indent=2, sort_keys=True, default=str)
            blocks.append(
                f"--- {verdict.model} (aggregate score {verdict.score:.3f}, "
                f"{verdict.latency_ms}ms) ---\n{raw}"
            )
        return "\n\n".join(blocks) or "(no verdicts)"

    def _build_prompt(self, token: Token, result: ConsensusResult, risk_summary: str) -> str:
        """Render the cross-examination prompt."""
        return PROMPT.format(
            name=token.name or "(unnamed)",
            symbol=token.symbol or "?",
            address=token.address,
            creator=token.creator_address or "(unknown)",
            age=token.age_minutes,
            curve=token.bonding_curve_pct,
            buyers=token.unique_buyers,
            volume=token.volume_sol,
            description=(token.description or "(none)")[:400],
            twitter=token.twitter or "(none)",
            website=token.website or "(none)",
            telegram=token.telegram or "(none)",
            risk=risk_summary,
            action=result.action,
            confidence=result.confidence,
            agreement=result.agreement_ratio,
            bulls=", ".join(result.bull_models) or "(none)",
            bears=", ".join(result.bear_models) or "(none)",
            conflict=result.conflict_detail or "(none)",
            verdicts=self._format_verdicts(result),
        )

    async def check(self, token: Token, result: ConsensusResult, risk_summary: str) -> dict:
        """Cross-examine the panel. Never raises; a failed check rejects the trade."""
        start = time.monotonic()
        prompt = self._build_prompt(token, result, risk_summary)

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
            print(f"[checker] call failed: {type(exc).__name__}: {exc}")
            out = self._fallback_with("api_error")
            out["latency_ms"] = self._elapsed_ms(start)
            return out

        parsed = self._parse_json(text)
        if parsed.get("error"):
            out = self._fallback_with(parsed["error"])
            out["latency_ms"] = self._elapsed_ms(start)
            return out

        try:
            adjustment = float(parsed.get("confidence_adjustment", 0.0))
        except (TypeError, ValueError):
            adjustment = -1.0
        out = {
            "approve": bool(parsed.get("approve", False)),
            "veto_reason": str(parsed.get("veto_reason", ""))[:400],
            "missed_risk": str(parsed.get("missed_risk", ""))[:400],
            "confidence_adjustment": max(-1.0, min(1.0, adjustment)),
            "latency_ms": self._elapsed_ms(start),
        }
        verdict = "APPROVE" if out["approve"] else "VETO"
        print(
            f"[checker] {token.symbol or '?'} {verdict} adj={out['confidence_adjustment']:+.2f} "
            f"({out['latency_ms']}ms) {out['veto_reason'] or out['missed_risk']}"
        )
        return out
