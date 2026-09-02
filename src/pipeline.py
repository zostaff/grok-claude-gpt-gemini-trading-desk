"""MultiModelPipeline: the orchestrator that runs filter -> four LLMs -> consensus -> check."""

from __future__ import annotations

import asyncio
import contextlib
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

from .agents import (
    AdversarialChecker,
    ClaudeWalletAuditor,
    GeminiImageAnalyst,
    GPTNarrativeScorer,
    GrokSocialSentinel,
)
from .config import Settings, load_config
from .consensus import ConsensusEngine
from .data import DataFetcher, RiskData
from .filter import CodeFilter
from .executor import Executor
from .log import TradeLog
from .models import ConsensusResult, ModelVerdict, Token
from .risk import RiskManager

# Keys that are risk indicators, not quality scores: high means worse, so averaging them
# into an aggregate would make a dangerous token look good. They drive vetoes instead.
INVERTED_KEYS = frozenset(
    {
        "coordinated_shilling",
        "red_flag_visual",
        "dump_risk",
        "coordination_score",
        "wash_trading",
        "fresh_wallet_pct",
    }
)
NON_SCORE_KEYS = frozenset({"latency_ms"})

MARKET_CONTEXT_INTERVAL = 15 * 60
COINGECKO = "https://api.coingecko.com/api/v3"
# The third-party risk report is a hard gate before any tokens are spent on a launch.
MAX_RISK_SCORE = 7


