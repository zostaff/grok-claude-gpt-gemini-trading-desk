"""The orchestrator: feed -> enrich -> panel -> consensus -> adjudicator -> sizing.

Everything it touches arrives through a port, so this file contains decision *sequence*
and nothing else. It does not know which provider scores social chatter, which agent may
veto and on what threshold, or whether the executor is real -- those are properties of
the objects it was handed.

The practical test of that: adding a sixth seat to the panel is a one-line change in
`composition.py` and no change at all here.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Sequence
from datetime import UTC

import httpx

from ..config.settings import Settings
from ..domain.consensus import ConsensusEngine
from ..domain.evaluation import EvaluationContext, MarketContext
from ..domain.risk import RiskManager, TradingStatus
from ..domain.token import Token
from ..domain.verdict import AdjudicationReport, AgentReport, ConsensusResult
from ..ports import (
    Adjudicator,
    DecisionJournal,
    MarketDataProvider,
    ScoringAgent,
    TokenFeed,
    TradeExecutor,
)

logger = logging.getLogger(__name__)

MARKET_CONTEXT_INTERVAL = 15 * 60


class TradingPipeline:
    """Runs the decision loop over a feed until it is stopped or the brakes engage."""

    def __init__(
        self,
        settings: Settings,
        *,
        feed: TokenFeed,
        market_data: MarketDataProvider,
        agents: Sequence[ScoringAgent],
        adjudicator: Adjudicator,
        consensus: ConsensusEngine,
        risk: RiskManager,
        executor: TradeExecutor,
        journal: DecisionJournal,
    ) -> None:
        self.settings = settings
        self.feed = feed
        self.market_data = market_data
        self.agents = list(agents)
        self.adjudicator = adjudicator
        self.consensus = consensus
        self.risk = risk
        self.executor = executor
        self.journal = journal

        self.market = MarketContext()
        self._background: list[asyncio.Task] = []
        self.evaluated = 0

    # --- main loop ------------------------------------------------------------

    async def run(self) -> None:
        """Consume the filtered launch stream and evaluate each token until stopped."""
        logger.info("starting in %s mode with %d panel seats", self.settings.mode,
                    len(self.agents))
        if self.settings.mode == "live":
            logger.warning(
                "live mode selected, but the executor is a stub: no transaction will be sent"
            )

        self._background = [
            asyncio.create_task(self._refresh_market_context(), name="market-context")
        ]
        try:
            async for token in self.feed.stream():
                check = self.risk.check()
                if check.status is TradingStatus.HALTED:
                    logger.warning("halting: %s", check.reason)
                    await self.journal.record_skip(token, "risk_halt", check.reason)
                    break
                if check.status is TradingStatus.PAUSED:
                    # Transient: a monitor task closing a position frees a slot. Skip
                    # this launch and keep consuming, rather than ending the run.
                    logger.info("skipping %s: %s", token.symbol or "?", check.reason)
                    await self.journal.record_skip(token, "at_capacity", check.reason)
                    continue
                try:
                    await self.evaluate(token)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # one bad token must not kill the loop
                    logger.exception("error evaluating %s", token.address[:8])
                    await self.journal.record_skip(
                        token, "pipeline_error", f"{type(exc).__name__}: {exc}"
                    )
        except (KeyboardInterrupt, asyncio.CancelledError):
            logger.info("interrupted")
        finally:
            await self.aclose()

    async def evaluate(self, token: Token) -> None:
        """Run one token through enrichment, the panel, consensus, review and sizing."""
        self.evaluated += 1
        started = time.monotonic()
        logger.info(
            "evaluating #%d %s (%s) buyers=%d vol=%.2f SOL",
            self.evaluated, token.symbol or "?", token.address,
            token.unique_buyers, token.volume_sol,
        )

        context = await self.market_data.fetch(token)
        context.market = self.market

        # The cheap third-party gate, before spending five model calls on a known rug.
        if context.risk.risk_score > self.settings.vetoes.max_rug_score:
            detail = context.risk_summary
            logger.info("skip high_risk_score: %s", detail)
            await self.journal.record_skip(
                token, "high_risk_score", detail, extra={"risk": context.risk.to_dict()}
            )
            return

        reports = await self._run_panel(context)
        result = self.consensus.evaluate(reports)
        panel_ms = int((time.monotonic() - started) * 1000)

        if result.action != "buy":
            await self.journal.record_skip(
                token,
                result.action if result.action == "conflict" else "consensus_skip",
                result.conflict_detail,
                result=result,
                extra={"panel_ms": panel_ms},
            )
            if result.action == "conflict" or result.bear_agents:
                await self.journal.record_disagreement(token, result)
            return

        adjudication = await self.adjudicator.review(context, result)

        if not adjudication.approved:
            await self.journal.record_skip(
                token, "adjudicator_veto", adjudication.veto_reason,
                result=result,
                extra={"adjudication": adjudication.to_dict(), "panel_ms": panel_ms},
            )
            await self.journal.record_disagreement(token, result)
            return

        final_confidence = result.confidence + adjudication.confidence_adjustment
        if final_confidence < self.settings.vetoes.min_final_confidence:
            await self.journal.record_skip(
                token, "post_review_low_confidence",
                f"{result.confidence:.3f} adjusted to {final_confidence:.3f}",
                result=result,
                extra={"adjudication": adjudication.to_dict(), "panel_ms": panel_ms},
            )
            return

        amount_sol = self.risk.position_size(result.avg_score, final_confidence)
        if amount_sol <= 0:
            await self.journal.record_skip(
                token, "no_size", "risk manager returned a zero position",
                result=result, extra={"adjudication": adjudication.to_dict()},
            )
            return

        await self._enter(token, result, adjudication, amount_sol, final_confidence)

    # --- steps ----------------------------------------------------------------

    async def _run_panel(self, context: EvaluationContext) -> list[AgentReport]:
        """Ask every seat at once. The slowest agent sets the round's latency.

        `evaluate` on a ScoringAgent never raises, so `gather` needs no exception
        handling here -- a dead provider arrives as a pessimistic report, which is
        exactly what the consensus engine should see.
        """
        return list(await asyncio.gather(*(agent.evaluate(context) for agent in self.agents)))

    async def _enter(
        self,
        token: Token,
        result: ConsensusResult,
        adjudication: AdjudicationReport,
        amount_sol: float,
        final_confidence: float,
    ) -> None:
        """Buy, or record what would have been bought."""
        if self.settings.dry_run:
            logger.info(
                "DRY_RUN BUY %.4f SOL of %s (%s) avg=%.3f confidence=%.3f bulls=%s | missed: %s",
                amount_sol, token.symbol or "?", token.address, result.avg_score,
                final_confidence, ",".join(result.bull_agents), adjudication.missed_risk,
            )
            await self.journal.record_entry(
                token, result, amount_sol, final_confidence, adjudication,
                self.risk.snapshot(), dry_run=True,
            )
            return

        tx = await self.executor.buy(token, amount_sol)
        self.risk.open_position(token.address, amount_sol)
        await self.journal.record_entry(
            token, result, amount_sol, final_confidence, adjudication,
            self.risk.snapshot(), dry_run=False, tx=tx,
        )
        task = asyncio.create_task(
            self._monitor(token, amount_sol), name=f"monitor-{token.address[:8]}"
        )
        self._background.append(task)
        task.add_done_callback(
            lambda t: self._background.remove(t) if t in self._background else None
        )

    async def _monitor(self, token: Token, amount_sol: float) -> None:
        """Watch an open position to its exit and book the result against the daily PnL."""
        started = time.monotonic()
        risk = self.settings.risk
        try:
            outcome = await self.executor.monitor_and_stop(
                token.address, risk.stop_loss_pct, risk.take_profit_pct, risk.max_hold_minutes
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("monitor failed for %s: %s", token.address[:8], type(exc).__name__)
            outcome = {"pnl_sol": 0.0, "exit_reason": f"monitor_error:{type(exc).__name__}"}
        pnl = float(outcome.get("pnl_sol", 0.0))
        hold = float(outcome.get("hold_seconds") or (time.monotonic() - started))
        self.risk.close_position(token.address, pnl)
        await self.journal.record_exit(
            token.address, token.symbol, pnl, hold,
            str(outcome.get("exit_reason", "unknown")), tx=outcome,
        )

    # --- market context -------------------------------------------------------

    async def _refresh_market_context(self) -> None:
        """Refresh SOL performance and trending narratives on a timer for the scorer."""
        base = self.settings.endpoints.coingecko
        async with httpx.AsyncClient(base_url=base, timeout=15) as client:
            while True:
                try:
                    self.market = await self._fetch_market_context(client)
                    logger.info(
                        "market context: SOL %s 24h, trending %s",
                        self.market.sol_24h_pct,
                        ", ".join(self.market.trending_memes[:6]) or "unknown",
                    )
                except asyncio.CancelledError:
                    raise
                except (httpx.HTTPError, ValueError, KeyError) as exc:
                    logger.warning(
                        "market context refresh failed: %s: %s", type(exc).__name__, exc
                    )
                await asyncio.sleep(MARKET_CONTEXT_INTERVAL)

    async def _fetch_market_context(self, client: httpx.AsyncClient) -> MarketContext:
        """One CoinGecko round trip; partial failure keeps whatever did arrive."""
        from datetime import datetime

        price_r, trending_r = await asyncio.gather(
            client.get(
                "/simple/price",
                params={
                    "ids": "solana",
                    "vs_currencies": "usd",
                    "include_24hr_change": "true",
                },
            ),
            client.get("/search/trending"),
            return_exceptions=True,
        )
        updated = datetime.now(UTC).isoformat(timespec="seconds")
        sol_pct: str = self.market.sol_24h_pct
        sol_usd: float | None = self.market.sol_usd
        trending: tuple[str, ...] = ()

        if not isinstance(price_r, BaseException) and price_r.status_code == 200:
            sol = price_r.json().get("solana", {})
            change = sol.get("usd_24h_change")
            if change is not None:
                sol_pct = f"{float(change):+.2f}%"
                sol_usd = sol.get("usd")
        if not isinstance(trending_r, BaseException) and trending_r.status_code == 200:
            coins = trending_r.json().get("coins", [])
            trending = tuple(
                str(c.get("item", {}).get("symbol", "")).upper()
                for c in coins[:12]
                if isinstance(c, dict)
            )

        return MarketContext(
            updated_at=updated,
            sol_24h_pct=sol_pct,
            sol_usd=sol_usd,
            trending_memes=trending or self.market.trending_memes,
            notes="source: coingecko public API",
        )

    # --- shutdown -------------------------------------------------------------

    async def aclose(self) -> None:
        """Cancel background tasks and close every client this pipeline owns."""
        for task in self._background:
            task.cancel()
        for task in self._background:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._background = []

        closers = [self.feed.aclose(), self.market_data.aclose(), self.adjudicator.aclose()]
        closers += [agent.aclose() for agent in self.agents]
        for closer in closers:
            with contextlib.suppress(Exception):
                await closer
        logger.info("stopped after evaluating %d tokens", self.evaluated)
