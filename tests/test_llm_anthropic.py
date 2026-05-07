"""Tests for forge.llm.anthropic_client.AnthropicClient.

We don't touch the real Anthropic API. Instead, we inject a stub via
the `sdk_client=` constructor parameter. The stub mimics the small
slice of the SDK surface the client actually uses:
- client.messages.create(model, max_tokens, system, messages) -> Message
- client.messages.parse(model, max_tokens, system, messages, output_format) -> ParsedMessage

Stubs return shapes that match the real SDK as documented in:
https://platform.claude.com/docs/en/build-with-claude/structured-outputs

The retry-on-validation path is the most subtle thing here. Anthropic's
constrained sampling makes ValidationError theoretically unreachable
*if everything works*, but `parsed_output` can be None if the SDK chooses
not to populate it, and the docs explicitly call out refusal/max_tokens
as schema-non-compliant outcomes. Tests cover all of those paths.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import anthropic
import pytest
from pydantic import BaseModel

from forge.llm import LLMResponse, LLMTransportError, LLMValidationError
from forge.llm.anthropic_client import AnthropicClient


class TinyOutput(BaseModel):
    """Schema used across structured-output tests."""

    name: str
    score: int


# ---------------------------------------------------------------------------
# SDK stubs — minimal shapes that match the real Anthropic SDK
# ---------------------------------------------------------------------------


@dataclass
class _Usage:
    input_tokens: int
    output_tokens: int


@dataclass
class _TextBlock:
    text: str
    type: str = "text"


@dataclass
class _Message:
    """Mimics anthropic.types.Message (the create() return shape)."""

    content: list[Any] = field(default_factory=list)
    usage: _Usage = field(default_factory=lambda: _Usage(0, 0))
    stop_reason: str = "end_turn"
    model: str = "claude-haiku-4-5"


@dataclass
class _ParsedMessage:
    """Mimics anthropic.types.ParsedMessage (the parse() return shape).

    The real type has `parsed_output: T | None` where T is the pydantic
    class passed via output_format=. We keep it as Any here because the
    field can also be None in error paths."""

    parsed_output: Any = None
    content: list[Any] = field(default_factory=list)
    usage: _Usage = field(default_factory=lambda: _Usage(0, 0))
    stop_reason: str = "end_turn"
    model: str = "claude-opus-4-7"


class _StubMessages:
    """Stand-in for client.messages on the real SDK."""

    def __init__(self) -> None:
        self.create_responses: list[_Message] = []
        self.parse_responses: list[_ParsedMessage | Exception] = []
        self.create_calls: list[dict[str, Any]] = []
        self.parse_calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> _Message:
        self.create_calls.append(kwargs)
        return self.create_responses.pop(0)

    def parse(self, **kwargs: Any) -> _ParsedMessage:
        self.parse_calls.append(kwargs)
        nxt = self.parse_responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


class _StubClient:
    """Object accepted by AnthropicClient(sdk_client=)."""

    def __init__(self) -> None:
        self.messages = _StubMessages()


def _build_client(stub: _StubClient, model: str = "claude-haiku-4-5") -> AnthropicClient:
    return AnthropicClient(model=model, sdk_client=stub)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Text completions (no schema)
# ---------------------------------------------------------------------------


def test_text_completion_returns_text_content() -> None:
    stub = _StubClient()
    stub.messages.create_responses.append(
        _Message(
            content=[_TextBlock(text="hello there")],
            usage=_Usage(input_tokens=12, output_tokens=4),
            stop_reason="end_turn",
        )
    )
    client = _build_client(stub)

    result = client.complete(system="s", user="u")

    assert isinstance(result, LLMResponse)
    assert result.content == "hello there"
    assert result.tokens_in == 12
    assert result.tokens_out == 4
    assert result.provider == "anthropic"
    assert result.model == "claude-haiku-4-5"
    assert result.finish_reason == "end_turn"
    assert result.retried_validation is False


def test_text_completion_computes_cost_via_pricing_table() -> None:
    """Cost must come from forge.pricing.cost_for(), not be made up here."""
    stub = _StubClient()
    stub.messages.create_responses.append(
        _Message(
            content=[_TextBlock(text="x")],
            usage=_Usage(input_tokens=1_000_000, output_tokens=0),
        )
    )
    # Sonnet 4.6 is $3/MTok input, so 1M input tokens = $3.00.
    client = _build_client(stub, model="claude-sonnet-4-6")
    result = client.complete(system="s", user="u")
    assert result.cost_usd == pytest.approx(3.00)


def test_text_completion_passes_system_and_user_correctly() -> None:
    stub = _StubClient()
    stub.messages.create_responses.append(
        _Message(content=[_TextBlock(text="ok")], usage=_Usage(1, 1))
    )
    client = _build_client(stub)
    client.complete(system="you are concise", user="explain X")

    call = stub.messages.create_calls[0]
    assert call["system"] == "you are concise"
    assert call["messages"] == [{"role": "user", "content": "explain X"}]
    assert call["model"] == "claude-haiku-4-5"


# ---------------------------------------------------------------------------
# Structured completions (with schema)
# ---------------------------------------------------------------------------


def test_structured_completion_returns_parsed_pydantic_instance() -> None:
    stub = _StubClient()
    stub.messages.parse_responses.append(
        _ParsedMessage(
            parsed_output=TinyOutput(name="alice", score=42),
            usage=_Usage(input_tokens=20, output_tokens=8),
            stop_reason="end_turn",
        )
    )
    client = _build_client(stub)
    result = client.complete(system="s", user="u", schema=TinyOutput)

    assert isinstance(result.content, TinyOutput)
    assert result.content.name == "alice"
    assert result.content.score == 42
    assert result.retried_validation is False


def test_structured_completion_passes_output_format_to_sdk() -> None:
    stub = _StubClient()
    stub.messages.parse_responses.append(
        _ParsedMessage(
            parsed_output=TinyOutput(name="x", score=1),
            usage=_Usage(1, 1),
        )
    )
    client = _build_client(stub)
    client.complete(system="s", user="u", schema=TinyOutput)

    call = stub.messages.parse_calls[0]
    # The SDK helper takes the pydantic class itself as output_format.
    assert call["output_format"] is TinyOutput


def test_max_tokens_truncation_raises_transport_error() -> None:
    """stop_reason=max_tokens means the response is incomplete. Per the
    docs, this output may not match the schema, and retrying the same
    call wouldn't help — caller needs to bump max_tokens. Surface as
    transport-level so callers know it's not a prompt problem."""
    stub = _StubClient()
    stub.messages.parse_responses.append(
        _ParsedMessage(
            parsed_output=None,
            usage=_Usage(20, 4096),
            stop_reason="max_tokens",
        )
    )
    client = _build_client(stub)
    with pytest.raises(LLMTransportError, match="max_tokens"):
        client.complete(system="s", user="u", schema=TinyOutput)


