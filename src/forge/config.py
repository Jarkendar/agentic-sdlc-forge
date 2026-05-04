"""Configuration loader for the Forge runtime.

Per IMPLEMENTATION_PLAN §0.6.1, config is split:
- secrets (API keys) live in `.env` (gitignored), loaded via python-dotenv
- model assignments and limits live in `.forge/config.toml` (commitable)

Two-step load is intentional:

    cfg = load_config(path)              # always works, even offline
    validate_credentials(cfg)            # eager fail on missing API keys

This split exists so commands that don't actually call paid providers
(e.g. `forge plan` against an all-Ollama config, or `forge config check`)
don't need credentials they won't use. `forge run` calls both, so missing
keys fail at startup instead of mid-pipeline.

The loader also enforces parity with forge.pricing: every (provider, model)
referenced in the config must exist in the PRICING table. Catches typos
in config.toml at load time instead of at first cost calculation.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Literal

from dotenv import find_dotenv, load_dotenv
from forge.pricing import known_models
from pydantic import BaseModel, ConfigDict, Field, field_validator

# ---------------------------------------------------------------------------
# Persona names — locked by IMPLEMENTATION_PLAN §0.3
# ---------------------------------------------------------------------------

PersonaName = Literal["orchestrator", "planner", "executor", "verifier", "reporter"]

# All providers we support. Keep in sync with forge/pricing.py PRICING keys.
ProviderName = Literal["anthropic", "ollama"]

# Map provider -> required env var. Add a new row when adding a new provider.
PROVIDER_ENV_VARS: dict[str, str] = {
    "anthropic": "ANTHROPIC_API_KEY",
    # Ollama runs locally and needs no API key.
}


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


class ModelAssignment(BaseModel):
    """One persona's model assignment."""

    model_config = ConfigDict(extra="forbid")

    provider: ProviderName
    model: str = Field(min_length=1)
    base_url: str | None = Field(
        default=None,
        description="Override endpoint URL. Used by Ollama for the local Docker host.",
    )


class Limits(BaseModel):
    """Runtime limits for the fix loop and per-task execution."""

    model_config = ConfigDict(extra="forbid")

    max_retries_per_task: int = Field(default=3, ge=0)
    max_retries_per_run: int = Field(default=10, ge=0)
    task_timeout_seconds: int = Field(default=600, ge=1)


class ForgeConfig(BaseModel):
    """Full runtime config — what `.forge/config.toml` deserializes to."""

    model_config = ConfigDict(extra="forbid")

    models: dict[PersonaName, ModelAssignment]
    limits: Limits = Field(default_factory=Limits)

    @field_validator("models")
    @classmethod
    def _all_personas_present(
        cls, v: dict[PersonaName, ModelAssignment]
    ) -> dict[PersonaName, ModelAssignment]:
        """Every persona must have a model assignment.

        We don't accept partial configs because the orchestrator would
        crash on the first call to a missing persona. Better to fail at
        load time with a clear list of what's missing.
        """
        required: set[PersonaName] = {
            "orchestrator",
            "planner",
            "executor",
            "verifier",
            "reporter",
        }
        missing = required - set(v.keys())
        if missing:
            raise ValueError(
                f"Missing model assignments for personas: {sorted(missing)}. "
                f"Every persona must be configured under [models.<name>]."
            )
        return v

    def providers_in_use(self) -> set[str]:
        """Distinct providers actually referenced by any persona.

        Used by validate_credentials() — we only check env vars for
        providers that something is configured to use.
        """
        return {assignment.provider for assignment in self.models.values()}


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


def load_config(path: Path) -> ForgeConfig:
    """Load and validate a TOML config file.

    Does NOT check API keys — that's `validate_credentials()`. This split
    lets commands that don't need paid providers (offline dev, config
    inspection) load successfully.

    Args:
        path: Path to a TOML config file (typically `.forge/config.toml`).

    Returns:
        Validated ForgeConfig.

    Raises:
        FileNotFoundError: If `path` doesn't exist.
        ValueError: If the TOML is malformed, fails Pydantic validation,
            or references a (provider, model) not in forge.pricing.PRICING.
    """
    if not path.exists():
        raise FileNotFoundError(
            f"Config file not found: {path}. "
            f"Copy .forge/config.example.toml to .forge/config.toml and edit."
        )

    try:
        with path.open("rb") as f:
            raw = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise ValueError(f"Invalid TOML in {path}: {e}") from e

    cfg = ForgeConfig.model_validate(raw)

    # Parity check: every (provider, model) must exist in the pricing table.
    # This catches typos in config.toml at load time.
    _check_models_in_pricing(cfg, source=str(path))

    return cfg


def validate_credentials(cfg: ForgeConfig) -> None:
    """Verify that every provider used in the config has its API key set.

    Call this from `forge run` before any LLM work begins, so missing
    credentials fail at startup instead of mid-pipeline.

    Loads `.env` from the current working directory if present, so users
    don't need to `source` it manually. Existing environment variables
    take precedence over `.env` (standard dotenv behavior, override=False).

    Args:
        cfg: A loaded ForgeConfig.

    Raises:
        ValueError: If any required env var is missing or empty. Message
            lists every missing key, not just the first one — fix-once
            instead of fix-rerun-fix-rerun.
    """
    # usecwd=True: search for .env starting from the current working
    # directory and walking up. The default uses the calling frame's
    # __file__, which under pytest points at site-packages and finds
    # nothing useful. cwd is also what users expect — they `cd` into
    # their project and `forge run`.
    #
    # override=False: real shell env wins over .env. Lets CI inject keys
    # without a stray .env file overriding them.
    dotenv_path = find_dotenv(usecwd=True)
    if dotenv_path:
        load_dotenv(dotenv_path=dotenv_path, override=False)

    missing: list[str] = []
    for provider in sorted(cfg.providers_in_use()):
        env_var = PROVIDER_ENV_VARS.get(provider)
        if env_var is None:
            # Provider doesn't need credentials (e.g. Ollama).
            continue
        value = os.environ.get(env_var, "").strip()
        if not value:
            missing.append(env_var)

    if missing:
        raise ValueError(
            f"Missing required environment variables: {missing}. "
            f"Set them in .env or your shell environment before running."
        )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _check_models_in_pricing(cfg: ForgeConfig, source: str) -> None:
    """Every (provider, model) in cfg must exist in forge.pricing.PRICING.

    Catches typos in config.toml at load time. Without this, a typo like
    `model = "claude-sonnnet-4-6"` would only surface when LLMClient
    tries to compute cost — possibly minutes into a run.
    """
    priced = known_models()
    unknown: list[str] = []
    for persona, assignment in cfg.models.items():
        pair = (assignment.provider, assignment.model)
        if pair not in priced:
            unknown.append(f"{persona}: {assignment.provider}/{assignment.model}")

    if unknown:
        raise ValueError(
            f"Config {source} references models not in forge/pricing.py: "
            f"{unknown}. Add them to the PRICING table or fix the config."
        )
