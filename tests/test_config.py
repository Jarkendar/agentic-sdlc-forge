"""Tests for the config loader and credential validator.

Three things to verify:
1. Round-trip — config.example.toml loads and validates (DoD).
2. Loud failures — missing keys, missing personas, unknown models, bad
   TOML all surface as clear errors.
3. Pricing parity — every model in config.example.toml exists in the
   PRICING table (DoD §1.1).

The credential validator gets its own group because the env-var dance
(load_dotenv, override behavior, monkeypatch isolation) is fiddly enough
to deserve focused tests.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest
from forge.config import (
    PROVIDER_ENV_VARS,
    ForgeConfig,
    Limits,
    ModelAssignment,
    load_config,
    validate_credentials,
)
from forge.pricing import known_models

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLE_CONFIG = REPO_ROOT / ".forge" / "config.example.toml"


# ---------- helpers ----------


def write_config(tmp_path: Path, content: str) -> Path:
    """Write a TOML config to tmp_path/config.toml and return the path."""
    path = tmp_path / "config.toml"
    path.write_text(dedent(content).lstrip(), encoding="utf-8")
    return path


def minimal_config_toml() -> str:
    """A valid config covering all five personas, used as a base for tests
    that mutate one section."""
    return """
        [models.orchestrator]
        provider = "anthropic"
        model = "claude-haiku-4-5"

        [models.planner]
        provider = "anthropic"
        model = "claude-opus-4-7"

        [models.executor]
        provider = "ollama"
        model = "qwen2.5-coder:7b"

        [models.verifier]
        provider = "anthropic"
        model = "claude-sonnet-4-6"

        [models.reporter]
        provider = "anthropic"
        model = "claude-sonnet-4-6"
    """


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Wipe provider env vars before each test.

    Without this, a developer's real ANTHROPIC_API_KEY would mask
    failures in validate_credentials tests.
    """
    for env_var in PROVIDER_ENV_VARS.values():
        monkeypatch.delenv(env_var, raising=False)


