"""The fifth call: cross-examines a panel that has already voted to buy.

This seat exists because consensus among language models is much weaker evidence than it
looks. Four models agreeing is not four independent confirmations -- they share training
data and they share blind spots. The adjudicator is given the other four raw outputs and
asked the one question none of them were asked: do these four accounts contradict each
other, and does anything here look like a scam none of them named?

It is the only seat with an absolute veto, and it reasons at a higher effort than the
panel, because finding what four models missed is strictly harder than scoring a launch.
"""

from __future__ import annotations

import logging
import time

import anthropic

from ...config.settings import Effort
from ...domain.evaluation import EvaluationContext
from ...domain.verdict import AdjudicationReport, ConsensusResult
from .base import AgentError, anthropic_text, parse_json_object

logger = logging.getLogger(__name__)

PROMPT = """Four independent models just voted to BUY this pump.fun launch. Your job is to
talk them out of it if they are wrong. You are the last check before real money moves.

TOKEN
name: {name} (${symbol})
contract: {address}
creator: {creator}
age: {age:.1f} minutes | curve: {curve:.1f}% | buyers: {buyers} | volume: {volume:.2f} SOL
description: {description}

THIRD-PARTY RISK REPORT
{risk_summary}

WHAT THE PANEL SAID
{verdicts}

PANEL AGGREGATE
average quality score: {avg:.3f} | agreement: {agreement:.2f} | spread: {spread:.2f}
bulls: {bulls} | bears: {bears}

Look for the things a scoring rubric cannot catch:
1. CONTRADICTIONS between the four accounts. If the social model reports organic
   excitement while the wallet auditor reports wallets funded from one source, both
   cannot be true. Name the contradiction.
2. AGREEMENT FOR THE WRONG REASON. If all four scored high on evidence that is really
   the same single signal seen four ways, that is one confirmation, not four.
3. A DEGRADED SEAT. A model that failed and returned its fallback did not actually vote.
   Say so if the buy depends on a seat that was blind.
4. ANYTHING THE RUBRIC HAS NO COLUMN FOR: impersonation, a known rug pattern, a launch
   timed to a larger scam, a creator address with history.

Be adversarial. The panel's job was to find reasons to buy; yours is to find the reason
not to. Approving is the exception, not the default. If nothing is actually wrong, say so
plainly and approve -- do not invent a concern to look diligent.

confidence_adjustment is added to the panel's confidence: negative to reduce it, positive
only when you found genuine corroboration the panel undersold. Keep it within -0.5..+0.2.

Reply with ONLY this JSON object, no prose, no markdown fence:
{{"approve": true, "confidence_adjustment": 0.0,
"veto_reason": "empty string if approving, else the specific reason",
"missed_risk": "the strongest risk the panel underweighted, even if you approve",
"reasoning": "one or two sentences, max 300 chars"}}"""


class AdversarialChecker:
    """Reviews a buy decision and holds an absolute veto over it."""

    name = "adjudicator"

    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        effort: Effort = "xhigh",
        max_tokens: int = 4096,
        max_retries: int = 2,
    ) -> None:
        self.model = model
        self.effort = effort
        self.max_tokens = max_tokens
        self.max_retries = max_retries
        self.client = anthropic.AsyncAnthropic(api_key=api_key, max_retries=max_retries)

    async def aclose(self) -> None:
        """Release the provider client."""
        await self.client.close()

    @staticmethod
    def _format_reports(result: ConsensusResult) -> str:
        """Render every seat's score, summary and health for side-by-side comparison."""
        lines = []
        for report in result.reports:
            health = f" [DEGRADED: {report.error}]" if report.degraded else ""
            risk_bits = ", ".join(
                f"{k}={report.scores[k]:.2f}" for k in sorted(report.scores) if k in report.scores
            )
            lines.append(
                f"- {report.agent}: quality={report.quality_score:.2f}{health}\n"
                f"    scores: {risk_bits}\n"
                f"    said: {report.summary or '(no summary)'}"
            )
        return "\n".join(lines) or "(no reports)"

    def _build_prompt(self, context: EvaluationContext, result: ConsensusResult) -> str:
        """Render the cross-examination prompt."""
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
            description=(token.description or "(none)")[:400],
            risk_summary=context.risk_summary,
            verdicts=self._format_reports(result),
            avg=result.avg_score,
            agreement=result.agreement_ratio,
            spread=result.spread,
            bulls=", ".join(result.bull_agents) or "none",
            bears=", ".join(result.bear_agents) or "none",
        )

    async def review(
        self, context: EvaluationContext, result: ConsensusResult
    ) -> AdjudicationReport:
        """Approve or veto the panel's buy. Never raises.

        A failure here is a veto, not an approval. The whole point of this seat is to be
        the thing standing between a confident panel and a bad trade; if it could not
        run, that thing was not there.
        """
        started = time.monotonic()
        prompt = self._build_prompt(context, result)

        try:
            message = await self.client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                thinking={"type": "adaptive"},
                output_config={"effort": self.effort},
                messages=[{"role": "user", "content": prompt}],
            )
            if getattr(message, "stop_reason", None) == "refusal":
                raise AgentError("provider declined the request")
            text = anthropic_text(message)
            parsed = parse_json_object(text)
            if parsed is None:
                raise AgentError(f"no JSON object in reply: {text[:160]!r}")
        except Exception as exc:
            logger.warning("adjudicator failed: %s: %s", type(exc).__name__, exc)
            return AdjudicationReport(
                approved=False,
                veto_reason=f"adjudicator unavailable ({type(exc).__name__}); failing closed",
                latency_ms=int((time.monotonic() - started) * 1000),
                error=type(exc).__name__,
            )

        adjustment = parsed.get("confidence_adjustment", 0.0)
        try:
            adjustment = max(-0.5, min(0.2, float(adjustment)))
        except (TypeError, ValueError):
            adjustment = 0.0

        report = AdjudicationReport(
            approved=bool(parsed.get("approve")),
            confidence_adjustment=adjustment,
            veto_reason=str(parsed.get("veto_reason", ""))[:400],
            missed_risk=str(parsed.get("missed_risk", ""))[:400],
            reasoning=str(parsed.get("reasoning", ""))[:400],
            latency_ms=int((time.monotonic() - started) * 1000),
        )
        logger.info(
            "adjudicator %s adj=%+.2f (%dms)",
            "APPROVED" if report.approved else "VETOED",
            report.confidence_adjustment, report.latency_ms,
        )
        return report
