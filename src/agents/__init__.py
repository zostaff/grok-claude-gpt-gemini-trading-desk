"""LLM agents. Each wraps one provider and returns a normalised score dict."""

from __future__ import annotations

from .base import BaseAgent
from .checker import AdversarialChecker
from .claude import ClaudeWalletAuditor
from .gemini import GeminiImageAnalyst
from .gpt import GPTNarrativeScorer
from .grok import GrokSocialSentinel

__all__ = [
    "BaseAgent",
    "GrokSocialSentinel",
    "ClaudeWalletAuditor",
    "GPTNarrativeScorer",
    "GeminiImageAnalyst",
    "AdversarialChecker",
]
