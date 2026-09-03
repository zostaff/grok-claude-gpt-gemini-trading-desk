"""Grok, the social sentinel: reads X for a launch and grades the social signal.

Grok holds this seat because it is the only panel member with first-party access to X.
That access is **not** implicit in the prompt: since xAI moved live retrieval behind
server-side tools, a model merely *asked* to "search X" will answer from its weights.
The `x_search` tool declared below is what actually makes this agent's premise true.
"""

from __future__ import annotations

from collections.abc import Mapping

import httpx

from ...config.settings import VetoConfig
from ...domain.evaluation import EvaluationContext
from .base import LLMAgent, extract_output_text

PROMPT = """You are a social-signal analyst for Solana memecoin launches.

Use your X search tool. Search for the ticker $${symbol}, the exact phrase "{name}", the
contract address {address}, and the creator account {twitter} if one is given.

TOKEN
name: {name}
symbol: {symbol}
contract: {address}
creator X account: {twitter}
description: {description}
bonding curve completion: {curve:.1f}%
unique buyers so far: {buyers}
volume: {volume:.2f} SOL
age: {age:.1f} minutes

Judge the social footprint. What matters:
- mention_velocity: how fast mentions are accelerating right now, not the raw count.
- whale_signal: are known large or credible accounts talking about it, or only nobodies?
- sentiment_tone: is the talk genuinely positive, or ironic/negative/bagholder cope?
- source_quality: real accounts with history, or eggs created this week?
- coordinated_shilling: how much of the chatter is a paid or botted campaign? Identical
  phrasing, reply-guy swarms, accounts posting the same ticker on a schedule, engagement
  pods. HIGH means it is coordinated. This is the single most important number here.

If you find essentially nothing about this token, that is a low-information result, not a
bullish one: set mention_velocity, whale_signal, sentiment_tone and source_quality near 0
and coordinated_shilling to whatever you actually observed (0 if there is no chatter).

Reply with ONLY this JSON object, no prose, no markdown fence:
{{"mention_velocity": 0.0, "whale_signal": 0.0, "sentiment_tone": 0.0,
"source_quality": 0.0, "coordinated_shilling": 0.0,
"summary": "one sentence, max 200 chars, naming the specific evidence you found"}}"""


class GrokSocialSentinel(LLMAgent):
    """Grades a launch on live X chatter, and vetoes when the chatter looks bought."""

    name = "grok"
    quality_keys = ("mention_velocity", "whale_signal", "sentiment_tone", "source_quality")
    risk_keys = ("coordinated_shilling",)

    def __init__(
        self,
        api_key: str,
        model: str,
        vetoes: VetoConfig,
        *,
        base_url: str = "https://api.x.ai/v1",
        timeout: float = 60.0,
    ) -> None:
        super().__init__(model)
        self.vetoes = vetoes
        self.client = httpx.AsyncClient(
            base_url=base_url,
            headers={"Authorization": f"Bearer {api_key}"},
            # Server-side search makes this the slowest seat on the panel; a 30s timeout
            # cut real answers short and turned them into pessimistic fallbacks.
            timeout=timeout,
        )

    async def aclose(self) -> None:
        """Release the HTTP connection pool."""
        await self.client.aclose()

    def _fallback_scores(self) -> dict[str, float]:
        """A failed social read is treated as a coordinated shill: worst case, veto-worthy."""
        return {
            "mention_velocity": 0.0,
            "whale_signal": 0.0,
            "sentiment_tone": 0.0,
            "source_quality": 0.0,
            "coordinated_shilling": 1.0,
        }

    def _veto(self, scores: Mapping[str, float]) -> tuple[bool, str]:
        """Kill the trade when the chatter looks manufactured."""
        value = scores.get("coordinated_shilling", 0.0)
        if value > self.vetoes.max_coordinated_shilling:
            return True, (
                f"coordinated_shilling {value:.2f} > {self.vetoes.max_coordinated_shilling}"
            )
        return False, ""

    def _build_prompt(self, context: EvaluationContext) -> str:
        """Render the social-scan prompt for one token."""
        token = context.token
        return PROMPT.format(
            name=token.name or "(unnamed)",
            symbol=token.symbol or "?",
            address=token.address,
            twitter=token.twitter or "(none listed)",
            description=(token.description or "(none)")[:200],
            curve=token.bonding_curve_pct,
            buyers=token.unique_buyers,
            volume=token.volume_sol,
            age=token.age_minutes,
        )

    async def _score(self, context: EvaluationContext) -> tuple[dict[str, float], str]:
        """Ask Grok to search X and grade what it finds."""
        prompt = self._build_prompt(context)

        async def call() -> httpx.Response:
            """One request, with the live-search tool actually switched on."""
            resp = await self.client.post(
                "/responses",
                json={
                    "model": self.model,
                    "input": [{"role": "user", "content": prompt}],
                    # Without this the model cannot reach X and will answer from memory.
                    "tools": [{"type": "x_search"}],
                },
            )
            resp.raise_for_status()
            return resp

        response = await self._with_retry(call)
        return self._parse_scores(extract_output_text(response.json()))
