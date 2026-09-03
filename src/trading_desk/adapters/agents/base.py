"""LLMAgent: the template every scoring agent follows.

The base class owns four things so that no subclass can get them wrong:

* **latency** -- every report is timed, so `analysis` can name the slow seat;
* **aggregation** -- the quality score is the mean of `quality_keys` only, never the
  risk keys, so a danger signal can't be averaged away by a pretty picture;
* **veto application** -- the agent's own thresholds, not the orchestrator's knowledge
  of which agent is which;
* **the never-raises guarantee** -- a provider outage becomes a pessimistic report, so
  one dead API cannot take down a round.

A subclass implements `_score` (call the provider, return raw scores + a summary),
`_fallback_scores` (its worst case) and `_veto` (its kill switch).
"""

from __future__ import annotations

import abc
import asyncio
import json
import logging
import random
import re
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, TypeVar

from ...domain.evaluation import EvaluationContext
from ...domain.verdict import AgentReport

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Transient on the provider side: worth one or two more attempts.
RETRYABLE_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504, 529})
# Model output still arrives wrapped in a markdown fence often enough to strip for.
_FENCE_RE = re.compile(r"^\s*```(?:json|JSON)?\s*|\s*```\s*$")


class AgentError(RuntimeError):
    """Raised when an agent cannot reach its provider after exhausting retries."""


def status_of(exc: BaseException) -> int | None:
    """Best-effort HTTP status from any provider SDK's exception object."""
    for attr in ("status_code", "code", "http_status"):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            return value
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    return status if isinstance(status, int) else None


def is_retryable(exc: BaseException) -> bool:
    """True for rate limits, upstream 5xx and transport hiccups; False for 4xx auth errors."""
    status = status_of(exc)
    if status is not None:
        return status in RETRYABLE_STATUS
    name = type(exc).__name__.lower()
    return any(word in name for word in ("timeout", "connect", "readerror", "protocol", "remote"))


def clamp01(value: object, default: float = 0.0) -> float:
    """Coerce a model-supplied number into [0.0, 1.0]."""
    try:
        return max(0.0, min(1.0, float(value)))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def first_json_object(text: str) -> str | None:
    """Return the first brace-balanced object in `text`, ignoring braces inside strings."""
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_string = False
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def parse_json_object(text: str) -> dict | None:
    """Extract a JSON object from a model reply, or None if there isn't one.

    Handles the three shapes that actually occur: bare JSON, JSON inside a markdown
    fence, and JSON surrounded by prose.
    """
    if not text or not text.strip():
        return None
    candidate = _FENCE_RE.sub("", text.strip())
    for blob in (candidate, first_json_object(candidate)):
        if not blob:
            continue
        try:
            parsed = json.loads(blob)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