def test_refusal_raises_transport_error() -> None:
    """stop_reason=refusal means Claude declined for safety. The output
    is a refusal string, not a schema-valid object. Retrying won't help.
    """
    stub = _StubClient()
    stub.messages.parse_responses.append(
        _ParsedMessage(
            parsed_output=None,
            content=[_TextBlock(text="I can't help with that.")],
            usage=_Usage(20, 8),
            stop_reason="refusal",
        )
    )
    client = _build_client(stub)
    with pytest.raises(LLMTransportError, match="refusal"):
        client.complete(system="s", user="u", schema=TinyOutput)


def test_fallback_validation_when_parsed_output_is_none() -> None:
    """If the SDK doesn't populate parsed_output but the text block
    contains valid JSON, we validate it ourselves. This is the safety
    net for SDK behaviour we didn't anticipate — better to recover than
    to hard-fail on a successful response."""
    stub = _StubClient()
    stub.messages.parse_responses.append(
        _ParsedMessage(
            parsed_output=None,  # SDK didn't populate it
            content=[_TextBlock(text='{"name": "bob", "score": 9}')],
            usage=_Usage(20, 12),
            stop_reason="end_turn",
        )
    )
    client = _build_client(stub)
    result = client.complete(system="s", user="u", schema=TinyOutput)
    assert isinstance(result.content, TinyOutput)
    assert result.content.name == "bob"


