"""Tests for forge.llm.ollama_client.OllamaClient.

Strategy: build the client with a mocked httpx transport. We assert on
both directions — the request payload (proves we send the right schema
and messages) and the response handling (proves we extract token counts,
finish_reason, and validation correctly).

We don't test against a live Ollama server — that's an integration
concern. The HTTP contract is small and stable enough that a test-double
is more reliable than running ollama in CI.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from pydantic import BaseModel

from forge.llm import LLMResponse, LLMTransportError, LLMValidationError
from forge.llm.ollama_client import OllamaClient


class TinyOutput(BaseModel):
    """Schema we use across structured-output tests."""

    name: str
    score: int


# ---------- helpers ----------


def _client_with_handler(handler: Any) -> OllamaClient:
    """Build an OllamaClient with an httpx MockTransport.

    handler: callable taking httpx.Request -> httpx.Response.
    """
    transport = httpx.MockTransport(handler)
    http_client = httpx.Client(transport=transport)
    return OllamaClient(model="qwen2.5-coder:7b", http_client=http_client)


def _ok_response(
    *,
    content: str,
    prompt_tokens: int = 50,
    eval_tokens: int = 20,
    done_reason: str = "stop",
) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "model": "qwen2.5-coder:7b",
            "message": {"role": "assistant", "content": content},
            "prompt_eval_count": prompt_tokens,
            "eval_count": eval_tokens,
            "done_reason": done_reason,
            "done": True,
        },
    )


# ---------- Unstructured (text) completions ----------


def test_text_completion_returns_raw_string() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _ok_response(content="Hello world")

    client = _client_with_handler(handler)
    try:
        result = client.complete(system="be brief", user="hi")
    finally:
        client._client.close()

    assert isinstance(result, LLMResponse)
    assert result.content == "Hello world"
    assert result.tokens_in == 50
    assert result.tokens_out == 20
    assert result.cost_usd == 0.0  # Ollama is free
    assert result.provider == "ollama"
    assert result.model == "qwen2.5-coder:7b"
    assert result.finish_reason == "stop"
    assert result.retried_validation is False


def test_text_completion_sends_no_format_field() -> None:
    """Without a schema, `format` must not appear in the request — we
    don't want to force JSON output when free text was asked for."""
    captured: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return _ok_response(content="ok")

    client = _client_with_handler(handler)
    try:
        client.complete(system="s", user="u")
    finally:
        client._client.close()

    assert "format" not in captured[0]


def test_text_completion_sends_system_and_user_messages() -> None:
    captured: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return _ok_response(content="ok")

    client = _client_with_handler(handler)
    try:
        client.complete(system="you are a planner", user="break this down")
    finally:
        client._client.close()

    msgs = captured[0]["messages"]
    assert msgs[0] == {"role": "system", "content": "you are a planner"}
    assert msgs[1] == {"role": "user", "content": "break this down"}
    assert captured[0]["stream"] is False


# ---------- Structured (schema) completions ----------


def test_structured_completion_returns_pydantic_instance() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _ok_response(content='{"name": "alice", "score": 42}')

    client = _client_with_handler(handler)
    try:
        result = client.complete(system="s", user="u", schema=TinyOutput)
    finally:
        client._client.close()

    assert isinstance(result.content, TinyOutput)
    assert result.content.name == "alice"
    assert result.content.score == 42
    assert result.retried_validation is False


def test_structured_completion_sends_json_schema_in_format() -> None:
    """`format` must be the pydantic model's JSON schema, not the string
    "json" — we want schema-constrained sampling, not arbitrary JSON."""
    captured: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return _ok_response(content='{"name": "x", "score": 1}')

    client = _client_with_handler(handler)
    try:
        client.complete(system="s", user="u", schema=TinyOutput)
    finally:
        client._client.close()

    fmt = captured[0]["format"]
    assert isinstance(fmt, dict)  # schema dict, not the string "json"
    # Pydantic-generated schema includes the property names and required list
    assert "name" in fmt["properties"]
    assert "score" in fmt["properties"]
    assert set(fmt["required"]) == {"name", "score"}


def test_structured_validation_retries_once_on_bad_json() -> None:
    """First response is missing a required field; second response is
    valid. The client must retry exactly once and surface the result
    with retried_validation=True."""
    responses = [
        _ok_response(content='{"name": "alice"}'),  # missing 'score'
        _ok_response(content='{"name": "alice", "score": 7}'),
    ]
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        i = call_count["n"]
        call_count["n"] += 1
        return responses[i]

    client = _client_with_handler(handler)
    try:
        result = client.complete(system="s", user="u", schema=TinyOutput)
    finally:
        client._client.close()

    assert call_count["n"] == 2
    assert isinstance(result.content, TinyOutput)
    assert result.content.score == 7
    assert result.retried_validation is True


def test_structured_validation_retry_includes_error_in_prompt() -> None:
    """The retry prompt must include the validation error so the model
    has something to correct against — empty 'try again' wastes a turn."""
    captured: list[dict[str, Any]] = []
    responses = [
        _ok_response(content='{"name": "alice"}'),  # missing 'score'
        _ok_response(content='{"name": "alice", "score": 7}'),
    ]
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        i = call_count["n"]
        call_count["n"] += 1
        return responses[i]

    client = _client_with_handler(handler)
    try:
        client.complete(system="s", user="original request", schema=TinyOutput)
    finally:
        client._client.close()

    second_user_msg = captured[1]["messages"][1]["content"]
    assert "original request" in second_user_msg
    assert "failed validation" in second_user_msg
    assert "score" in second_user_msg  # the missing field is mentioned


def test_structured_validation_raises_after_two_failures() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _ok_response(content='{"name": "alice"}')  # always missing score

    client = _client_with_handler(handler)
    try:
        with pytest.raises(LLMValidationError) as exc_info:
            client.complete(system="s", user="u", schema=TinyOutput)
    finally:
        client._client.close()
    assert exc_info.value.attempts == 2
    assert "TinyOutput" in str(exc_info.value)


# ---------- Transport failures ----------


def test_http_5xx_raises_transport_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "internal"})

    client = _client_with_handler(handler)
    try:
        with pytest.raises(LLMTransportError):
            client.complete(system="s", user="u")
    finally:
        client._client.close()


def test_connection_error_raises_transport_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    client = _client_with_handler(handler)
    try:
        with pytest.raises(LLMTransportError):
            client.complete(system="s", user="u")
    finally:
        client._client.close()


def test_non_json_body_raises_transport_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not json at all")

    client = _client_with_handler(handler)
    try:
        with pytest.raises(LLMTransportError, match="non-JSON"):
            client.complete(system="s", user="u")
    finally:
        client._client.close()


# ---------- Construction ----------


def test_base_url_trailing_slash_stripped() -> None:
    """Trailing slash on base_url must not produce //api/chat."""
    captured_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_urls.append(str(request.url))
        return _ok_response(content="ok")

    transport = httpx.MockTransport(handler)
    http_client = httpx.Client(transport=transport)
    client = OllamaClient(
        model="qwen2.5-coder:7b",
        base_url="http://host:11434/",
        http_client=http_client,
    )
    try:
        client.complete(system="s", user="u")
    finally:
        http_client.close()

    assert "//api/chat" not in captured_urls[0]
    assert captured_urls[0].endswith("/api/chat")