class MultiModelPipeline:
    """Wires every component together and runs the decision loop over the live feed.

    The four scoring agents run concurrently on one token because they are independent
    and the slowest of them sets the round's latency; the adversarial checker runs after,
    because it needs all four outputs to look for contradictions between them.
    """

    def __init__(self, config_path: str | Path) -> None:
        self.config: Settings = load_config(config_path)

        self.data = DataFetcher(self.config)
        # The filter shares the fetcher so its metric poll and the pipeline's enrichment
        # are one cached request rather than two.
        self.filter = CodeFilter(self.config, self.data)
        self.consensus = ConsensusEngine(self.config.consensus)
        self.risk = RiskManager(self.config.risk)
        self.executor = Executor(self.config)
        self.log = TradeLog(self.config.log_path, self.config.conflict_log_path)

        self.grok = GrokSocialSentinel(self.config.grok_key, self.config.grok_model)
        self.claude = ClaudeWalletAuditor(self.config.claude_key, self.config.claude_model)
        self.gpt = GPTNarrativeScorer(self.config.openai_key, self.config.gpt_model)
        self.gemini = GeminiImageAnalyst(self.config.gemini_key, self.config.gemini_model)
        self.checker = AdversarialChecker(self.config.claude_key, self.config.claude_model)

        self.market_context: dict = {
            "updated_at": "not yet fetched",
            "sol_24h_pct": "unknown",
            "trending_memes": [],
            "notes": "market context has not been refreshed yet",
        }
        self._background: list[asyncio.Task] = []
        self.evaluated = 0

    # --- scoring helpers ------------------------------------------------------

    @staticmethod
    def _avg_score(agent_output: dict) -> float:
        """Average an agent's positive-direction scores, excluding risk flags and metadata."""
        values = [
            float(v)
            for k, v in agent_output.items()
            if isinstance(v, (int, float))
            and not isinstance(v, bool)
            and k not in INVERTED_KEYS
            and k not in NON_SCORE_KEYS
        ]
        if not values:
            return 0.0
        return sum(values) / len(values)

    def _build_verdicts(
        self, grok_out: dict, claude_out: dict, gpt_out: dict, gemini_out: dict
    ) -> list[ModelVerdict]:
        """Turn four raw agent dicts into verdicts, applying the configured veto thresholds."""
        v = self.config.vetoes
        specs = (
            ("grok", grok_out, grok_out.get("coordinated_shilling", 0.0) > v.max_coordinated_shilling,
             f"coordinated_shilling {grok_out.get('coordinated_shilling', 0.0):.2f} "
             f"> {v.max_coordinated_shilling}"),
            ("claude", claude_out,
             claude_out.get("dump_risk", 0.0) > v.max_dump_risk
             or claude_out.get("coordination_score", 0.0) > v.max_coordination_score,
             f"dump_risk {claude_out.get('dump_risk', 0.0):.2f} / "
             f"coordination {claude_out.get('coordination_score', 0.0):.2f}"),
            ("gpt", gpt_out, False, ""),
            ("gemini", gemini_out,
             gemini_out.get("red_flag_visual", 0.0) > v.max_red_flag_visual,
             f"red_flag_visual {gemini_out.get('red_flag_visual', 0.0):.2f} "
             f"> {v.max_red_flag_visual}"),
        )
        verdicts = []
        for model, raw, vetoed, veto_detail in specs:
            summary = str(raw.get("summary", ""))
            if vetoed:
                summary = f"VETO ({veto_detail}) {summary}".strip()
            verdicts.append(
                ModelVerdict(
                    model=model,
                    score=self._avg_score(raw),
                    summary=summary,
                    raw=raw,
                    hard_veto=bool(vetoed),
                    latency_ms=int(raw.get("latency_ms", 0)),
                )
            )
        return verdicts

    # --- market context -------------------------------------------------------

    async def _refresh_market_context(self) -> None:
        """Refresh SOL performance and trending narratives every 15 minutes for the scorer."""
        async with httpx.AsyncClient(base_url=COINGECKO, timeout=15) as client:
            while True:
                try:
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
                    context = dict(self.market_context)
                    context["updated_at"] = datetime.now(timezone.utc).isoformat(
                        timespec="seconds"
                    )
                    if not isinstance(price_r, BaseException) and price_r.status_code == 200:
                        sol = price_r.json().get("solana", {})
                        change = sol.get("usd_24h_change")
                        if change is not None:
                            context["sol_24h_pct"] = f"{float(change):+.2f}%"
                            context["sol_usd"] = sol.get("usd")
                    if not isinstance(trending_r, BaseException) and trending_r.status_code == 200:
                        coins = trending_r.json().get("coins", [])
                        context["trending_memes"] = [
                            str(c.get("item", {}).get("symbol", "")).upper()
                            for c in coins[:12]
                            if isinstance(c, dict)
                        ]
                    context["notes"] = "source: coingecko public API"
                    self.market_context = context
                    print(
                        f"[pipeline] market context: SOL {context['sol_24h_pct']} 24h, "
                        f"trending {', '.join(context['trending_memes'][:6]) or 'unknown'}"
                    )
                except asyncio.CancelledError:
                    raise
                except (httpx.HTTPError, ValueError, KeyError) as exc:
                    print(f"[pipeline] market context refresh failed: {type(exc).__name__}: {exc}")
                await asyncio.sleep(MARKET_CONTEXT_INTERVAL)

    # --- main loop ------------------------------------------------------------

    async def run(self) -> None:
        """Consume the filtered launch stream and evaluate each token until stopped."""
        mode = self.config.mode
        print(f"[pipeline] starting in {mode} mode")
        if mode == "live":
            print("[pipeline] WARNING: live mode selected, but the executor is a stub. "
                  "No transaction will actually be sent.")

        self._background = [
            asyncio.create_task(self._refresh_market_context(), name="market-context")
        ]
        try:
            async for token in self.filter.stream():
                allowed, reason = self.risk.can_trade()
                if not allowed:
                    print(f"[pipeline] halting: {reason}")
                    await self.log.log_skip(token, "risk_halt", reason)
                    break
                try:
                    await self._evaluate(token)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # one bad token must not kill the loop
                    print(f"[pipeline] error evaluating {token.address[:8]}: "
                          f"{type(exc).__name__}: {exc}")
                    await self.log.log_skip(token, "pipeline_error", f"{type(exc).__name__}: {exc}")
        except (KeyboardInterrupt, asyncio.CancelledError):
            print("[pipeline] interrupted")
        finally:
            await self.aclose()

    async def _evaluate(self, token: Token) -> None:
        """Run one token through data, the panel, consensus, the checker, and sizing."""
        self.evaluated += 1
        started = time.monotonic()
        print(
            f"[pipeline] evaluating #{self.evaluated} {token.symbol or '?'} "
            f"({token.address}) buyers={token.unique_buyers} vol={token.volume_sol:.2f} SOL"
        )

        # Step 2: enrich with trades, holders and the third-party risk report.
        token_data = await self.data.fetch(token)
        risk_data: RiskData = token_data.risk

        # Step 3: the cheap third-party gate, before spending four API calls.
        if risk_data.risk_score > MAX_RISK_SCORE:
            detail = risk_data.summary()
            print(f"[pipeline] skip high_risk_score: {detail}")
            await self.log.log_skip(token, "high_risk_score", detail,
                                    extra={"risk": risk_data.to_dict()})
            return

        # Step 4: the four agents, concurrently.
        risk_summary = risk_data.summary()
        grok_out, claude_out, gpt_out, gemini_out = await asyncio.gather(
            self.grok.scan(token),
            self.claude.audit(token, token_data.trades, token_data.holders, risk_summary),
            self.gpt.score(token, self.market_context),
            self.gemini.analyze(token),
        )

        # Steps 5 and 6: verdicts, then consensus.
        verdicts = self._build_verdicts(grok_out, claude_out, gpt_out, gemini_out)
        result = self.consensus.evaluate(verdicts)
        panel_ms = int((time.monotonic() - started) * 1000)

        # Step 7: anything but a buy is logged and dropped.
        if result.action != "buy":
            await self.log.log_skip(
                token,
                result.action if result.action == "conflict" else "consensus_skip",
                result.conflict_detail,
                result=result,
                extra={"panel_ms": panel_ms},
            )
            if result.action == "conflict" or result.bear_models:
                await self.log.log_conflict(token, result)
            return

        # Step 8: the adversarial cross-examination.
        checker_out = await self.checker.check(token, result, risk_summary)

        # Step 9: the checker holds an absolute veto.
        if not checker_out.get("approve"):
            await self.log.log_skip(
                token,
                "checker_veto",
                str(checker_out.get("veto_reason", "")),
                result=result,
                extra={"checker": checker_out, "panel_ms": panel_ms},
            )
            await self.log.log_conflict(token, result)
            return

        # Steps 10 and 11: adjusted confidence must still clear the floor.
        final_confidence = result.confidence + float(checker_out.get("confidence_adjustment", 0.0))
        if final_confidence < 0.4:
            await self.log.log_skip(
                token,
                "post_check_low_confidence",
                f"{result.confidence:.3f} adjusted to {final_confidence:.3f}",
                result=result,
                extra={"checker": checker_out, "panel_ms": panel_ms},
            )
            return

        # Step 12: size it.
        avg_score = result.avg_score
        amount_sol = self.risk.position_size(avg_score, final_confidence)
        if amount_sol <= 0:
            await self.log.log_skip(
                token, "no_size", "risk manager returned a zero position",
                result=result, extra={"checker": checker_out},
            )
            return

        # Step 13: buy, or say what we would have bought.
        if self.config.dry_run:
            print(
                f"[pipeline] DRY_RUN BUY {amount_sol:.4f} SOL of {token.symbol or '?'} "
                f"({token.address}) avg={avg_score:.3f} confidence={final_confidence:.3f} "
                f"bulls={','.join(result.bull_models)} | "
                f"missed_risk: {checker_out.get('missed_risk', '')}"
            )
            await self.log.log_entry(
                token, result, amount_sol, final_confidence, checker_out,
                self.risk.snapshot(), dry_run=True,
            )
            return

        tx = await self.executor.buy(token, amount_sol)
        self.risk.open_position(token.address, amount_sol)
        await self.log.log_entry(
            token, result, amount_sol, final_confidence, checker_out,
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
        risk = self.config.risk
        try:
            outcome = await self.executor.monitor_and_stop(
                token.address, risk.stop_loss_pct, risk.take_profit_pct, risk.max_hold_minutes
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"[pipeline] monitor failed for {token.address[:8]}: {type(exc).__name__}: {exc}")
            outcome = {"pnl_sol": 0.0, "exit_reason": f"monitor_error:{type(exc).__name__}"}
        pnl = float(outcome.get("pnl_sol", 0.0))
        hold = float(outcome.get("hold_seconds") or (time.monotonic() - started))
        self.risk.close_position(token.address, pnl)
        await self.log.log_exit(
            token.address, token.symbol, pnl, hold,
            str(outcome.get("exit_reason", "unknown")), tx=outcome,
        )

    # --- shutdown -------------------------------------------------------------

    async def aclose(self) -> None:
        """Cancel background tasks and close every HTTP client the pipeline owns."""
        for task in self._background:
            task.cancel()
        for task in self._background:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._background = []
        for closer in (
            self.filter.close(),
            self.data.aclose(),
            self.grok.aclose(),
            self.claude.aclose(),
            self.gpt.aclose(),
            self.gemini.aclose(),
            self.checker.aclose(),
        ):
            with contextlib.suppress(Exception):
                await closer
        print(f"[pipeline] stopped after evaluating {self.evaluated} tokens; "
              f"filter stats: {self.filter.stats}")


if __name__ == "__main__":
    from .__main__ import main

    raise SystemExit(main())
