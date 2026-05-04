"""Tests for the static pricing table and cost calculation.

Two things matter here:
1. The math is right (per-MTok division done once, no off-by-1000 bugs).
2. Misuse is loud (unknown provider/model raises with a helpful message,
   not a silent zero-cost result).

The "every model in config.example.toml exists in PRICING" invariant
lives in tests/test_config.py once config.py exists — it can't be tested
here without the config loader.
"""

from __future__ import annotations

import pytest
from forge.pricing import PRICING, PriceEntry, cost_for, known_models

# ---------- cost_for: math ----------


def test_cost_for_zero_tokens_is_zero() -> None:
    assert cost_for("anthropic", "claude-sonnet-4-6", 0, 0) == 0.0


def test_cost_for_input_only() -> None:
    # Sonnet 4.6: $3.00 per MTok input. 1M tokens -> $3.00.
    assert cost_for("anthropic", "claude-sonnet-4-6", 1_000_000, 0) == pytest.approx(3.00)


def test_cost_for_output_only() -> None:
    # Sonnet 4.6: $15.00 per MTok output. 1M tokens -> $15.00.
    assert cost_for("anthropic", "claude-sonnet-4-6", 0, 1_000_000) == pytest.approx(15.00)


def test_cost_for_mixed_tokens() -> None:
    # Opus 4.7 at $5/$25 per MTok. 100K input + 50K output:
    #   input  = 5 * 100_000 / 1_000_000 = 0.50
    #   output = 25 * 50_000 / 1_000_000 = 1.25
    #   total  = 1.75
    assert cost_for("anthropic", "claude-opus-4-7", 100_000, 50_000) == pytest.approx(1.75)


def test_cost_for_haiku_realistic_request() -> None:
    # Haiku 4.5 at $1/$5. A 6K input + 1K output request:
    #   input  = 1 * 6_000 / 1_000_000 = 0.006
    #   output = 5 * 1_000 / 1_000_000 = 0.005
    #   total  = 0.011
    assert cost_for("anthropic", "claude-haiku-4-5", 6_000, 1_000) == pytest.approx(0.011)


def test_cost_for_ollama_is_always_zero() -> None:
    # Ollama models are local; they exist in the table with $0/$0
    # so the call path stays uniform.
    assert cost_for("ollama", "qwen2.5-coder:7b", 1_000_000, 1_000_000) == 0.0


def test_cost_for_returns_non_negative_for_all_known_models() -> None:
    # Sanity check on the table: no negative prices snuck in.
    for provider, model in known_models():
        cost = cost_for(provider, model, 1000, 1000)
        assert cost >= 0.0, f"Negative cost for {provider}/{model}"


# ---------- cost_for: error paths ----------


def test_unknown_provider_raises_value_error() -> None:
    with pytest.raises(ValueError, match="Unknown provider"):
        cost_for("openai", "gpt-5", 100, 100)


def test_unknown_model_raises_value_error() -> None:
    with pytest.raises(ValueError, match="Unknown model"):
        cost_for("anthropic", "claude-mythos-preview", 100, 100)


def test_unknown_model_error_lists_known_models() -> None:
    """The error should help the user fix it — not just say 'not found'."""
    with pytest.raises(ValueError) as exc_info:
        cost_for("anthropic", "claude-fake", 0, 0)
    msg = str(exc_info.value)
    # Must reference the file the user needs to edit.
    assert "forge/pricing.py" in msg
    # Must include at least one real model so they see what the format is.
    assert "claude-sonnet-4-6" in msg


def test_negative_tokens_in_raises() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        cost_for("anthropic", "claude-haiku-4-5", -1, 0)


def test_negative_tokens_out_raises() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        cost_for("anthropic", "claude-haiku-4-5", 0, -1)


# ---------- Table integrity ----------


def test_pricing_table_is_not_empty() -> None:
    """Smoke test: someone didn't accidentally clear the table."""
    assert len(PRICING) > 0
    assert all(len(models) > 0 for models in PRICING.values())


def test_all_entries_are_price_entry_instances() -> None:
    """Catches a regression where a dict slipped in instead of a PriceEntry."""
    for provider, models in PRICING.items():
        for model, entry in models.items():
            assert isinstance(entry, PriceEntry), (
                f"{provider}/{model} is not a PriceEntry"
            )


def test_no_negative_prices_in_table() -> None:
    """Defensive: prices must be >= 0. A negative price would leak as a credit."""
    for provider, models in PRICING.items():
        for model, entry in models.items():
            assert entry.input_per_mtok_usd >= 0, f"{provider}/{model} input price negative"
            assert entry.output_per_mtok_usd >= 0, f"{provider}/{model} output price negative"


def test_anthropic_models_priced_above_zero() -> None:
    """Sanity: paid providers aren't accidentally listed as free.

    If this fails, either Anthropic gave us a free tier (unlikely) or
    someone fat-fingered a 0.0 in the table.
    """
    for model, entry in PRICING["anthropic"].items():
        assert entry.input_per_mtok_usd > 0, f"anthropic/{model} has zero input price"
        assert entry.output_per_mtok_usd > 0, f"anthropic/{model} has zero output price"


def test_ollama_models_are_free() -> None:
    """Inverse sanity: local models must stay at $0."""
    for model, entry in PRICING["ollama"].items():
        assert entry.input_per_mtok_usd == 0.0, f"ollama/{model} has non-zero input price"
        assert entry.output_per_mtok_usd == 0.0, f"ollama/{model} has non-zero output price"


def test_price_entry_is_frozen() -> None:
    """PriceEntry is a frozen dataclass — table entries can't be mutated."""
    entry = PRICING["anthropic"]["claude-haiku-4-5"]
    with pytest.raises((AttributeError, Exception)):  # FrozenInstanceError
        entry.input_per_mtok_usd = 999.0  # type: ignore[misc]


# ---------- known_models ----------


def test_known_models_returns_all_pairs() -> None:
    pairs = known_models()
    # Spot-check a few we know are in the table.
    assert ("anthropic", "claude-haiku-4-5") in pairs
    assert ("anthropic", "claude-opus-4-7") in pairs
    assert ("ollama", "qwen2.5-coder:7b") in pairs


def test_known_models_count_matches_table() -> None:
    expected = sum(len(models) for models in PRICING.values())
    assert len(known_models()) == expected
