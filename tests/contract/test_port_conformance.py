"""Contract tests: every adapter must satisfy the port it claims to implement.

These are the tests that make the ports real. Without them "implements the ScoringAgent
port" is a comment; with them, deleting a method or renaming it fails the build.
"""

from __future__ import annotations

import inspect

import pytest

from tests.conftest import make_settings
from trading_desk.adapters.agents import (
    AdversarialChecker,
    ClaudeWalletAuditor,
    GeminiImageAnalyst,
    GPTNarrativeScorer,
    GrokSocialSentinel,
    LLMAgent,
)
from trading_desk.adapters.execution import StubExecutor
from trading_desk.adapters.feed import PumpPortalFeed
from trading_desk.adapters.journal import JsonlJournal
from trading_desk.adapters.market import SolanaTrackerProvider
from trading_desk.app.composition import build_panel
from trading_desk.domain.clock import FrozenClock, SystemClock
from trading_desk.ports import (
    Adjudicator,
    Clock,
    DecisionJournal,
    MarketDataProvider,
    ScoringAgent,
    TokenFeed,
    TradeExecutor,
)

pytestmark = pytest.mark.contract

SCORING_AGENTS = (
    GrokSocialSentinel,
    ClaudeWalletAuditor,
    GPTNarrativeScorer,
    GeminiImageAnalyst,
)


def _instantiate(cls, settings):
    """Build an adapter with throwaway credentials; none of these touch the network."""
    if cls is GrokSocialSentinel:
        return cls("k", settings.models.grok, settings.vetoes)
    if cls is ClaudeWalletAuditor:
        return cls("k", settings.models.claude, settings.vetoes)
    if cls is GPTNarrativeScorer:
        return cls("k", settings.models.gpt)
    if cls is GeminiImageAnalyst:
        return cls("k", settings.models.gemini, settings.vetoes)
    raise AssertionError(f"no constructor recipe for {cls}")


@pytest.mark.parametrize("cls", SCORING_AGENTS, ids=lambda c: c.__name__)
def test_scoring_agents_satisfy_the_port(cls):
    agent = _instantiate(cls, make_settings())
    assert isinstance(agent, ScoringAgent)


@pytest.mark.parametrize("cls", SCORING_AGENTS, ids=lambda c: c.__name__)
def test_quality_and_risk_keys_are_disjoint(cls):
    """A key counted as both quality and danger would be averaged against itself."""
    assert not set(cls.quality_keys) & set(cls.risk_keys)


@pytest.mark.parametrize("cls", SCORING_AGENTS, ids=lambda c: c.__name__)
def test_fallback_covers_every_declared_key(cls):
    """A fallback missing a key would silently score it 0.0 and skip its veto check."""
    agent = _instantiate(cls, make_settings())
    assert set(agent._fallback_scores()) == set(agent.all_keys)


@pytest.mark.parametrize("cls", SCORING_AGENTS, ids=lambda c: c.__name__)
def test_evaluate_is_not_overridden_away(cls):
    """The never-raises guarantee lives in the base class; a subclass must not lose it.

    A subclass that reimplements `evaluate` silently opts out of latency timing, risk-key
    exclusion and the exception firewall. If one ever needs to, this test is the place to
    record why -- not a thing to discover from a dead pipeline.
    """
    assert cls.evaluate is LLMAgent.evaluate
    assert inspect.iscoroutinefunction(cls.evaluate)


@pytest.mark.parametrize("cls", SCORING_AGENTS, ids=lambda c: c.__name__)
def test_every_risk_key_can_be_vetoed_or_is_documented_as_advisory(cls):
    """Declaring a danger reading nobody acts on is a scoring bug worth catching."""
    agent = _instantiate(cls, make_settings())
    if not cls.risk_keys:
        return
    tripped = agent._veto(dict.fromkeys(agent.all_keys, 1.0))
    assert tripped[0] is True, f"{cls.__name__} declares risk keys but never vetoes"
    assert tripped[1], "a veto must say why"


def test_agents_have_unique_names():
    """The journal and the analysis key on `name`; a collision would merge two seats."""
    names = [a.name for a in build_panel(make_settings())]
    assert len(names) == len(set(names))


def test_adjudicator_satisfies_the_port():
    assert isinstance(AdversarialChecker("k", "claude-opus-5"), Adjudicator)


def test_executor_satisfies_the_port():
    assert isinstance(StubExecutor(make_settings()), TradeExecutor)


def test_journal_satisfies_the_port(tmp_path):
    journal = JsonlJournal(str(tmp_path / "a.jsonl"), str(tmp_path / "b.jsonl"))
    assert isinstance(journal, DecisionJournal)


def test_market_provider_satisfies_the_port():
    assert isinstance(SolanaTrackerProvider(make_settings()), MarketDataProvider)


def test_feed_satisfies_the_port():
    assert isinstance(PumpPortalFeed(make_settings(), data=None), TokenFeed)


@pytest.mark.parametrize("cls", [SystemClock, FrozenClock])
def test_clocks_satisfy_the_port(cls):
    assert isinstance(cls(), Clock)
