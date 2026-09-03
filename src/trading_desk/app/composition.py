"""The composition root: the one place that knows which concrete class fills which port.

Every other module depends on `trading_desk.ports`. This file is where the abstractions
are traded for real objects, so swapping a provider, adding a seat to the panel, or
pointing the pipeline at a recorded feed for a replay test is a change here and nowhere
else.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

from ..adapters.agents import (
    AdversarialChecker,
    ClaudeWalletAuditor,
    GeminiImageAnalyst,
    GPTNarrativeScorer,
    GrokSocialSentinel,
)
from ..adapters.execution import StubExecutor
from ..adapters.feed import PumpPortalFeed
from ..adapters.journal import JsonlJournal
from ..adapters.market import SolanaTrackerProvider
from ..config.settings import Settings
from ..domain.clock import SystemClock
from ..domain.consensus import ConsensusEngine
from ..domain.risk import RiskManager
from ..ports import ScoringAgent
from .pipeline import TradingPipeline

logger = logging.getLogger(__name__)


def build_panel(settings: Settings) -> list[ScoringAgent]:
    """Construct the scoring seats.

    Each seat is here because it can see something the others cannot: Grok has live X
    access, Gemini can look at the artwork, Claude reads the wallet tables as forensics,
    GPT judges the idea. A seat that duplicates another's view is a cost, not a vote --
    which is what `trading_desk.analysis` exists to check.
    """
    creds, models = settings.credentials, settings.models
    return [
        GrokSocialSentinel(
            creds.grok, models.grok, settings.vetoes, base_url=settings.endpoints.xai
        ),
        ClaudeWalletAuditor(
            creds.anthropic, models.claude, settings.vetoes, effort=models.claude_effort
        ),
        GPTNarrativeScorer(creds.openai, models.gpt, effort=models.gpt_effort),
        GeminiImageAnalyst(
            creds.google, models.gemini, settings.vetoes,
            ipfs_gateway=settings.endpoints.ipfs_gateway,
        ),
    ]


def build_pipeline(
    settings: Settings, *, panel: Sequence[ScoringAgent] | None = None
) -> TradingPipeline:
    """Wire a complete pipeline from validated settings.

    `panel` is injectable so tests can run the real orchestrator against fake seats
    without touching a provider.
    """
    market_data = SolanaTrackerProvider(settings)
    # The feed shares the provider so its metric poll and the pipeline's enrichment are
    # one cached request rather than two.
    feed = PumpPortalFeed(settings, market_data)

    agents = list(panel) if panel is not None else build_panel(settings)
    logger.info("panel: %s", ", ".join(a.name for a in agents))

    return TradingPipeline(
        settings,
        feed=feed,
        market_data=market_data,
        agents=agents,
        adjudicator=AdversarialChecker(
            settings.credentials.anthropic,
            settings.models.claude,
            effort=settings.models.adjudicator_effort,
        ),
        consensus=ConsensusEngine(settings.consensus),
        risk=RiskManager(settings.risk, SystemClock()),
        executor=StubExecutor(settings),
        journal=JsonlJournal(
            settings.journal.decisions_path, settings.journal.disagreements_path
        ),
    )
