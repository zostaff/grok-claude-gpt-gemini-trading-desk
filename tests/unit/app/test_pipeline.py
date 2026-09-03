"""The orchestrator, driven entirely through fake ports.

No provider, no socket, no file beyond tmp_path. If this file ever needs a network, the
ports have stopped doing their job.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

from tests.conftest import make_settings, make_token
from trading_desk.app.pipeline import TradingPipeline
from trading_desk.domain.clock import FrozenClock
from trading_desk.domain.consensus import ConsensusEngine
from trading_desk.domain.evaluation import EvaluationContext, RiskReport
from trading_desk.domain.risk import RiskManager
from trading_desk.domain.verdict import AdjudicationReport, AgentReport


class FakeAgent:
    """A scoring seat with a scripted answer."""

    def __init__(self, name: str, score: float, *, vetoed: bool = False):
        self.name, self.score, self.vetoed = name, score, vetoed
        self.quality_keys, self.risk_keys = ("q",), ()
        self.calls = 0

    async def evaluate(self, context: EvaluationContext) -> AgentReport:
        self.calls += 1
        return AgentReport(
            agent=self.name, quality_score=self.score, scores={"q": self.score},
            summary="fake", vetoed=self.vetoed,
            veto_reason="scripted veto" if self.vetoed else "",
        )

    async def aclose(self) -> None:
        return None


class FakeAdjudicator:
    """An adjudicator with a scripted ruling."""

    name = "adjudicator"

    def __init__(self, approved: bool = True, adjustment: float = 0.0):
        self.report = AdjudicationReport(
            approved=approved, confidence_adjustment=adjustment,
            veto_reason="" if approved else "scripted veto",
        )
        self.calls = 0

    async def review(self, context, result) -> AdjudicationReport:
        self.calls += 1
        return self.report

    async def aclose(self) -> None:
        return None


class FakeMarketData:
    """Returns a fixed enrichment context."""

    def __init__(self, risk_score: float = 1.0):
        self.risk_score = risk_score

    async def fetch(self, token) -> EvaluationContext:
        return EvaluationContext(token=token, risk=RiskReport(risk_score=self.risk_score))

    async def aclose(self) -> None:
        return None


class FakeFeed:
    """Yields a scripted list of launches, then ends."""

    def __init__(self, tokens=()):
        self.tokens = list(tokens)

    async def stream(self):
        for token in self.tokens:
            yield token

    async def aclose(self) -> None:
        return None


class RecordingJournal:
    """Captures what the pipeline decided, instead of writing files."""

    def __init__(self):
        self.entries: list[dict[str, Any]] = []
        self.skips: list[tuple[str, str]] = []
        self.disagreements: list[Any] = []

    async def record_entry(self, token, result, amount_sol, final_confidence,
                           adjudication, risk_state, dry_run, tx=None):
        self.entries.append(
            {"amount_sol": amount_sol, "confidence": final_confidence, "dry_run": dry_run}
        )

    async def record_skip(self, token, reason, detail="", result=None, extra=None):
        self.skips.append((reason, detail))

    async def record_exit(self, *a, **k):
        return None

    async def record_disagreement(self, token, result):
        self.disagreements.append(result)


class FakeExecutor:
    """Records calls; should never be reached in dry-run."""

    def __init__(self):
        self.buys: list[Any] = []

    async def buy(self, token, amount_sol) -> Mapping[str, Any]:
        self.buys.append((token.address, amount_sol))
        return {"ok": True}

    async def sell(self, token_address, pct):
        return {"ok": True}

    async def monitor_and_stop(self, token_address, stop_pct, take_profit_pct, max_hold_minutes):
        return {"pnl_sol": 0.0, "exit_reason": "test"}


def build(agents, *, adjudicator=None, risk_score=1.0, settings=None, feed_tokens=()):
    """Assemble a pipeline from fakes and return (pipeline, journal, executor)."""
    settings = settings or make_settings()
    journal, executor = RecordingJournal(), FakeExecutor()
    pipeline = TradingPipeline(
        settings,
        feed=FakeFeed(feed_tokens),
        market_data=FakeMarketData(risk_score),
        agents=agents,
        adjudicator=adjudicator or FakeAdjudicator(),
        consensus=ConsensusEngine(settings.consensus),
        risk=RiskManager(settings.risk, FrozenClock()),
        executor=executor,
        journal=journal,
    )
    return pipeline, journal, executor


BULLISH = lambda: [FakeAgent(n, 0.9) for n in ("grok", "claude", "gpt", "gemini")]  # noqa: E731


async def test_unanimous_buy_is_recorded_in_dry_run():
    pipeline, journal, executor = build(BULLISH())
    await pipeline.evaluate(make_token())

    assert len(journal.entries) == 1
    assert journal.entries[0]["dry_run"] is True
    assert journal.entries[0]["amount_sol"] > 0
    assert executor.buys == [], "dry-run must never reach the executor"


async def test_rug_score_gate_runs_before_any_model_call():
    """The cheap gate exists to save five API calls; prove it actually skips them."""
    agents = BULLISH()
    pipeline, journal, _ = build(agents, risk_score=9.0)
    await pipeline.evaluate(make_token())

    assert [a.calls for a in agents] == [0, 0, 0, 0]
    assert journal.skips[0][0] == "high_risk_score"
    assert journal.entries == []


async def test_one_veto_stops_a_unanimous_panel():
    agents = BULLISH()
    agents[1] = FakeAgent("claude", 0.9, vetoed=True)
    pipeline, journal, _ = build(agents)
    await pipeline.evaluate(make_token())

    assert journal.entries == []
    assert journal.skips[0][0] == "consensus_skip"
    assert "scripted veto" in journal.skips[0][1]


async def test_adjudicator_veto_overrides_the_panel():
    pipeline, journal, _ = build(BULLISH(), adjudicator=FakeAdjudicator(approved=False))
    await pipeline.evaluate(make_token())

    assert journal.entries == []
    assert journal.skips[0][0] == "adjudicator_veto"
    assert journal.disagreements, "a vetoed buy is a disagreement worth recording"


async def test_confidence_adjustment_can_sink_a_buy():
    """The adjudicator does not have to veto outright to stop a marginal trade."""
    agents = [FakeAgent(n, 0.85) for n in ("grok", "claude", "gpt", "gemini")]
    pipeline, journal, _ = build(
        agents, adjudicator=FakeAdjudicator(approved=True, adjustment=-0.5)
    )
    await pipeline.evaluate(make_token())

    assert journal.entries == []           # 0.85 - 0.50 = 0.35, under the 0.40 floor
    assert journal.skips[0][0] == "post_review_low_confidence"


async def test_confidence_exactly_at_the_floor_still_trades():
    """The floor is a minimum, not a margin: `< floor` skips, `== floor` proceeds.

    Pinned deliberately -- an off-by-one here silently changes how much the adjudicator
    is allowed to move before a trade dies, which is the opposite of a cosmetic bug.
    """
    pipeline, journal, _ = build(
        BULLISH(), adjudicator=FakeAdjudicator(approved=True, adjustment=-0.5)
    )
    await pipeline.evaluate(make_token())

    assert journal.entries and journal.entries[0]["confidence"] == pytest.approx(0.4)


async def test_exhausted_budget_skips_instead_of_sending_dust():
    pipeline, journal, _ = build(BULLISH())
    pipeline.risk.daily_pnl_sol = -0.49   # 30% of what's left is under the floor
    await pipeline.evaluate(make_token())

    assert journal.entries == []
    assert journal.skips[0][0] == "no_size"


async def test_conflict_is_logged_to_the_disagreement_journal():
    agents = [FakeAgent("grok", 0.95), FakeAgent("claude", 0.05),
              FakeAgent("gpt", 0.5), FakeAgent("gemini", 0.5)]
    pipeline, journal, _ = build(agents)
    await pipeline.evaluate(make_token())

    assert journal.skips[0][0] == "conflict"
    assert len(journal.disagreements) == 1


async def test_live_mode_reaches_the_executor():
    settings = make_settings(mode="live")
    pipeline, journal, executor = build(BULLISH(), settings=settings)
    await pipeline.evaluate(make_token())

    assert len(executor.buys) == 1
    assert journal.entries[0]["dry_run"] is False


async def test_adding_a_fifth_seat_needs_no_pipeline_change():
    """The point of the whole layout: the panel is data, not structure.

    This agent has a name the orchestrator has never heard of and a veto rule of its own.
    It participates fully without one line of `pipeline.py` knowing it exists.
    """
    agents = [*BULLISH(), FakeAgent("liquidity-sentinel", 0.9, vetoed=True)]
    pipeline, journal, _ = build(agents)
    await pipeline.evaluate(make_token())

    assert journal.entries == []
    assert "liquidity-sentinel" in journal.skips[0][1]


async def test_every_seat_is_asked_exactly_once_per_token():
    agents = BULLISH()
    pipeline, _, _ = build(agents)
    await pipeline.evaluate(make_token())
    assert all(a.calls == 1 for a in agents)


@pytest.mark.parametrize("failing_seat", range(4))
async def test_a_dead_seat_does_not_kill_the_round(failing_seat):
    """A degraded agent reports pessimistically; it must not raise through the panel."""
    agents = BULLISH()
    agents[failing_seat] = FakeAgent(agents[failing_seat].name, 0.0, vetoed=True)
    pipeline, journal, _ = build(agents)

    await pipeline.evaluate(make_token())   # must not raise
    assert journal.skips, "the round completed and recorded a decision"


# --- the run loop ------------------------------------------------------------

async def test_running_out_of_slots_skips_the_launch_and_keeps_consuming():
    """The bug this replaced: `break` on a transient condition ended the run for good.

    Capacity clears the moment a monitor task closes a position, so the loop must skip
    the launch in front of it and carry on. It never bit in dry-run, because dry-run
    opens no positions -- it would only have shown up with real money on the line.
    """
    tokens = [make_token(symbol=f"T{i}") for i in range(3)]
    pipeline, journal, _ = build(BULLISH(), feed_tokens=tokens)
    for i in range(3):
        pipeline.risk.open_position(f"held{i}", 0.01)

    await pipeline.run()

    assert [r for r, _ in journal.skips] == ["at_capacity"] * 3
    assert pipeline.evaluated == 0, "no model calls were spent while full"


async def test_a_freed_slot_resumes_evaluation_mid_run():
    tokens = [make_token(symbol=f"T{i}") for i in range(2)]
    pipeline, journal, _ = build(BULLISH(), feed_tokens=tokens)
    for i in range(3):
        pipeline.risk.open_position(f"held{i}", 0.01)

    # A monitor task closes a position between the two launches.
    original = pipeline.risk.check
    calls = {"n": 0}

    def check_then_free():
        calls["n"] += 1
        if calls["n"] == 2:
            pipeline.risk.close_position("held0", 0.0)
        return original()

    pipeline.risk.check = check_then_free  # type: ignore[method-assign]
    await pipeline.run()

    reasons = [r for r, _ in journal.skips]
    assert reasons[0] == "at_capacity"
    assert pipeline.evaluated == 1, "the second launch was evaluated once a slot freed"


async def test_the_daily_loss_limit_ends_the_run():
    """A spent budget does not clear until tomorrow; consuming the feed is pointless."""
    tokens = [make_token(symbol=f"T{i}") for i in range(3)]
    pipeline, journal, _ = build(BULLISH(), feed_tokens=tokens)
    pipeline.risk.daily_pnl_sol = -0.5

    await pipeline.run()

    assert [r for r, _ in journal.skips] == ["risk_halt"]
    assert pipeline.evaluated == 0


async def test_the_daily_trade_cap_ends_the_run():
    tokens = [make_token(symbol=f"T{i}") for i in range(3)]
    pipeline, journal, _ = build(BULLISH(), feed_tokens=tokens)
    pipeline.risk.trades_today = 10

    await pipeline.run()

    assert [r for r, _ in journal.skips] == ["risk_halt"]


async def test_a_raising_evaluation_does_not_kill_the_loop():
    """One malformed launch must cost that launch, not the session."""
    tokens = [make_token(symbol=f"T{i}") for i in range(3)]
    pipeline, journal, _ = build(BULLISH(), feed_tokens=tokens)

    original = pipeline.evaluate
    calls = {"n": 0}

    async def sometimes_explode(token):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("bad token")
        return await original(token)

    pipeline.evaluate = sometimes_explode  # type: ignore[method-assign]
    await pipeline.run()

    assert calls["n"] == 3, "all three launches were attempted"
    assert ("pipeline_error", "RuntimeError: bad token") in journal.skips


async def test_the_feed_is_closed_when_the_run_ends():
    pipeline, _, _ = build(BULLISH(), feed_tokens=[])
    closed = {"feed": False}
    pipeline.feed.aclose = lambda: _mark(closed)  # type: ignore[method-assign]

    await pipeline.run()

    assert closed["feed"] is True


async def _mark(flag: dict) -> None:
    """Helper coroutine that records that aclose was awaited."""
    flag["feed"] = True
