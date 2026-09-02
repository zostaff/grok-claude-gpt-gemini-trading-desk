"""GPTNarrativeScorer: judges whether the meme itself has any reason to spread."""

from __future__ import annotations

import time

import openai

from ..models import Token
from .base import AgentError, BaseAgent

PROMPT = """You are a memecoin narrative analyst. Judge whether THIS token's idea has a
reason to spread, independent of its chart or its wallets. You are not asked whether it
is a scam; other analysts cover that. You are asked whether the meme is any good.

TOKEN
name: {name}
symbol: ${symbol}
contract: {address}
description: {description}

SOCIAL LINKS
twitter: {twitter}
website: {website}
telegram: {telegram}

MARKET CONTEXT (as of {context_time})
SOL 24h change: {sol_24h}
currently trending narratives: {trending}
notes: {notes}

Score each 0.0 to 1.0, where high is good:
- narrative_fit: does this ride a narrative the market is actually paying for right now,
  per the context above? A perfectly executed meme about a dead trend scores low.
- virality: is it screenshot-able and repeatable? Would somebody post this unpaid?
- originality: fresh, or the ninth derivative of a dog coin this week?
- community_signal: do the links suggest a real community was built before launch, or
  were they filled in for the checkbox? No links at all is a 0.
- name_quality: is the ticker and name memorable, pronounceable, and searchable? A name
  that collides with an existing large token is a low score, not a high one.

Be harsh. Most launches are worthless and should score under 0.3.

Reply with ONLY this JSON object, no prose, no markdown fence:
{{"narrative_fit": 0.0, "virality": 0.0, "originality": 0.0,
"community_signal": 0.0, "name_quality": 0.0,
"summary": "one sentence, max 200 chars, saying what the meme actually is and who shares it"}}"""


class GPTNarrativeScorer(BaseAgent):
    """Scores the meme's narrative strength. Holds no veto: a weak meme is not a rug."""

    name = "gpt"
    SCORE_KEYS = ("narrative_fit", "virality", "originality", "community_signal", "name_quality")

    def __init__(self, api_key: str, model: str = "gpt-4o") -> None:
        super().__init__(model)
        self.client = openai.AsyncOpenAI(api_key=api_key)

    async def aclose(self) -> None:
        """Release the SDK's HTTP connection pool."""
        await self.client.close()

    def _get_fallback(self) -> dict:
        """No narrative read means no narrative credit; all zeros, but no veto."""
        return {
            "narrative_fit": 0.0,
            "virality": 0.0,
            "originality": 0.0,
            "community_signal": 0.0,
            "name_quality": 0.0,
            "summary": "gpt unavailable; no narrative credit given",
        }

    def _build_prompt(self, token: Token, market_context: dict) -> str:
        """Render the narrative prompt, injecting the refreshed market context."""
        trending = market_context.get("trending_memes") or []
        return PROMPT.format(
            name=token.name or "(unnamed)",
            symbol=token.symbol or "?",
            address=token.address,
            description=(token.description or "(none provided)")[:400],
            twitter=token.twitter or "(none)",
            website=token.website or "(none)",
            telegram=token.telegram or "(none)",
            context_time=market_context.get("updated_at", "unknown"),
            sol_24h=market_context.get("sol_24h_pct", "unknown"),
            trending=", ".join(str(t) for t in trending) or "unknown",
            notes=market_context.get("notes", "none"),
        )

    async def score(self, token: Token, market_context: dict) -> dict:
        """Score the token's narrative. Never raises; returns the fallback on failure."""
        start = time.monotonic()
        prompt = self._build_prompt(token, market_context)

        async def call() -> openai.types.chat.ChatCompletion:
            return await self.client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0,
            )

        try:
            resp = await self._call_with_retry(call)
            text = resp.choices[0].message.content
        except (AgentError, IndexError, AttributeError) as exc:
            print(f"[gpt] call failed: {type(exc).__name__}: {exc}")
            out = self._fallback_with("api_error")
            out["latency_ms"] = self._elapsed_ms(start)
            return out

        parsed = self._parse_json(text or "")
        out = {key: self._clamp01(parsed.get(key)) for key in self.SCORE_KEYS}
        if parsed.get("error"):
            out["error"] = parsed["error"]
        out["summary"] = str(parsed.get("summary", ""))[:300]
        out["latency_ms"] = self._elapsed_ms(start)
        print(
            f"[gpt] {token.symbol or '?'} narrative={out['narrative_fit']:.2f} "
            f"virality={out['virality']:.2f} ({out['latency_ms']}ms)"
        )
        return out
