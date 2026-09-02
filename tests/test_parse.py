"""BaseAgent._parse_json(): fences, prose, garbage, and the pessimistic fallback."""

from __future__ import annotations

import asyncio

import pytest

from src.agents.base import AgentError, BaseAgent, is_retryable, status_of


class DummyAgent(BaseAgent):
    """Minimal concrete agent so the base-class machinery can be exercised directly."""

    name = "dummy"

    def _get_fallback(self) -> dict:
        return {"score": 0.0, "danger": 1.0, "summary": "fallback"}


@pytest.fixture
def agent() -> DummyAgent:
    return DummyAgent(model="test-model", base_delay=0.0)


def test_bare_json(agent: DummyAgent) -> None:
    assert agent._parse_json('{"a": 1, "b": 0.5}') == {"a": 1, "b": 0.5}


def test_markdown_fenced_json(agent: DummyAgent) -> None:
    text = '```json\n{"a": 1}\n```'
    assert agent._parse_json(text) == {"a": 1}


def test_unlabelled_fence(agent: DummyAgent) -> None:
    assert agent._parse_json('```\n{"a": 2}\n```') == {"a": 2}


def test_json_wrapped_in_prose(agent: DummyAgent) -> None:
    text = 'Sure! Here is my analysis:\n{"a": 3, "summary": "ok"}\nLet me know if you need more.'
    assert agent._parse_json(text) == {"a": 3, "summary": "ok"}


def test_nested_braces_are_balanced(agent: DummyAgent) -> None:
    text = 'noise {"outer": {"inner": {"deep": 1}}, "b": 2} trailing'
    assert agent._parse_json(text) == {"outer": {"inner": {"deep": 1}}, "b": 2}


def test_braces_inside_strings_do_not_break_balancing(agent: DummyAgent) -> None:
    text = '{"summary": "a } brace and a \\" quote", "a": 1}'
    parsed = agent._parse_json(text)
    assert parsed["a"] == 1
    assert parsed["summary"] == 'a } brace and a " quote'


def test_garbage_returns_tagged_fallback(agent: DummyAgent) -> None:
    parsed = agent._parse_json("I cannot help with that request.")
    assert parsed["danger"] == 1.0
    assert parsed["error"] == "parse_failed"


def test_empty_string_returns_tagged_fallback(agent: DummyAgent) -> None:
    assert agent._parse_json("")["error"] == "empty_response"
    assert agent._parse_json("   \n  ")["error"] == "empty_response"


def test_json_array_is_not_accepted_as_an_object(agent: DummyAgent) -> None:
    # A list has no named scores, so it is unusable and must fall back.
    assert agent._parse_json("[1, 2, 3]")["error"] == "parse_failed"


def test_truncated_json_falls_back(agent: DummyAgent) -> None:
    assert agent._parse_json('{"a": 1, "b":')["error"] == "parse_failed"


def test_fallback_is_a_copy_not_the_shared_dict(agent: DummyAgent) -> None:
    first = agent._parse_json("garbage")
    first["danger"] = 0.0
    assert agent._parse_json("more garbage")["danger"] == 1.0


def test_clamp01_bounds_and_rejects_nonsense(agent: DummyAgent) -> None:
    assert agent._clamp01(0.5) == 0.5
    assert agent._clamp01(1.7) == 1.0
    assert agent._clamp01(-3) == 0.0
    assert agent._clamp01("0.25") == 0.25
    assert agent._clamp01("high") == 0.0
    assert agent._clamp01(None) == 0.0


# --- retry policy ---------------------------------------------------------------


class FakeStatusError(Exception):
    """Stands in for a provider SDK's status-carrying exception."""

    def __init__(self, status_code: int) -> None:
        super().__init__(f"status {status_code}")
        self.status_code = status_code


def test_status_and_retryability() -> None:
    assert status_of(FakeStatusError(429)) == 429
    assert is_retryable(FakeStatusError(429)) is True
    assert is_retryable(FakeStatusError(503)) is True
    assert is_retryable(FakeStatusError(401)) is False
    assert is_retryable(FakeStatusError(400)) is False
    assert is_retryable(asyncio.TimeoutError()) is True


def test_retry_succeeds_on_the_second_attempt(agent: DummyAgent) -> None:
    calls = {"n": 0}

    async def flaky() -> str:
        calls["n"] += 1
        if calls["n"] == 1:
            raise FakeStatusError(429)
        return "ok"

    assert asyncio.run(agent._call_with_retry(flaky)) == "ok"
    assert calls["n"] == 2


def test_retry_gives_up_and_raises_agent_error(agent: DummyAgent) -> None:
    calls = {"n": 0}

    async def always_429() -> str:
        calls["n"] += 1
        raise FakeStatusError(429)

    with pytest.raises(AgentError):
        asyncio.run(agent._call_with_retry(always_429))
    assert calls["n"] == 3          # initial attempt plus max_retries=2


def test_non_retryable_error_fails_immediately(agent: DummyAgent) -> None:
    calls = {"n": 0}

    async def unauthorised() -> str:
        calls["n"] += 1
        raise FakeStatusError(401)

    with pytest.raises(AgentError):
        asyncio.run(agent._call_with_retry(unauthorised))
    assert calls["n"] == 1
