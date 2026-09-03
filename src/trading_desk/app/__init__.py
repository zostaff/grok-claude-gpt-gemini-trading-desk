"""Application layer: the orchestrator and the composition root."""

from __future__ import annotations

from .composition import build_panel, build_pipeline
from .pipeline import TradingPipeline

__all__ = ["TradingPipeline", "build_panel", "build_pipeline"]
