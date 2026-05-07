"""LLM client abstraction — Stage 3 of IMPLEMENTATION_PLAN.

Public surface:
    LLMClient      — abstract base class (provider-agnostic interface)
    LLMResponse    — normalised completion result
    LLMError       — base exception
    LLMTransportError, LLMValidationError — narrow error categories
    get_client()   — factory; the recommended way to construct a client

Provider implementations (AnthropicClient, OllamaClient) live in
sibling modules but are not re-exported. Call sites should go through
get_client() so the persona/provider binding stays in one place.
"""

from __future__ import annotations

from forge.llm.base import LLMClient, LLMResponse
from forge.llm.errors import LLMError, LLMTransportError, LLMValidationError
from forge.llm.factory import get_client

__all__ = [
    "LLMClient",
    "LLMError",
    "LLMResponse",
    "LLMTransportError",
    "LLMValidationError",
    "get_client",
]
