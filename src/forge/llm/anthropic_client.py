"""Anthropic provider for forge.llm.

Uses the official `anthropic` SDK. Two code paths:

- `schema=None` -> `client.messages.create()`, return raw text.
- `schema=SomeModel` -> `client.messages.parse(output_format=SomeModel)`,
  return the parsed pydantic instance.

The `messages.parse()` helper is the SDK's structured-output convenience
wrapper. It compiles the pydantic schema into Anthropic's
`output_config.format` (JSON Schema with grammar-constrained sampling) and
returns the response with `.parsed_output` already validated.

Why we still implement a validation retry even though Anthropic's
constrained sampling is in theory unfailing:
- `stop_reason == "max_tokens"` truncates output mid-JSON.
- `stop_reason == "refusal"` returns a refusal string that doesn't match
  the schema.
- The grammar can theoretically produce JSON that the SDK then fails to
  validate against the original (pre-transformation) pydantic schema —
  the SDK strips constraints like `minLength` from the sent schema and
  validates them client-side, so a model that ignores a description hint
  can still produce a value that fails pydantic validation.

We treat refusal/max_tokens as transport-level failures (no retry) and
genuine pydantic ValidationError as the case worth one retry.
"""

from __future__ import annotations

import os
import time
from typing import Any

from anthropic import (
    Anthropic,
    APIConnectionError,
    APIStatusError,
    AuthenticationError,
    RateLimitError,
)
from pydantic import BaseModel, ValidationError

from forge.llm.base import LLMClient, LLMResponse
from forge.llm.errors import LLMTransportError, LLMValidationError
from forge.pricing import cost_for

#: Hard cap on output tokens. Picked high enough that legitimate Plan/Report
#: outputs aren't truncated, low enough that a runaway loop can't drain
#: a budget on a single call. Each persona's prompt should aim well below.
DEFAULT_MAX_TOKENS = 4096


