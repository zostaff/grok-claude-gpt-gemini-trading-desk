"""BaseAgent: JSON extraction, retry-with-backoff and latency tracking shared by all agents."""

from __future__ import annotations

import asyncio
import json
import random
import re
import time
from typing import Awaitable, Callable, TypeVar

T = TypeVar("T")

# Transient on the provider side: worth one or two more attempts.
RETRYABLE_STATUS = {408, 409, 429, 500, 502, 503, 504, 529}
# Model output routinely arrives wrapped in a markdown fence despite instructions.
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


class BaseAgent:
    """Common machinery for a single-provider LLM agent: call, retry, time, parse.

    Subclasses supply `name` and `_get_fallback()`, and implement one public method that
    builds a prompt, sends it through `_call_with_retry`, and parses the reply with
    `_parse_json`. Every return value carries `latency_ms` so the pipeline can log which
    model is the slow one.
    """

    name: str = "base"

    def __init__(self, model: str, max_retries: int = 2, base_delay: float = 1.0) -> None:
        self.model = model
        self.max_retries = max_retries
        self.base_delay = base_delay

    # --- subclass hooks -------------------------------------------------------

    def _get_fallback(self) -> dict:
        """Maximally pessimistic scores for this agent, used when the call or parse fails."""
        raise NotImplementedError

    # --- helpers --------------------------------------------------------------

    @staticmethod
    def _clamp01(value: object, default: float = 0.0) -> float:
        """Coerce a model-supplied number into [0.0, 1.0]."""
        try:
            return max(0.0, min(1.0, float(value)))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return default

    def _parse_json(self, text: str) -> dict:
        """Extract a JSON object from a model reply, falling back on anything unparseable.

        Handles the three shapes that actually occur: bare JSON, JSON inside a markdown
        fence, and JSON preceded or followed by prose. Returns `_get_fallback()` with an
        `error` key set when none of them yield an object.
        """
        if not text or not text.strip():
            return self._fallback_with("empty_response")

        candidate = _FENCE_RE.sub("", text.strip())
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass

        block = self._first_json_object(candidate)
        if block is not None:
            try:
                parsed = json.loads(block)
                if isinstance(parsed, dict):
                    return parsed
            except json.JSONDecodeError:
                pass

        print(f"[{self.name}] could not parse JSON from response: {text[:160]!r}")
        return self._fallback_with("parse_failed")

    @staticmethod
    def _first_json_object(text: str) -> str | None:
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

    def _fallback_with(self, reason: str) -> dict:
        """The agent's pessimistic dict, tagged with why we are using it."""
        fallback = dict(self._get_fallback())
        fallback["error"] = reason
        return fallback

    async def _call_with_retry(
        self,
        make_call: Callable[[], Awaitable[T]],
        max_retries: int | None = None,
        base_delay: float | None = None,
    ) -> T:
        """Run `make_call()` with exponential backoff on retryable provider errors.

        Takes a zero-argument factory rather than a coroutine object because a coroutine
        can only be awaited once, and a retry needs a fresh one.
        """
        retries = self.max_retries if max_retries is None else max_retries
        delay = self.base_delay if base_delay is None else base_delay
        last: BaseException | None = None

        for attempt in range(retries + 1):
            try:
                return await make_call()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                last = exc
                if attempt >= retries or not is_retryable(exc):
                    break
                # Jitter keeps four agents from re-firing in lockstep after a shared blip.
                wait = delay * (2**attempt) + random.uniform(0, 0.25)
                status = status_of(exc)
                print(
                    f"[{self.name}] {type(exc).__name__}"
                    f"{f' {status}' if status else ''}, retry {attempt + 1}/{retries} in {wait:.1f}s"
                )
                await asyncio.sleep(wait)

        assert last is not None
        raise AgentError(f"{self.name}: {type(last).__name__}: {last}") from last

    async def _timed(self, make_call: Callable[[], Awaitable[T]]) -> tuple[T, int]:
        """Run a call through the retry policy and return (result, latency_ms)."""
        start = time.monotonic()
        try:
            result = await self._call_with_retry(make_call)
        finally:
            self._last_latency_ms = int((time.monotonic() - start) * 1000)
        return result, self._last_latency_ms

    def _elapsed_ms(self, start: float) -> int:
        """Milliseconds since a `time.monotonic()` mark."""
        return int((time.monotonic() - start) * 1000)
