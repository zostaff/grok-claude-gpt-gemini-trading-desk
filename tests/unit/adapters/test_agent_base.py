"""The agent template: parsing, retries, aggregation, and the never-raises guarantee."""

from __future__ import annotations

from collections.abc import Mapping

import pytest

from tests.conftest import make_token
from trading_desk.adapters.agents.base import (
    AgentError,
    LLMAgent,
    clamp01,
    is_retryable,
    parse_json_object,
    status_of,
)
from trading_desk.domain.evaluation import EvaluationContext


class SpyAgent(LLMAgent):
    """An agent whose provider call is scripted by the test."""

    name = "spy"
    quality_keys = ("good_a", "good_b")
    risk_keys = ("danger",)

    def __init__(self, reply: str | None = None, boom: Exception | None = None):
        super().__init__("test-model", max_retries=0, base_delay=0.0)
        self.reply, self.boom, self.calls = reply, boom, 0

    async def _score(self, context):
        self.calls += 1
        if self.boom:
            raise self.boom
        return self._parse_scores(self.reply or "")

    def _fallback_scores(self) -> dict[str, float]:
        return {"good_a": 0.0, "good_b": 0.0, "danger": 1.0}

    def _veto(self, scores: Mapping[str, float]) -> tuple[bool, str]:
        return (scores["danger"] > 0.7, "danger too high")


@pytest.fixture
def context() -> EvaluationContext:
    """A bare context; these tests never look at its contents."""
    return EvaluationContext(token=make_token())


# --- JSON extraction ---------------------------------------------------------

@pytest.mark.parametrize(
    "raw",
    [
        '{"a": 1}',
        '```json\n{"a": 1}\n```',
        '```\n{"a": 1}\n```',
        'Here you go:\n{"a": 1}\nHope that helps.',
    ],
    ids=["bare", "fenced", "unlabelled-fence", "prose-wrapped"],
)
def test_json_is_extracted_from_every_real_shape(raw):
    assert parse_json_object(raw) == {"a": 1}


def test_nested_braces_stay_balanced():
    assert parse_json_object('{"a": {"b": {"c": 1}}}') == {"a": {"b": {"c": 1}}}


def test_braces_inside_strings_do_not_break_balancing():
    assert parse_json_object('{"note": "a } brace"}') == {"note": "a } brace"}


@pytest.mark.parametrize(
    "raw", ["", "   ", "no json here", '[1, 2, 3]', '{"a": 1'],
    ids=["empty", "blank", "prose", "array", "truncated"],
)
def test_unusable_replies_return_none(raw):
    assert parse_json_object(raw) is None


def test_clamp01_bounds_and_rejects_nonsense():
    assert clamp01(0.5) == 0.5
    assert clamp01(9) == 1.0
    assert clamp01(-9) == 0.0
    assert clamp01("nope") == 0.0
    assert clamp01(None) == 0.0


# --- retry policy ------------------------------------------------------------

class FakeStatusError(Exception):
    """Stands in for a provider SDK exception carrying an HTTP status."""

    def __init__(self, status: int):
        super().__init__(f"HTTP {status}")
        self.status_code = status


@pytest.mark.parametrize(
    ("status", "retryable"),
    [(429, True), (500, True), (503, True), (529, True), (400, False), (401, False), (404, False)],
)
def test_retryability_follows_status(status, retryable):
    exc = FakeStatusError(status)
    assert status_of(exc) == status
    assert is_retryable(exc) is retryable


async def test_retry_succeeds_on_the_second_attempt():
    agent = SpyAgent()
    agent.max_retries = 2
    attempts = {"n": 0}

    async def flaky():
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise FakeStatusError(429)
        return "ok"

    assert await agent._with_retry(flaky) == "ok"
    assert attempts["n"] == 2


async def test_non_retryable_error_fails_immediately():
    agent = SpyAgent()
    agent.max_retries = 3
    attempts = {"n": 0}

    async def unauthorised():
        attempts["n"] += 1
        raise FakeStatusError(401)

    with pytest.raises(AgentError):
        await agent._with_retry(unauthorised)
    assert attempts["n"] == 1, "a 401 must not be retried"


# --- the template method -----------------------------------------------------

async def test_quality_score_excludes_risk_keys(context):
    """The core rule: a danger reading must not be averaged into the quality score."""
    agent = SpyAgent('{"good_a": 1.0, "good_b": 1.0, "danger": 1.0, "summary": "s"}')
    report = await agent.evaluate(context)
    assert report.quality_score == pytest.approx(1.0)
    assert report.scores["danger"] == 1.0
    assert report.vetoed is True


async def test_agent_never_raises_and_fails_pessimistically(context):
    agent = SpyAgent(boom=RuntimeError("provider on fire"))
    report = await agent.evaluate(context)
    assert report.error == "RuntimeError"
    assert report.degraded is True
    assert report.vetoed is True, "a blind agent must not read as a clean bill of health"


async def test_unparseable_reply_is_treated_like_an_outage(context):
    """Not getting an opinion and not understanding one are the same failure."""
    agent = SpyAgent("I refuse to answer in JSON")
    report = await agent.evaluate(context)
    assert report.degraded is True
    assert report.vetoed is True


async def test_neutral_exception_scores_zero_without_a_veto(context):
    """Absence of evidence is not evidence of fraud."""

    class Missing(Exception):
        pass

    agent = SpyAgent(boom=Missing("nothing to look at"))
    agent.neutral_exceptions = (Missing,)
    report = await agent.evaluate(context)
    assert report.quality_score == 0.0
    assert report.vetoed is False
    assert report.error == "no_input"


async def test_latency_is_always_recorded(context):
    agent = SpyAgent('{"good_a": 0.5, "good_b": 0.5, "danger": 0.0, "summary": "s"}')
    report = await agent.evaluate(context)
    assert report.latency_ms >= 0
    assert report.vetoed is False


async def test_a_nonsense_retry_budget_fails_loudly_rather_than_silently():
    """`assert` would vanish under `python -O`; the error must survive optimisation."""
    agent = SpyAgent()
    agent.max_retries = -1

    async def never_called():  # pragma: no cover - the loop must not reach it
        raise AssertionError("the retry loop should not have run")

    with pytest.raises(AgentError, match="without attempting a call"):
        await agent._with_retry(never_called)