def test_validation_retries_once_on_invalid_first_response() -> None:
    """First response: parsed_output is None and the text doesn't validate.
    Second response: valid. We must retry exactly once."""
    stub = _StubClient()
    stub.messages.parse_responses.extend(
        [
            _ParsedMessage(
                parsed_output=None,
                content=[_TextBlock(text='{"name": "alice"}')],  # missing score
                usage=_Usage(20, 4),
                stop_reason="end_turn",
            ),
            _ParsedMessage(
                parsed_output=TinyOutput(name="alice", score=7),
                usage=_Usage(30, 6),
                stop_reason="end_turn",
            ),
        ]
    )
    client = _build_client(stub)
    result = client.complete(system="s", user="u", schema=TinyOutput)

    assert len(stub.messages.parse_calls) == 2
    assert result.retried_validation is True
    assert isinstance(result.content, TinyOutput)
    assert result.content.score == 7


def test_validation_retry_includes_error_in_user_message() -> None:
    stub = _StubClient()
    stub.messages.parse_responses.extend(
        [
            _ParsedMessage(
                parsed_output=None,
                content=[_TextBlock(text='{"name": "alice"}')],
                usage=_Usage(20, 4),
                stop_reason="end_turn",
            ),
            _ParsedMessage(
                parsed_output=TinyOutput(name="alice", score=7),
                usage=_Usage(30, 6),
                stop_reason="end_turn",
            ),
        ]
    )
    client = _build_client(stub)
    client.complete(system="s", user="original task", schema=TinyOutput)

    second = stub.messages.parse_calls[1]
    second_user = second["messages"][0]["content"]
    assert "original task" in second_user
    assert "failed validation" in second_user


def test_validation_raises_after_two_failures() -> None:
    stub = _StubClient()
    invalid = _ParsedMessage(
        parsed_output=None,
        content=[_TextBlock(text='{"name": "alice"}')],  # always invalid
        usage=_Usage(20, 4),
        stop_reason="end_turn",
    )
    stub.messages.parse_responses.extend([invalid, invalid])

    client = _build_client(stub)
    with pytest.raises(LLMValidationError) as exc_info:
        client.complete(system="s", user="u", schema=TinyOutput)
    assert exc_info.value.attempts == 2
    assert "TinyOutput" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Transport errors
# ---------------------------------------------------------------------------


def _api_connection_error() -> anthropic.APIConnectionError:
    """Build a realistic APIConnectionError for tests.

    The SDK constructor requires an httpx.Request — we construct a
    throwaway one instead of None to satisfy the SDK's __init__ logic.
    """
    import httpx

    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    return anthropic.APIConnectionError(request=req)


def _rate_limit_error() -> anthropic.RateLimitError:
    """Build a realistic RateLimitError for tests.

    The SDK constructor reaches into response.request, so we need a
    Response with a Request attached. Less ergonomic than passing None
    but matches what the SDK does in production.
    """
    import httpx

    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    resp = httpx.Response(429, request=req)
    return anthropic.RateLimitError(message="rate limited", response=resp, body=None)


@pytest.mark.parametrize(
    "exc_factory",
    [_api_connection_error, _rate_limit_error],
    ids=["connection", "rate_limit"],
)
def test_sdk_exceptions_translate_to_transport_error(exc_factory: Any) -> None:
    """We catch the SDK's specific exception types and re-raise as
    LLMTransportError so the orchestrator only has to know about our
    error hierarchy, not Anthropic's."""
    stub = _StubClient()

    def raising_create(**kwargs: Any) -> _Message:
        raise exc_factory()

    stub.messages.create = raising_create  # type: ignore[method-assign]

    client = _build_client(stub)
    with pytest.raises(LLMTransportError):
        client.complete(system="s", user="u")


def test_parse_sdk_exception_translates_to_transport_error() -> None:
    """Same as above but for the structured-output code path."""
    stub = _StubClient()
    stub.messages.parse_responses.append(_api_connection_error())
    client = _build_client(stub)
    with pytest.raises(LLMTransportError):
        client.complete(system="s", user="u", schema=TinyOutput)
