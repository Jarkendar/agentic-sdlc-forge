"""Static pricing table and cost calculation for LLM API usage.

Per IMPLEMENTATION_PLAN §0.6.4, every event in the JSONL log carries a
`cost_usd` field. That field is computed here, inside `cost_for()`, called
from `LLMClient` (Stage 3) immediately after each completion.

Prices are stored per million tokens (per MTok), matching the unit Anthropic
publishes in their pricing docs. Vendors quote MTok rates; we keep them as
quoted to minimize transcription errors. The actual division by 1_000_000
happens once, inside `cost_for()`.

Local providers (Ollama) appear in the table with $0/$0 so the lookup path
stays uniform — no special-casing in the call site, and the DoD invariant
"every model in config.example.toml exists in the price table" stays
honest as we add new providers.

When adding a new model:
1. Add an entry to PRICING below with prices copied from the vendor's docs
2. Verify `tests/test_pricing.py` still passes
3. If the model is referenced in `.forge/config.example.toml`, the
   parity test will enforce it
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PriceEntry:
    """Per-MTok pricing for one model.

    Frozen so entries can't be mutated at runtime — the table is a constant.
    """

    input_per_mtok_usd: float
    output_per_mtok_usd: float


# ---------------------------------------------------------------------------
# Price table
# ---------------------------------------------------------------------------
#
# Anthropic prices verified 2026-05-04 against vendor docs and matched
# across multiple sources. Output is consistently 5x input across the
# current generation.
#
# Local Ollama models are zero-cost; listed explicitly so config validation
# can still verify "model exists in price table" without special-casing
# the provider.

PRICING: dict[str, dict[str, PriceEntry]] = {
    "anthropic": {
        "claude-haiku-4-5": PriceEntry(
            input_per_mtok_usd=1.00,
            output_per_mtok_usd=5.00,
        ),
        "claude-sonnet-4-6": PriceEntry(
            input_per_mtok_usd=3.00,
            output_per_mtok_usd=15.00,
        ),
        "claude-opus-4-6": PriceEntry(
            input_per_mtok_usd=5.00,
            output_per_mtok_usd=25.00,
        ),
        "claude-opus-4-7": PriceEntry(
            input_per_mtok_usd=5.00,
            output_per_mtok_usd=25.00,
        ),
    },
    "ollama": {
        # Local inference — zero variable cost. Kept in the table so
        # cost_for() doesn't need a provider special case.
        "qwen2.5-coder:7b": PriceEntry(0.0, 0.0),
        "gemma2:9b": PriceEntry(0.0, 0.0),
        "llama3.1:8b": PriceEntry(0.0, 0.0),
    },
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def cost_for(
    provider: str,
    model: str,
    tokens_in: int,
    tokens_out: int,
) -> float:
    """Compute cost in USD for one completion.

    Args:
        provider: Provider key, e.g. "anthropic" or "ollama".
        model: Model ID as used by the provider, e.g. "claude-sonnet-4-6".
        tokens_in: Input (prompt) tokens consumed.
        tokens_out: Output (completion) tokens generated.

    Returns:
        Cost in USD as a float. Always non-negative.

    Raises:
        ValueError: If (provider, model) is not in the price table, or
            if token counts are negative.
    """
    if tokens_in < 0 or tokens_out < 0:
        raise ValueError(
            f"Token counts must be non-negative; got tokens_in={tokens_in}, "
            f"tokens_out={tokens_out}"
        )

    entry = _lookup(provider, model)

    # Prices are per million tokens; divide once at the end.
    input_cost = entry.input_per_mtok_usd * tokens_in / 1_000_000
    output_cost = entry.output_per_mtok_usd * tokens_out / 1_000_000
    return input_cost + output_cost


def known_models() -> set[tuple[str, str]]:
    """All (provider, model) pairs in the price table.

    Used by config validation (Stage 1.2) to enforce: every model
    referenced in config.example.toml must have a price entry.
    """
    return {
        (provider, model)
        for provider, models in PRICING.items()
        for model in models
    }


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _lookup(provider: str, model: str) -> PriceEntry:
    """Look up a price entry, raising a helpful error on miss."""
    provider_table = PRICING.get(provider)
    if provider_table is None:
        known = sorted(PRICING.keys())
        raise ValueError(
            f"Unknown provider: {provider!r}. "
            f"Known providers: {known}. "
            f"Add it to forge/pricing.py PRICING table."
        )

    entry = provider_table.get(model)
    if entry is None:
        known = sorted(provider_table.keys())
        raise ValueError(
            f"Unknown model: {provider}/{model!r}. "
            f"Known models for {provider!r}: {known}. "
            f"Add it to forge/pricing.py PRICING table."
        )

    return entry