class AnthropicClient(LLMClient):
    """Anthropic provider implementation.

    Construction is bound to one model — every persona gets its own client
    instance, so we don't pass `model=` per call. Cost computation can
    therefore use `self.model` without ambiguity.
    """

    provider = "anthropic"

    def __init__(
        self,
        *,
        model: str,
        api_key: str | None = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        sdk_client: Anthropic | None = None,
    ) -> None:
        """Construct one persona-bound client.

        Args:
            model: Exact model string (e.g. "claude-haiku-4-5"). Must
                exist in forge.pricing.PRICING — otherwise cost_for()
                will raise on the first call.
            api_key: Override for ANTHROPIC_API_KEY. Normally None;
                set explicitly only in tests where we don't want to
                rely on env state.
            max_tokens: Per-call ceiling on output tokens.
            sdk_client: Pre-built Anthropic SDK client. Tests inject a
                stub here to avoid touching the network.
        """
        self.model = model
        self.max_tokens = max_tokens
        if sdk_client is not None:
            self._client = sdk_client
        else:
            # Anthropic() picks up ANTHROPIC_API_KEY from env if api_key
            # is None — same behaviour as our config.validate_credentials,
            # which has already run by the time the runtime gets here.
            self._client = Anthropic(api_key=api_key or os.environ.get("ANTHROPIC_API_KEY"))

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def complete(
        self,
        *,
        system: str,
        user: str,
        schema: type[BaseModel] | None = None,
    ) -> LLMResponse:
        if schema is None:
            return self._complete_text(system=system, user=user)
        return self._complete_structured(system=system, user=user, schema=schema)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _complete_text(self, *, system: str, user: str) -> LLMResponse:
        """Free-text completion — used by the Reporter only.

        Reporter produces markdown for humans, no schema involved.
        """
        start = time.perf_counter()
        try:
            response = self._client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
            )
        except (APIConnectionError, AuthenticationError, RateLimitError, APIStatusError) as e:
            raise LLMTransportError(f"Anthropic call failed: {e}") from e

        duration_ms = int((time.perf_counter() - start) * 1000)
        text = self._extract_text(response)
        return self._build_response(
            content=text,
            response=response,
            duration_ms=duration_ms,
            retried_validation=False,
        )

    def _complete_structured(
        self,
        *,
        system: str,
        user: str,
        schema: type[BaseModel],
    ) -> LLMResponse:
        """Structured completion using Anthropic's constrained sampling.

        On a pydantic ValidationError we retry exactly once, appending the
        validation error to the user message. Refusals and max_tokens cuts
        are NOT retried — they signal a different problem (safety / size).
        """
        validation_errors: list[str] = []

        for attempt in (1, 2):
            attempt_user = user
            if attempt == 2 and validation_errors:
                # Re-prompt with the validation feedback. We pass the prior
                # errors verbatim — pydantic errors are precise enough that
                # the model can usually self-correct.
                attempt_user = (
                    f"{user}\n\n"
                    f"Your previous response failed validation with these errors:\n"
                    f"{validation_errors[-1]}\n\n"
                    f"Return a corrected response that satisfies the schema."
                )

            start = time.perf_counter()
            try:
                response = self._client.messages.parse(
                    model=self.model,
                    max_tokens=self.max_tokens,
                    system=system,
                    messages=[{"role": "user", "content": attempt_user}],
                    output_format=schema,
                )
            except (
                APIConnectionError,
                AuthenticationError,
                RateLimitError,
                APIStatusError,
            ) as e:
                raise LLMTransportError(f"Anthropic call failed: {e}") from e
            duration_ms = int((time.perf_counter() - start) * 1000)

            # Refusal / truncation: not a validation problem. The model
            # decided not to comply (or ran out of room); retrying with
            # the same prompt won't help. Surface it as transport-level.
            stop_reason = getattr(response, "stop_reason", "") or ""
            if stop_reason in ("refusal", "max_tokens"):
                raise LLMTransportError(
                    f"Anthropic returned stop_reason={stop_reason!r} — "
                    f"response did not satisfy schema and retry would not help."
                )

            parsed = getattr(response, "parsed_output", None)

            # The SDK's `parsed_output` should be a validated instance when
            # constrained sampling worked. If it's None or wrong type, fall
            # through to manual validation against the raw text.
            if isinstance(parsed, schema):
                return self._build_response(
                    content=parsed,
                    response=response,
                    duration_ms=duration_ms,
                    retried_validation=(attempt == 2),
                )

            # Fallback: try to extract and validate the raw JSON ourselves.
            try:
                text = self._extract_text(response)
                instance = schema.model_validate_json(text)
            except ValidationError as ve:
                validation_errors.append(str(ve))
                if attempt == 2:
                    raise LLMValidationError(
                        f"Schema validation failed twice for {schema.__name__}. "
                        f"Last errors: {ve}",
                        attempts=2,
                    ) from ve
                continue

            return self._build_response(
                content=instance,
                response=response,
                duration_ms=duration_ms,
                retried_validation=(attempt == 2),
            )

        # Unreachable — the loop either returns or raises on each iteration.
        raise AssertionError("unreachable: structured completion loop exited without result")

    # ------------------------------------------------------------------
    # Response shaping
    # ------------------------------------------------------------------

    def _build_response(
        self,
        *,
        content: BaseModel | str,
        response: Any,
        duration_ms: int,
        retried_validation: bool,
    ) -> LLMResponse:
        """Construct LLMResponse from a successful SDK call."""
        usage = getattr(response, "usage", None)
        tokens_in = getattr(usage, "input_tokens", 0) if usage else 0
        tokens_out = getattr(usage, "output_tokens", 0) if usage else 0
        cost = cost_for(self.provider, self.model, tokens_in, tokens_out)
        finish_reason = getattr(response, "stop_reason", "") or ""

        return LLMResponse(
            content=content,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=cost,
            duration_ms=duration_ms,
            model=self.model,
            provider=self.provider,
            finish_reason=finish_reason,
            retried_validation=retried_validation,
        )

    @staticmethod
    def _extract_text(response: Any) -> str:
        """Pull the text from the first content block.

        Anthropic's content is a list of blocks (text, tool_use, ...).
        For our calls there is exactly one text block; we don't need
        the multi-block plumbing yet.
        """
        content = getattr(response, "content", None) or []
        for block in content:
            block_type = getattr(block, "type", None)
            if block_type == "text":
                return getattr(block, "text", "") or ""
        return ""
