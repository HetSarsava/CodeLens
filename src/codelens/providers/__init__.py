"""Thin, swappable LLM provider abstraction.

Agent code must never import a provider SDK directly — it should depend only
on the `Provider` protocol in `providers.base` and receive a concrete
implementation (e.g. `providers.groq_provider.GroqProvider`) injected in.
"""

from codelens.providers.base import LLMMessage, Provider, ProviderError

__all__ = ["LLMMessage", "Provider", "ProviderError"]