class LLMAgent(abc.ABC):
    """One seat on the panel, wrapping one provider."""

    #: Stable identifier used in logs, the journal and the conflict analysis.
    name: str = "agent"
    #: Scores where high is good. These and only these form the aggregate.
    quality_keys: tuple[str, ...] = ()
    #: Scores where high is BAD. Never aggregated; they drive `_veto` instead.
    risk_keys: tuple[str, ...] = ()
    #: Exceptions that mean "there was nothing here to judge", as opposed to "we failed
    #: to judge it". These produce a NEUTRAL report -- zeros, no veto -- because absence
    #: of evidence must not read as evidence of fraud. Everything else is pessimistic.
    neutral_exceptions: tuple[type[Exception], ...] = ()

    def __init__(self, model: str, *, max_retries: int = 2, base_delay: float = 1.0) -> None:
        self.model = model
        self.max_retries = max_retries
        self.base_delay = base_delay

    # --- subclass contract ----------------------------------------------------

    @abc.abstractmethod
    async def _score(self, context: EvaluationContext) -> tuple[dict[str, float], str]:
        """Call the provider and return (raw scores, one-line summary). May raise."""

    @abc.abstractmethod
    def _fallback_scores(self) -> dict[str, float]:
        """This agent's worst case, used when the call or the parse fails."""

    @abc.abstractmethod
    def _veto(self, scores: Mapping[str, float]) -> tuple[bool, str]:
        """Return (vetoed, reason) from this agent's own thresholds."""

    async def aclose(self) -> None:
        """Release the provider client.

        Deliberately concrete and empty: an agent with no pooled client has nothing to
        close, and forcing every subclass to write an empty override would be noise.
        """
        return None

    # --- template method ------------------------------------------------------

    async def evaluate(self, context: EvaluationContext) -> AgentReport:
        """Score the launch. Never raises: a dead provider becomes a pessimistic report."""
        started = time.monotonic()
        error: str | None = None
        summary = ""

        try:
            scores, summary = await self._score(context)
        except asyncio.CancelledError:
            raise
        except self.neutral_exceptions as exc:
            # Nothing to judge is not the same as failing to judge: score zero, no veto.
            logger.info("%s: nothing to evaluate (%s)", self.name, exc)
            scores = dict.fromkeys(self.all_keys, 0.0)
            summary = str(exc)
            error = "no_input"
        except Exception as exc:  # a provider outage must not kill the round
            logger.warning("%s failed: %s: %s", self.name, type(exc).__name__, exc)
            scores = self._fallback_scores()
            summary = f"{self.name} unavailable ({type(exc).__name__})"
            error = f"{type(exc).__name__}"

        normalised = {key: clamp01(scores.get(key)) for key in self.all_keys}
        vetoed, reason = self._veto(normalised)
        latency_ms = int((time.monotonic() - started) * 1000)

        report = AgentReport(
            agent=self.name,
            quality_score=self._aggregate(normalised),
            scores=normalised,
            summary=summary[:300],
            vetoed=vetoed,
            veto_reason=reason,
            latency_ms=latency_ms,
            error=error,
        )
        logger.info(
            "%s %s quality=%.2f%s (%dms)",
            self.name,
            context.token.symbol or "?",
            report.quality_score,
            " VETO" if vetoed else "",
            latency_ms,
        )
        return report

    # --- helpers --------------------------------------------------------------

    @property
    def all_keys(self) -> tuple[str, ...]:
        """Every score this agent produces, quality and risk together."""
        return self.quality_keys + self.risk_keys

    def _aggregate(self, scores: Mapping[str, float]) -> float:
        """Mean of the quality keys. Risk keys are excluded by construction."""
        if not self.quality_keys:
            return 0.0
        return sum(scores.get(k, 0.0) for k in self.quality_keys) / len(self.quality_keys)

    def _parse_scores(self, text: str) -> tuple[dict[str, float], str]:
        """Turn a model reply into (scores, summary), raising if it is unusable.

        Raising rather than returning a default is deliberate: `evaluate` converts the
        exception into the agent's pessimistic fallback, so an unreadable answer and an
        unreachable provider are handled identically -- we did not get an opinion.
        """
        parsed = parse_json_object(text)
        if parsed is None:
            raise AgentError(f"{self.name}: no JSON object in reply: {text[:160]!r}")
        scores = {key: clamp01(parsed.get(key)) for key in self.all_keys}
        return scores, str(parsed.get("summary", ""))

    async def _with_retry(self, make_call: Callable[[], Awaitable[T]]) -> T:
        """Run `make_call()` with exponential backoff on retryable provider errors.

        Takes a zero-argument factory rather than a coroutine, because a coroutine can
        only be awaited once and a retry needs a fresh one.
        """
        last: BaseException | None = None
        for attempt in range(self.max_retries + 1):
            try:
                return await make_call()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                last = exc
                if attempt >= self.max_retries or not is_retryable(exc):
                    break
                # Jitter keeps four agents from re-firing in lockstep after a shared blip.
                wait = self.base_delay * (2**attempt) + random.uniform(0, 0.25)
                logger.info(
                    "%s %s%s, retry %d/%d in %.1fs",
                    self.name, type(exc).__name__,
                    f" {status_of(exc)}" if status_of(exc) else "",
                    attempt + 1, self.max_retries, wait,
                )
                await asyncio.sleep(wait)
        assert last is not None
        raise AgentError(f"{self.name}: {type(last).__name__}: {last}") from last


def anthropic_text(message: Any) -> str:
    """Concatenate the text blocks of an Anthropic message, ignoring the other kinds.

    A response can carry thinking, tool-use and server-tool-result blocks alongside the
    text; reaching for `.text` on all of them is how this breaks the day a new block
    type ships.
    """
    parts: list[str] = []
    for block in getattr(message, "content", []) or []:
        if getattr(block, "type", None) == "text":
            text = getattr(block, "text", None)
            if isinstance(text, str):
                parts.append(text)
    return "".join(parts)


def extract_output_text(payload: Any) -> str:
    """Pull assistant text out of a provider reply without assuming one exact schema.

    Providers are converging on a Responses-style envelope but disagree on the details,
    and several serve both that and the older chat-completions shape on the same host.
    Rather than pin one JSON path and break on the next revision, walk the shapes we
    know and concatenate whatever text we find.
    """
    if payload is None:
        return ""
    if isinstance(payload, str):
        return payload

    # Responses API convenience field.
    text = getattr(payload, "output_text", None)
    if isinstance(text, str) and text.strip():
        return text
    if isinstance(payload, Mapping):
        direct = payload.get("output_text")
        if isinstance(direct, str) and direct.strip():
            return direct

    chunks: list[str] = []

    def walk(node: Any, depth: int = 0) -> None:
        """Collect `text` fields from message/content nodes, bounded against cycles."""
        if depth > 8:
            return
        if isinstance(node, Mapping):
            if node.get("type") in {"output_text", "text"} and isinstance(node.get("text"), str):
                chunks.append(node["text"])
                return
            content = node.get("content")
            if isinstance(content, str) and node.get("role") in {None, "assistant"}:
                chunks.append(content)
            for key in ("output", "content", "choices", "message", "delta"):
                if key in node:
                    walk(node[key], depth + 1)
        elif isinstance(node, list):
            for item in node:
                walk(item, depth + 1)

    walk(payload if isinstance(payload, (Mapping, list)) else {})
    return "\n".join(c for c in chunks if c).strip()
