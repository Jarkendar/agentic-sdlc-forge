"""Exception types for the LLM layer.

Two categories, kept narrow on purpose:

- LLMTransportError: something below the model itself failed (network,
  auth, 5xx, rate limit that exhausted SDK retries). Actionable by
  retrying the run later, not by changing the prompt.
- LLMValidationError: the model returned, but its output didn't match
  the requested schema after one retry. Actionable by changing the
  schema or the prompt, not by retrying.

Splitting these lets the orchestrator (Stage 7) decide policy:
transport errors → escalate to human; validation errors → log loudly
because they signal prompt-or-schema drift.

We deliberately do NOT catch and re-raise vendor exceptions inside the
provider implementations as a generic "LLMError". The traceback chain
preserves the underlying cause via `from e`.
"""

from __future__ import annotations


class LLMError(Exception):
    """Base for all LLM-layer errors. Don't catch this in business code —
    catch the specific subclasses. Exists for `except LLMError` in
    top-level orchestrator/runner code that needs a single net."""


class LLMTransportError(LLMError):
    """Network, auth, or server-side failure that retries didn't recover.

    Examples: connection refused, invalid API key, persistent 5xx,
    rate-limit exhaustion. The model never produced a usable response.
    """


class LLMValidationError(LLMError):
    """Model produced output that failed schema validation after retry.

    The provider sent the model's first invalid output back as a
    correction prompt; the model's second attempt also failed. At this
    point retrying further is unlikely to help — the prompt or schema
    needs human attention.

    The original pydantic ValidationError is the `__cause__` of this
    exception; `str(err)` summarises both attempts.
    """

    def __init__(self, message: str, *, attempts: int) -> None:
        super().__init__(message)
        self.attempts = attempts
