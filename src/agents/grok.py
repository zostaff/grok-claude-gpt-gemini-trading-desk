"""GrokSocialSentinel: asks Grok to read X for a launch and grade the social signal."""

from __future__ import annotations

import time

import httpx

from ..models import Token
from .base import AgentError, BaseAgent

PROMPT = """You are a social-signal analyst for Solana memecoin launches.

Search X (Twitter) right now for the ticker $${symbol} and for the exact phrase "{name}".
Also check the creator account {twitter} if one is given, and search for the contract
address {address}.

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
and coordinated_shilling to whatever you actually observed (0 if there is no chatter at all).

Reply with ONLY this JSON object, no prose, no markdown fence:
{{"mention_velocity": 0.0, "whale_signal": 0.0, "sentiment_tone": 0.0,
"source_quality": 0.0, "coordinated_shilling": 0.0,
"summary": "one sentence, max 200 chars, naming the specific evidence you found"}}"""


class GrokSocialSentinel(BaseAgent):
    """Grades a launch on live X chatter, and vetoes when the chatter looks bought."""

    name = "grok"
    SCORE_KEYS = (
        "mention_velocity",
        "whale_signal",
        "sentiment_tone",
        "source_quality",
        "coordinated_shilling",
    )

    def __init__(self, api_key: str, model: str = "grok-4-fast") -> None:
        super().__init__(model)
        self.client = httpx.AsyncClient(
            base_url="https://api.x.ai/v1",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=30,
        )

    async def aclose(self) -> None:
        """Release the HTTP connection pool."""
        await self.client.aclose()

    def _get_fallback(self) -> dict:
        """A failed social read is treated as a coordinated shill: worst case, veto-worthy."""
        return {
            "mention_velocity": 0.0,
            "whale_signal": 0.0,
            "sentiment_tone": 0.0,
            "source_quality": 0.0,
            "coordinated_shilling": 1.0,
            "summary": "grok unavailable; assuming coordinated shilling",
        }

    def _build_prompt(self, token: Token) -> str:
        """Render the social-scan prompt for one token."""
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

    async def scan(self, token: Token) -> dict:
        """Score the token's social footprint. Never raises; returns the fallback on failure."""
        start = time.monotonic()
        prompt = self._build_prompt(token)

        async def call() -> httpx.Response:
            resp = await self.client.post(
                "/chat/completions",
                json={
                    "model": self.model,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0,
                },
            )
            resp.raise_for_status()
            return resp

        try:
            resp = await self._call_with_retry(call)
            text = resp.json()["choices"][0]["message"]["content"]
        except (AgentError, KeyError, IndexError, TypeError, ValueError) as exc:
            print(f"[grok] call failed: {type(exc).__name__}: {exc}")
            out = self._fallback_with("api_error")
            out["latency_ms"] = self._elapsed_ms(start)
            return out

        parsed = self._parse_json(text)
        out = {key: self._clamp01(parsed.get(key)) for key in self.SCORE_KEYS}
        # A parse failure must not read as "no shilling detected".
        if parsed.get("error"):
            out["coordinated_shilling"] = 1.0
            out["error"] = parsed["error"]
        out["summary"] = str(parsed.get("summary", ""))[:300]
        out["latency_ms"] = self._elapsed_ms(start)
        print(
            f"[grok] {token.symbol or '?'} velocity={out['mention_velocity']:.2f} "
            f"shill={out['coordinated_shilling']:.2f} ({out['latency_ms']}ms)"
        )
        return out
