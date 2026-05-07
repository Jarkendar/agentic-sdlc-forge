"""Provider-agnostic LLM client interface.

Per IMPLEMENTATION_PLAN Stage 3, every persona talks to its model through
this interface. The point is *not* to support arbitrary providers — it's
to keep the call site identical whether we're hitting a paid API or a
local Ollama box.

Two providers exist today (Anthropic, Ollama). Adding a third without a
concrete need is yak-shaving (§3.3). The abstraction earns its keep when
the same agent code can run against either provider with no changes.

Design points worth knowing:
- `complete()` is sync. Stage 3+ runs sequentially (decision §0.6.3) and
  the rest of the runtime is sync; an async layer would be dead weight.
- The schema parameter is `type[BaseModel] | None`. When given, the
  provider must return an instance of that schema in `LLMResponse.content`.
  When None, `content` is raw text. The union forces call sites to assert
  what they asked for.
- Cost is computed inside the client, not by the caller. This keeps the
  pricing table (forge.pricing) referenced in exactly one place per
  provider, and keeps the EventLog ingestion path uniform.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from pydantic import BaseModel


@dataclass(frozen=True)
class LLMResponse:
    """One completion result, normalised across providers.

    Frozen so EventLog payloads can't mutate this after the fact — the
    cost number on a logged event must match the cost number that was
    computed at the moment the call returned.

    Fields:
        content: Parsed pydantic instance if the caller passed `schema=`,
            otherwise raw text. Using a union forces every call site to
            check what it actually got — bugs where someone forgets to
            pass a schema and treats `content` as a model surface here
            instead of mid-pipeline.
        tokens_in / tokens_out: From the provider's usage block. Both
            providers (Anthropic, Ollama) return these natively.
        cost_usd: Pre-computed via forge.pricing.cost_for(). Ollama
            returns 0.0 — see pricing.PRICING for the rationale.
        duration_ms: Wall-clock duration of the network call. Measured
            in the client, not derived from any provider field.
        model: The exact model string the request hit. Echoed back so
            callers can verify nothing surprising was substituted by a
            gateway/proxy.
        provider: "anthropic" | "ollama". Used by the EventLog and the
            Reporter for per-provider aggregations.
        finish_reason: Raw stop_reason from the provider, kept verbatim
            for debugging. Anthropic uses "end_turn", "max_tokens",
            "tool_use", "stop_sequence"; Ollama uses "stop", "length".
            Don't normalise — when something goes wrong we want the
            original string, not our interpretation of it.
        retried_validation: True if the schema validation required a
            second round-trip. Lets the EventLog flag prompts that
            consistently need retries (signal for prompt-tuning).
    """

    content: BaseModel | str
    tokens_in: int
    tokens_out: int
    cost_usd: float
    duration_ms: int
    model: str
    provider: str
    finish_reason: str
    retried_validation: bool = False


class LLMClient(ABC):
    """Abstract interface every provider must satisfy.

    Subclasses live in forge.llm.anthropic_client and forge.llm.ollama_client.
    Use `forge.llm.get_client(persona, config)` to construct one — call
    sites should not import provider classes directly.
    """

    #: The provider name this client handles. Subclasses set this; the
    #: factory uses it for routing. Kept as a class attribute so it's
    #: visible without instantiation.
    provider: str

    @abstractmethod
    def complete(
        self,
        *,
        system: str,
        user: str,
        schema: type[BaseModel] | None = None,
    ) -> LLMResponse:
        """Run one completion.

        Args:
            system: System prompt. Required — every persona has one.
            user: User message. The actual request.
            schema: If given, the response is validated against this
                pydantic model. The provider implementation is responsible
                for: (a) telling the model what shape to produce, and
                (b) retrying once on validation failure with the validation
                error appended to the prompt (per plan §3.1).

        Returns:
            LLMResponse with `content` typed per the rules above.

        Raises:
            LLMValidationError: schema was given and the model failed to
                produce a valid instance after one retry.
            LLMTransportError: network/auth/server-side failure that the
                provider's own retry budget couldn't recover from.
        """
        ...
