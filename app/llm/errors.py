"""Internal LLM errors used for failover decisions inside the router.

Caller-facing typed errors live in app.core.errors (LLMUnavailableError,
BudgetExceededError).
"""
from __future__ import annotations


class ProviderError(Exception):
    """Base for in-router failover signals."""


class ProviderUnavailable(ProviderError):
    """Provider is down / unreachable / misconfigured — fail over to next."""


class RateLimited(ProviderError):
    """Provider rate-limited the request — fail over to next after retry budget exhausted."""


class SchemaViolation(ProviderError):
    """Model returned content that doesn't satisfy the requested schema."""


class ContextOverflowError(ProviderError):
    """Request exceeds the provider's context window."""
