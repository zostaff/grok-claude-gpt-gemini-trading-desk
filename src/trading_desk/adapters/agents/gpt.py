"""GPT, the narrative scorer: judges whether the idea itself has anywhere to go.

This is the one seat that never vetoes. A weak meme is a reason not to buy, not evidence
of fraud, and conflating the two would let taste veto a trade the forensics cleared.

It is also the only agent that gets a schema-guaranteed reply: the Responses API's
structured outputs make the shape a server-side guarantee rather than something we parse
out of prose and hope for.
"""

from __future__ import annotations

from collections.abc import Mapping

from openai import AsyncOpenAI
from pydantic import BaseModel, Field

from ...config.settings import Effort
from ...domain.evaluation import EvaluationContext
from .base import LLMAgent

PROMPT = """You are a memecoin narrative analyst. Judge whether THIS token's idea has a
reason to spread, independently of its chart.

TOKEN
name: {name}
symbol: {symbol}
description: {description}
socials: twitter={twitter} website={website} telegram={telegram}
metadata completeness: {metadata_score:.2f}
age: {age:.1f} minutes | curve: {curve:.1f}% | buyers: {buyers}

CURRENT MARKET CONTEXT
{market}

Score each 0.0 to 1.0:
- narrative_fit: does this ride a narrative that is live RIGHT NOW, per the context above?
- virality: is the joke legible in two seconds to someone who has never seen it?
- originality: fresh idea, or the four-hundredth dog coin this week?
- community_signal: do the socials suggest a real community rather than a placeholder?
- name_quality: is the name and ticker memorable, pronounceable and searchable?

Be harsh. Most launches are derivative and should score below 0.4."""


class NarrativeScores(BaseModel):
    """Schema the model is constrained to return."""

    narrative_fit: float = Field(ge=0.0, le=1.0)
    virality: float = Field(ge=0.0, le=1.0)
    originality: float = Field(ge=0.0, le=1.0)
    community_signal: float = Field(ge=0.0, le=1.0)
    name_quality: float = Field(ge=0.0, le=1.0)
    summary: str = Field(max_length=300)


class GPTNarrativeScorer(LLMAgent):
    """Grades the idea. Never vetoes: a weak meme is not a rug."""

    name = "gpt"
    quality_keys = (
        "narrative_fit",
        "virality",
        "originality",
        "community_signal",
        "name_quality",
    )
    risk_keys = ()

    def __init__(self, api_key: str, model: str, *, effort: Effort = "high") -> None:
        super().__init__(model)
        self.effort = effort
        self.client = AsyncOpenAI(api_key=api_key)

    async def aclose(self) -> None:
        """Release the HTTP connection pool."""
        await self.client.close()

    def _fallback_scores(self) -> dict[str, float]:
        """A failed narrative read scores zero: no opinion is not a good opinion."""
        return dict.fromkeys(self.quality_keys, 0.0)

    def _veto(self, scores: Mapping[str, float]) -> tuple[bool, str]:
        """This seat holds no veto, by design."""
        return False, ""

    def _build_prompt(self, context: EvaluationContext) -> str:
        """Render the narrative prompt for one token."""
        token = context.token
        return PROMPT.format(
            name=token.name or "(unnamed)",
            symbol=token.symbol or "?",
            description=(token.description or "(none provided)")[:600],
            twitter=token.twitter or "(none)",
            website=token.website or "(none)",
            telegram=token.telegram or "(none)",
            metadata_score=token.metadata_score,
            age=token.age_minutes,
            curve=token.bonding_curve_pct,
            buyers=token.unique_buyers,
            market=context.market.as_prompt_block(),
        )

    async def _score(self, context: EvaluationContext) -> tuple[dict[str, float], str]:
        """Ask GPT to grade the idea, with the reply shape guaranteed by the schema."""
        prompt = self._build_prompt(context)

        async def call() -> NarrativeScores | None:
            """One request; `parse` validates the reply against NarrativeScores."""
            response = await self.client.responses.parse(
                model=self.model,
                input=[{"role": "user", "content": prompt}],
                reasoning={"effort": self.effort},
                text_format=NarrativeScores,
            )
            return response.output_parsed

        parsed = await self._with_retry(call)
        if parsed is None:
            # Structured outputs make this near-impossible, but a None here would
            # otherwise sail through as all-zeros without the degraded flag set.
            raise ValueError(f"{self.name}: structured output came back empty")
        return parsed.model_dump(exclude={"summary"}), parsed.summary
