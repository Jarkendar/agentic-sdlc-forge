"""Factory for constructing the right LLM client for a given persona.

Call sites should always go through `get_client()` rather than importing
provider classes directly. This keeps the provider/persona binding in
one place — if we ever add a third provider or change the routing rules,
only this file needs to change.

The factory does not cache. One client per call is intentional: clients
are cheap to construct (the underlying httpx/anthropic clients are
constructed inside) and pinning instances at module level would make
test isolation harder. Caller can hold the returned instance themselves
if they want to reuse it.
"""

from __future__ import annotations

from forge.config import ForgeConfig, PersonaName
from forge.llm.anthropic_client import AnthropicClient
from forge.llm.base import LLMClient
from forge.llm.ollama_client import OllamaClient


def get_client(persona: PersonaName, config: ForgeConfig) -> LLMClient:
    """Build the LLMClient for `persona` per `config`'s model assignments.

    Args:
        persona: One of "orchestrator" | "planner" | "executor" |
            "verifier" | "reporter". The config schema enforces that
            every persona has an assignment, so this lookup never fails.
        config: A loaded ForgeConfig. Caller is responsible for having
            run `validate_credentials(config)` if any persona uses a
            paid provider — this factory does NOT validate keys, since
            it's reasonable to construct an Ollama-only client without
            an Anthropic key set.

    Returns:
        A provider-specific LLMClient bound to the persona's model.

    Raises:
        ValueError: If the persona's provider is unknown. Should never
            happen because config validation rejects unknown providers,
            but the explicit check makes the failure mode obvious if the
            invariant is ever violated.
    """
    assignment = config.models[persona]

    if assignment.provider == "anthropic":
        return AnthropicClient(model=assignment.model)

    if assignment.provider == "ollama":
        # Ollama's base_url is optional in the config; fall back to the
        # client default (which matches docker-compose.yml in the repo).
        kwargs: dict[str, str] = {"model": assignment.model}
        if assignment.base_url is not None:
            kwargs["base_url"] = assignment.base_url
        return OllamaClient(**kwargs)

    # Unreachable in practice — ProviderName is a Literal and config
    # validation rejects unknown providers. Kept as defence-in-depth
    # so a future refactor that loosens the type doesn't fail silently.
    raise ValueError(
        f"Unknown provider {assignment.provider!r} for persona {persona!r}. "
        f"Add a branch in forge.llm.factory.get_client()."
    )
