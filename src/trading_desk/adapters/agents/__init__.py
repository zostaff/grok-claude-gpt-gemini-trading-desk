"""Provider adapters for the panel. Each implements the `ScoringAgent` port."""

from __future__ import annotations

from .adjudicator import AdversarialChecker
from .base import AgentError, LLMAgent
from .claude import ClaudeWalletAuditor
from .gemini import GeminiImageAnalyst, MissingArtwork
from .gpt import GPTNarrativeScorer
from .grok import GrokSocialSentinel

__all__ = [
    "AdversarialChecker",
    "AgentError",
    "ClaudeWalletAuditor",
    "GPTNarrativeScorer",
    "GeminiImageAnalyst",
    "GrokSocialSentinel",
    "LLMAgent",
    "MissingArtwork",
]
