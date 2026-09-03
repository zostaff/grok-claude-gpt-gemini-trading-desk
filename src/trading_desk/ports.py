"""Ports: the contracts every component is written against.

This module is the seam of the architecture. It contains no I/O, no provider SDKs and
no business rules -- only the protocols that the domain and the orchestrator depend on,
so that an adapter can be swapped (a real executor for the stub, a recorded feed for the
live socket) without any file outside `adapters/` changing.

Every protocol is `@runtime_checkable` so the contract tests in `tests/contract/` can
assert that each adapter actually satisfies the port it claims.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, Protocol, runtime_checkable

from .domain.evaluation import EvaluationContext
from .domain.token import Token
from .domain.verdict import AdjudicationReport, AgentReport, ConsensusResult

__all__ = [
    "Adjudicator",
    "Clock",
    "DecisionJournal",
    "MarketDataProvider",
    "ScoringAgent",
    "TokenFeed",
    "TradeExecutor",
]


@runtime_checkable
class TokenFeed(Protocol):
    """A source of launches that have already cleared the cheap pre-LLM gate.

    Implementations own their own transport and their own gate. The orchestrator only
    ever sees tokens worth spending model calls on, and never learns how they arrived.
    """

    def stream(self) -> AsyncIterator[Token]:
        """Yield qualifying tokens until the feed is closed."""
        ...

    async def aclose(self) -> None:
        """Release sockets, tasks and buffers."""
        ...


@runtime_checkable
class MarketDataProvider(Protocol):
    """Enrichment for a single token: trade tape, holder table and a third-party risk report."""

    async def fetch(self, token: Token) -> EvaluationContext:
        """Return everything the panel needs to judge `token`."""
        ...

    async def aclose(self) -> None:
        """Release the HTTP connection pools."""
        ...


@runtime_checkable
class ScoringAgent(Protocol):
    """One model's opinion on one launch.

    Every agent takes the same `EvaluationContext` and picks the fields it cares about.
    That uniformity is what lets the orchestrator run the panel as a plain list: adding a
    sixth agent is a registration, not an edit to the pipeline.

    An agent declares its own contract rather than having the orchestrator know it:

    `quality_keys`   scores where high is good; these form the aggregate.
    `risk_keys`      scores where high is BAD; excluded from the aggregate entirely,
                     because averaging them in would let one good score cancel a warning.
    `evaluate`       must never raise -- a provider outage is reported as a pessimistic
                     `AgentReport`, never as an exception that kills the round.
    """

    name: str

    # Declared read-only. A plain `quality_keys: tuple[str, ...]` would be an invariant
    # mutable attribute, so an agent whose concrete tuple has a known length -- which is
    # every agent -- would fail to satisfy the protocol.
    @property
    def quality_keys(self) -> tuple[str, ...]:
        """Scores where high is good; these and only these form the aggregate."""
        ...

    @property
    def risk_keys(self) -> tuple[str, ...]:
        """Scores where high is BAD; excluded from the aggregate, they drive the veto."""
        ...

    async def evaluate(self, context: EvaluationContext) -> AgentReport:
        """Score the launch. Never raises."""
        ...

    async def aclose(self) -> None:
        """Release the provider client."""
        ...


@runtime_checkable
class Adjudicator(Protocol):
    """The fifth call: cross-examines a panel that already voted to buy."""

    name: str

    async def review(
        self, context: EvaluationContext, result: ConsensusResult
    ) -> AdjudicationReport:
        """Approve or veto the panel's buy. Never raises."""
        ...

    async def aclose(self) -> None:
        """Release the provider client."""
        ...


@runtime_checkable
class TradeExecutor(Protocol):
    """Chain-side effects. The shipped implementation is a stub that does nothing."""

    async def buy(self, token: Token, amount_sol: float) -> dict[str, Any]:
        """Enter a position. Returns a receipt."""
        ...

    async def sell(self, token_address: str, pct: float) -> dict[str, Any]:
        """Exit `pct` percent of a position. Returns a receipt."""
        ...

    async def monitor_and_stop(
        self, token_address: str, stop_pct: float, take_profit_pct: float, max_hold_minutes: float
    ) -> dict[str, Any]:
        """Hold until an exit trigger fires; return the realised outcome."""
        ...


@runtime_checkable
class DecisionJournal(Protocol):
    """Append-only record of every decision, including the ones that declined to trade."""

    async def record_entry(
        self,
        token: Token,
        result: ConsensusResult,
        amount_sol: float,
        final_confidence: float,
        adjudication: AdjudicationReport,
        risk_state: dict[str, Any],
        dry_run: bool,
        tx: dict[str, Any] | None = None,
    ) -> None:
        """Record a buy, real or simulated."""
        ...

    async def record_skip(
        self,
        token: Token,
        reason: str,
        detail: str = "",
        result: ConsensusResult | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        """Record a decline, with the machine-readable reason code."""
        ...

    async def record_exit(
        self,
        address: str,
        symbol: str,
        pnl_sol: float,
        hold_seconds: float,
        exit_reason: str,
        tx: dict[str, Any] | None = None,
    ) -> None:
        """Record a position close."""
        ...

    async def record_disagreement(self, token: Token, result: ConsensusResult) -> None:
        """Record every model's score and reasoning when the panel split."""
        ...


@runtime_checkable
class Clock(Protocol):
    """Injectable time, so the daily brakes can be tested without waiting for midnight."""

    def today(self) -> Any:
        """Current calendar date."""
        ...

    def monotonic(self) -> float:
        """Monotonic seconds, for measuring durations."""
        ...
