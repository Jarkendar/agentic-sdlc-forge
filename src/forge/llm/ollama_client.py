"""Ollama provider for forge.llm.

Speaks directly to Ollama's HTTP API (`POST /api/chat`) over httpx. We
deliberately avoid the `ollama` Python package — one less dependency,
the API is stable enough that a 30-line client is the right amount of
code, and `httpx` mocks (`respx`) are easier to test against than the
SDK's internal client.

Structured outputs use Ollama's `format` parameter with a JSON Schema
derived from the requested pydantic model. Per Ollama's docs, this
constrains the model's sampling to schema-valid JSON. The model still
needs to be told *what* to produce — schema constraints don't substitute
for a clear prompt — but the format we get back is guaranteed parseable.

Ollama is local: no auth, no API key, no network egress. Cost is always
$0 (per forge.pricing). Token counts come from `prompt_eval_count` and
`eval_count` in the response. Note: Ollama has a known quirk where
`prompt_eval_count` doesn't include the JSON schema itself when `format`
is set (ollama/ollama#11022) — we accept this; it's a small under-count
and it's their bug to fix, not ours to work around.
"""

from __future__ import annotations

import time
from typing import Any

import httpx
from pydantic import BaseModel, ValidationError

from forge.llm.base import LLMClient, LLMResponse
from forge.llm.errors import LLMTransportError, LLMValidationError
from forge.pricing import cost_for

#: Default Ollama server location — matches the docker-compose.yml that
#: ships in the repo. Per-call override via the constructor `base_url`.
DEFAULT_BASE_URL = "http://localhost:11434"

#: Total request timeout. Local inference is slower than a hosted API;
#: 120s covers a Plan-sized completion on modest hardware. Connect
#: timeout is shorter — if the server isn't responding to TCP within 5s
#: we don't gain anything by waiting for the full body timeout.
DEFAULT_TIMEOUT = httpx.Timeout(120.0, connect=5.0)


class OllamaClient(LLMClient):
    """Ollama provider implementation.

    Bound to one model on construction, like AnthropicClient — every
    persona that uses Ollama gets its own instance.
    """

    provider = "ollama"

    def __init__(
        self,
        *,
        model: str,
        base_url: str = DEFAULT_BASE_URL,
        timeout: httpx.Timeout = DEFAULT_TIMEOUT,
        http_client: httpx.Client | None = None,
    ) -> None:
        """Construct one persona-bound Ollama client.

        Args:
            model: Exact model tag (e.g. "qwen2.5-coder:7b"). Must exist
                in forge.pricing.PRICING (with $0/$0 prices for Ollama)
                otherwise cost_for() raises.
            base_url: Ollama server URL. Stripped of trailing slash to
                avoid double-slashing the /api/chat path.
            timeout: httpx Timeout. Override in tests where you don't
                want long waits on simulated failures.
            http_client: Pre-built httpx.Client. Tests inject a mocked
                client (`respx_mock` or a manually-stubbed transport) here.
        """
        self.model = model
        self.base_url = base_url.rstrip("/")
        self._owns_client = http_client is None
        self._client = http_client or httpx.Client(timeout=timeout)

    def close(self) -> None:
        """Release the underlying HTTP client if we created it.

        Used in long-lived process shutdown paths. Safe to call multiple
        times. Test fixtures that inject their own client are responsible
        for closing it themselves.
        """
        if self._owns_client:
            self._client.close()

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
            return self._call_once(system=system, user=user, schema=None)

        validation_errors: list[str] = []
        for attempt in (1, 2):
            attempt_user = user
            if attempt == 2 and validation_errors:
                attempt_user = (
                    f"{user}\n\n"
                    f"Your previous response failed validation with these errors:\n"
                    f"{validation_errors[-1]}\n\n"
                    f"Return a corrected response that satisfies the schema."
                )
            response = self._call_once(system=system, user=attempt_user, schema=schema)

            # `content` here is raw text; we have to parse it ourselves
            # because Ollama doesn't return a pre-validated object the way
            # Anthropic's `parse()` helper does.
            assert isinstance(response.content, str)
            try:
                instance = schema.model_validate_json(response.content)
            except ValidationError as ve:
                validation_errors.append(str(ve))
                if attempt == 2:
                    raise LLMValidationError(
                        f"Schema validation failed twice for {schema.__name__}. "
                        f"Last errors: {ve}",
                        attempts=2,
                    ) from ve
                continue

            # Replace text content with the parsed instance, keep all
            # the metadata (tokens, cost, duration) from the actual call.
            return LLMResponse(
                content=instance,
                tokens_in=response.tokens_in,
                tokens_out=response.tokens_out,
                cost_usd=response.cost_usd,
                duration_ms=response.duration_ms,
                model=response.model,
                provider=response.provider,
                finish_reason=response.finish_reason,
                retried_validation=(attempt == 2),
            )

        raise AssertionError("unreachable: validation loop exited without result")

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _call_once(
        self,
        *,
        system: str,
        user: str,
        schema: type[BaseModel] | None,
    ) -> LLMResponse:
        """Send one request to /api/chat and shape the response."""
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": False,
        }
        if schema is not None:
            # Ollama accepts a JSON Schema dict in `format` and
            # constrains sampling to match. We use pydantic's
            # model_json_schema() rather than building one by hand so the
            # schema definition stays in one place (the model class).
            payload["format"] = schema.model_json_schema()

        start = time.perf_counter()
        try:
            http_response = self._client.post(
                f"{self.base_url}/api/chat",
                json=payload,
            )
            http_response.raise_for_status()
        except httpx.HTTPError as e:
            raise LLMTransportError(f"Ollama call failed: {e}") from e
        duration_ms = int((time.perf_counter() - start) * 1000)

        try:
            data = http_response.json()
        except ValueError as e:
            raise LLMTransportError(f"Ollama returned non-JSON body: {e}") from e

        # Ollama's response shape: {"message": {"content": ...},
        # "prompt_eval_count": N, "eval_count": M, "done_reason": "stop"|...}
        message = data.get("message") or {}
        content_text = message.get("content", "") or ""
        tokens_in = int(data.get("prompt_eval_count", 0) or 0)
        tokens_out = int(data.get("eval_count", 0) or 0)
        finish_reason = str(data.get("done_reason", "") or "")
        cost = cost_for(self.provider, self.model, tokens_in, tokens_out)

        return LLMResponse(
            content=content_text,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=cost,
            duration_ms=duration_ms,
            model=self.model,
            provider=self.provider,
            finish_reason=finish_reason,
            retried_validation=False,
        )