@pytest.fixture(autouse=True)
def _no_dotenv(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Prevent python-dotenv from finding a real .env walking up the tree.

    cd into tmp_path so load_dotenv() can't pick up the developer's
    .env file from the repo root or home.
    """
    monkeypatch.chdir(tmp_path)


# ---------- load_config: happy paths ----------


def test_loads_minimal_valid_config(tmp_path: Path) -> None:
    path = write_config(tmp_path, minimal_config_toml())
    cfg = load_config(path)

    assert isinstance(cfg, ForgeConfig)
    assert set(cfg.models.keys()) == {
        "orchestrator", "planner", "executor", "verifier", "reporter",
    }
    assert cfg.models["orchestrator"].model == "claude-haiku-4-5"


def test_loads_example_config_from_repo() -> None:
    """The shipped .forge/config.example.toml must always load.

    This is the DoD check from §1.2: 'config.py loads config.example.toml
    and validates'.
    """
    assert EXAMPLE_CONFIG.exists(), (
        f"Expected {EXAMPLE_CONFIG} to exist — it ships in the repo."
    )
    cfg = load_config(EXAMPLE_CONFIG)
    assert isinstance(cfg, ForgeConfig)


def test_default_limits_applied_when_omitted(tmp_path: Path) -> None:
    """Limits section is optional — defaults match the locked decision in §0.4."""
    path = write_config(tmp_path, minimal_config_toml())
    cfg = load_config(path)
    assert cfg.limits.max_retries_per_task == 3
    assert cfg.limits.max_retries_per_run == 10
    assert cfg.limits.task_timeout_seconds == 600


def test_limits_can_be_overridden(tmp_path: Path) -> None:
    path = write_config(
        tmp_path,
        minimal_config_toml() + """
            [limits]
            max_retries_per_task = 5
            task_timeout_seconds = 900
        """,
    )
    cfg = load_config(path)
    assert cfg.limits.max_retries_per_task == 5
    assert cfg.limits.task_timeout_seconds == 900
    # Unset fields keep defaults.
    assert cfg.limits.max_retries_per_run == 10


# ---------- load_config: failure paths ----------


def test_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="config.example.toml"):
        load_config(tmp_path / "nope.toml")


def test_invalid_toml_raises_value_error(tmp_path: Path) -> None:
    path = tmp_path / "broken.toml"
    path.write_text("this is = not [valid TOML", encoding="utf-8")
    with pytest.raises(ValueError, match="Invalid TOML"):
        load_config(path)


def test_missing_persona_fails_with_clear_message(tmp_path: Path) -> None:
    """If reporter is missing, the error must say 'reporter' explicitly."""
    path = write_config(
        tmp_path,
        """
            [models.orchestrator]
            provider = "anthropic"
            model = "claude-haiku-4-5"

            [models.planner]
            provider = "anthropic"
            model = "claude-opus-4-7"

            [models.executor]
            provider = "ollama"
            model = "qwen2.5-coder:7b"

            [models.verifier]
            provider = "anthropic"
            model = "claude-sonnet-4-6"
        """,
    )
    with pytest.raises(ValueError, match="reporter"):
        load_config(path)


def test_extra_persona_field_rejected(tmp_path: Path) -> None:
    """extra='forbid' on Pydantic — typos must not silently pass."""
    path = write_config(
        tmp_path,
        minimal_config_toml() + """
            [models.architect]
            provider = "anthropic"
            model = "claude-opus-4-7"
        """,
    )
    with pytest.raises(ValueError):
        load_config(path)


def test_extra_field_in_assignment_rejected(tmp_path: Path) -> None:
    """Same protection inside ModelAssignment."""
    path = write_config(
        tmp_path,
        """
            [models.orchestrator]
            provider = "anthropic"
            model = "claude-haiku-4-5"
            tempreture = 0.7  # typo + unsupported field

            [models.planner]
            provider = "anthropic"
            model = "claude-opus-4-7"

            [models.executor]
            provider = "ollama"
            model = "qwen2.5-coder:7b"

            [models.verifier]
            provider = "anthropic"
            model = "claude-sonnet-4-6"

            [models.reporter]
            provider = "anthropic"
            model = "claude-sonnet-4-6"
        """,
    )
    with pytest.raises(ValueError):
        load_config(path)


def test_unknown_provider_rejected(tmp_path: Path) -> None:
    """Pydantic Literal type rejects unsupported providers."""
    # Build the config first, then replace — minimal_config_toml() is
    # raw indented text, replacing inside it before dedent corrupts the
    # match. Write the manipulated TOML directly.
    base = dedent(minimal_config_toml()).lstrip()
    bad = base.replace(
        'provider = "anthropic"\nmodel = "claude-haiku-4-5"',
        'provider = "openai"\nmodel = "gpt-5"',
        1,
    )
    path = tmp_path / "config.toml"
    path.write_text(bad, encoding="utf-8")

    with pytest.raises(ValueError):
        load_config(path)


def test_negative_retry_limit_rejected(tmp_path: Path) -> None:
    """ge=0 on Limits — negative retries make no sense."""
    path = write_config(
        tmp_path,
        minimal_config_toml() + """
            [limits]
            max_retries_per_task = -1
        """,
    )
    with pytest.raises(ValueError):
        load_config(path)


# ---------- load_config: pricing parity ----------


def test_unknown_model_rejected_at_load_time(tmp_path: Path) -> None:
    """If a model isn't in PRICING, config load must fail with a pointer
    to forge/pricing.py — not silently succeed and crash later."""
    path = write_config(
        tmp_path,
        minimal_config_toml().replace(
            "claude-haiku-4-5",
            "claude-mythos-preview",  # not in PRICING
        ),
    )
    with pytest.raises(ValueError, match="forge/pricing.py"):
        load_config(path)


def test_typo_in_model_name_caught(tmp_path: Path) -> None:
    """Realistic typo: tripled 'n' in sonnet."""
    path = write_config(
        tmp_path,
        minimal_config_toml().replace(
            "claude-sonnet-4-6",
            "claude-sonnnet-4-6",  # typo
            1,
        ),
    )
    with pytest.raises(ValueError, match="forge/pricing.py"):
        load_config(path)


def test_example_config_models_all_in_pricing() -> None:
    """DoD §1.1: every model referenced in config.example.toml must
    exist in forge/pricing.py PRICING table.

    This is the explicit unit test the plan calls out.
    """
    cfg = load_config(EXAMPLE_CONFIG)
    priced = known_models()
    for persona, assignment in cfg.models.items():
        pair = (assignment.provider, assignment.model)
        assert pair in priced, (
            f"{persona}: {assignment.provider}/{assignment.model} "
            f"missing from forge/pricing.py PRICING"
        )


# ---------- validate_credentials ----------


def test_validate_credentials_passes_when_keys_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fake-for-test")
    cfg = load_config(write_config(tmp_path, minimal_config_toml()))
    validate_credentials(cfg)  # should not raise


def test_validate_credentials_fails_loudly_on_missing_key(tmp_path: Path) -> None:
    """DoD §1.2: missing API key for a configured provider fails loudly."""
    cfg = load_config(write_config(tmp_path, minimal_config_toml()))
    with pytest.raises(ValueError, match="ANTHROPIC_API_KEY"):
        validate_credentials(cfg)


def test_validate_credentials_treats_empty_string_as_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty/whitespace key is the same as no key — fail."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "   ")
    cfg = load_config(write_config(tmp_path, minimal_config_toml()))
    with pytest.raises(ValueError, match="ANTHROPIC_API_KEY"):
        validate_credentials(cfg)


def test_validate_credentials_skips_unused_providers(tmp_path: Path) -> None:
    """All-Ollama config: no API keys needed, no errors raised.

    This is the offline-dev case from the design discussion — and
    the reason load/validate are separate functions.
    """
    path = write_config(
        tmp_path,
        """
            [models.orchestrator]
            provider = "ollama"
            model = "qwen2.5-coder:7b"

            [models.planner]
            provider = "ollama"
            model = "gemma2:9b"

            [models.executor]
            provider = "ollama"
            model = "qwen2.5-coder:7b"

            [models.verifier]
            provider = "ollama"
            model = "llama3.1:8b"

            [models.reporter]
            provider = "ollama"
            model = "gemma2:9b"
        """,
    )
    cfg = load_config(path)
    validate_credentials(cfg)  # no Anthropic in use → no key required


def test_validate_credentials_loads_dotenv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A .env in the working directory gets loaded automatically —
    user shouldn't need to `source` anything before `forge run`."""
    # python-dotenv finds .env starting from cwd and walking up.
    # _no_dotenv fixture already chdir'd us into tmp_path.
    (tmp_path / ".env").write_text(
        "ANTHROPIC_API_KEY=fake\n",
        encoding="utf-8",
    )
    cfg = load_config(write_config(tmp_path, minimal_config_toml()))
    validate_credentials(cfg)  # should not raise


def test_validate_credentials_real_env_overrides_dotenv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If both shell env and .env set the key, shell wins.

    This is standard dotenv behavior (override=False). Matters because
    CI injects keys via env, and a stray .env shouldn't override that.
    """
    (tmp_path / ".env").write_text(
        "ANTHROPIC_API_KEY=from-dotenv\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("ANTHROPIC_API_KEY", "from-shell")

    cfg = load_config(write_config(tmp_path, minimal_config_toml()))
    validate_credentials(cfg)

    import os
    assert os.environ["ANTHROPIC_API_KEY"] == "from-shell"


# ---------- ForgeConfig helpers ----------


def test_providers_in_use_returns_distinct_providers() -> None:
    cfg = ForgeConfig(
        models={
            "orchestrator": ModelAssignment(provider="anthropic", model="claude-haiku-4-5"),
            "planner": ModelAssignment(provider="anthropic", model="claude-opus-4-7"),
            "executor": ModelAssignment(provider="ollama", model="qwen2.5-coder:7b"),
            "verifier": ModelAssignment(provider="anthropic", model="claude-sonnet-4-6"),
            "reporter": ModelAssignment(provider="anthropic", model="claude-sonnet-4-6"),
        },
        limits=Limits(),
    )
    assert cfg.providers_in_use() == {"anthropic", "ollama"}
