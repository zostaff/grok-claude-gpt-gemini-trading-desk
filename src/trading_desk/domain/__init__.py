"""Pure domain: launches, reports, consensus and risk. No I/O, no provider SDKs.

Nothing in this package imports httpx, websockets or any model client. That constraint
is enforced by a test (`tests/unit/domain/test_domain_is_pure.py`) rather than by
convention, because it is the property that keeps the rules cheap to test.
"""

from __future__ import annotations

from .clock import FrozenClock, SystemClock
from .consensus import ConsensusEngine
from .evaluation import EvaluationContext, MarketContext, RiskReport
from .risk import RiskManager
from .token import Token, curve_pct_from_reserves
from .verdict import AdjudicationReport, AgentReport, ConsensusResult

__all__ = [
    "AdjudicationReport",
    "AgentReport",
    "ConsensusEngine",
    "ConsensusResult",
    "EvaluationContext",
    "FrozenClock",
    "MarketContext",
    "RiskManager",
    "RiskReport",
    "SystemClock",
    "Token",
    "curve_pct_from_reserves",
]
