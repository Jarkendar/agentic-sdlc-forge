"""Tests for forge.llm.factory.

Pure routing — no network. We pass in synthetic ForgeConfig instances
and assert that get_client() returns the right provider class with the
right model wired in.
"""

from __future__ import annotations

import pytest

from forge.config import ForgeConfig, Limits, ModelAssignment
from forge.llm import LLMClient, get_client
from forge.llm.anthropic_client import AnthropicClient
from forge.llm.ollama_client import OllamaClient


def _config(
    *,
    orchestrator: tuple[str, str] = ("anthropic", "claude-haiku-4-5"),
    planner: tuple[str, str] = ("anthropic", "claude-opus-4-7"),
    executor: tuple[str, str] = ("ollama", "qwen2.5-coder:7b"),
    verifier: tuple[str, str] = ("anthropic", "claude-sonnet-4-6"),
    reporter: tuple[str, str] = ("anthropic", "claude-sonnet-4-6"),
    executor_base_url: str | None = None,
) -> ForgeConfig:
    """Build a valid ForgeConfig from (provider, model) pairs.

    Defaults match the example config in .forge/config.example.toml so
    most tests need no overrides.
    """
    return ForgeConfig(
        models={
            "orchestrator": ModelAssignment(provider=orchestrator[0], model=orchestrator[1]),  # type: ignore[arg-type]
            "planner": ModelAssignment(provider=planner[0], model=planner[1]),  # type: ignore[arg-type]
            "executor": ModelAssignment(
                provider=executor[0],  # type: ignore[arg-type]
                model=executor[1],
                base_url=executor_base_url,
            ),
            "verifier": ModelAssignment(provider=verifier[0], model=verifier[1]),  # type: ignore[arg-type]
            "reporter": ModelAssignment(provider=reporter[0], model=reporter[1]),  # type: ignore[arg-type]
        },
        limits=Limits(),
    )


# ---------- Provider routing ----------


def test_anthropic_persona_returns_anthropic_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fake")
    cfg = _config()
    client = get_client("planner", cfg)
    assert isinstance(client, AnthropicClient)
    assert client.provider == "anthropic"
    assert client.model == "claude-opus-4-7"


def test_ollama_persona_returns_ollama_client() -> None:
    cfg = _config()
    client = get_client("executor", cfg)
    assert isinstance(client, OllamaClient)
    assert client.provider == "ollama"
    assert client.model == "qwen2.5-coder:7b"
    client.close()


def test_returned_client_is_subclass_of_llmclient(monkeypatch: pytest.MonkeyPatch) -> None:
    """The factory contract: every return value satisfies LLMClient."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fake")
    cfg = _config()
    for persona in ("orchestrator", "planner", "executor", "verifier", "reporter"):
        client = get_client(persona, cfg)  # type: ignore[arg-type]
        assert isinstance(client, LLMClient), f"{persona} returned non-LLMClient"
        if isinstance(client, OllamaClient):
            client.close()


# ---------- Ollama base_url plumbing ----------


def test_ollama_base_url_passed_through_when_set() -> None:
    cfg = _config(executor_base_url="http://gpu-box.local:11434")
    client = get_client("executor", cfg)
    assert isinstance(client, OllamaClient)
    assert client.base_url == "http://gpu-box.local:11434"
    client.close()


def test_ollama_base_url_falls_back_to_default_when_unset() -> None:
    cfg = _config(executor_base_url=None)
    client = get_client("executor", cfg)
    assert isinstance(client, OllamaClient)
    # Default lives in the client; we just check the factory didn't
    # pass an explicit None through and break the default.
    assert client.base_url.startswith("http://")
    client.close()


# ---------- Defence-in-depth ----------


def test_unknown_provider_raises_clear_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """If the Literal[ProviderName] invariant is ever violated, the
    factory must fail loudly rather than silently returning None."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fake")
    cfg = _config()
    # Bypass pydantic by mutating a constructed instance — simulates a
    # future refactor that loosens the type without updating the factory.
    object.__setattr__(cfg.models["planner"], "provider", "openai")
    with pytest.raises(ValueError, match="Unknown provider"):
        get_client("planner", cfg)
