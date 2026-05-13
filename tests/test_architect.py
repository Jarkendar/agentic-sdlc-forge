"""Tests for forge.agents.architect.

Drives run_architect with a fake LLMClient. Covers:
- happy path: valid output passes through unchanged
- malformed output triggers exactly one retry
- still-malformed output after retry raises ArchitectError
- persona with non-null output_schema is rejected
- event_log without run_id is rejected
- empty answers are dropped from the prompt
- question IDs sort numerically (1.2 before 1.10)
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import BaseModel

from forge.agents.architect import (
    ArchitectError,
    _format_answers,
    _missing_sections,
    _sort_key,
    run_architect,
)
from forge.event_log import EventLog
from forge.llm.base import LLMClient, LLMResponse
from forge.personas import Persona, load_persona

# ---------------------------------------------------------------------------
# Fakes (duplicated from test_planner.py, see test_cli.py for the rationale)
# ---------------------------------------------------------------------------


class FakeLLM(LLMClient):
    """Returns a sequence of pre-baked responses, one per .complete() call."""

    provider = "fake"

    def __init__(self, *responses: LLMResponse) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, object]] = []

    def complete(
        self,
        *,
        system: str,
        user: str,
        schema: type[BaseModel] | None = None,
    ) -> LLMResponse:
        self.calls.append({"system": system, "user": user, "schema": schema})
        return self._responses.pop(0)


def _resp(content: str) -> LLMResponse:
    return LLMResponse(
        content=content,
        tokens_in=100,
        tokens_out=500,
        cost_usd=0.01,
        duration_ms=1234,
        model="fake-model",
        provider="fake",
        finish_reason="end_turn",
    )


# A markdown document with all five required section headers.
_VALID_DOC = """# Architecture Map — Demo

> A demo project.

## 1. Project Identity

- Problem: x
- Platform: y

## 2. Technology Stack

### Required Technologies
- thing

## 3. Architecture & Responsibilities

### Pattern
MVI

## 4. System Boundaries & Integrations

### External APIs
REST

## 5. Known Constraints & Tech Debt

None.
"""


# Same shape but missing section 4 entirely.
_DOC_MISSING_SECTION_4 = """# Architecture Map — Demo

## 1. Project Identity
x

## 2. Technology Stack
x

## 3. Architecture & Responsibilities
x

## 5. Known Constraints & Tech Debt
x
"""


# ---------------------------------------------------------------------------
# Persona fixture — load the actual architect persona from templates so
# tests catch drift between the persona file and the agent's assumptions.
# ---------------------------------------------------------------------------


@pytest.fixture
def architect_persona() -> Persona:
    """Load the architect persona from the bundled templates."""
    # Resolve via the package — works for editable install and wheel both.
    from importlib import resources

    src = resources.files("forge.templates.forge_dir.personas").joinpath("architect.md")
    # importlib.resources doesn't give us a real Path on a wheel, so
    # write the file out to tmp and load from there.
    # On editable install src IS a Path; load_persona accepts both
    # if we coerce.
    text = src.read_text(encoding="utf-8")
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "architect.md"
        path.write_text(text, encoding="utf-8")
        return load_persona(path)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_happy_path_returns_content_unchanged(architect_persona: Persona) -> None:
    """Valid output from first call returns verbatim, no retry."""
    llm = FakeLLM(_resp(_VALID_DOC))
    out = run_architect(
        project_name="Demo",
        answers={"1.1": "Demo", "1.2": "A demo."},
        persona=architect_persona,
        llm=llm,
    )
    assert out == _VALID_DOC
    assert len(llm.calls) == 1


def test_prompt_includes_project_name_and_answers(architect_persona: Persona) -> None:
    """The rendered system prompt carries project_name + raw_answers."""
    llm = FakeLLM(_resp(_VALID_DOC))
    run_architect(
        project_name="MyApp",
        answers={"1.1": "MyApp", "2.1": "Android"},
        persona=architect_persona,
        llm=llm,
    )
    system_prompt = llm.calls[0]["system"]
    assert isinstance(system_prompt, str)
    assert "MyApp" in system_prompt
    assert "Android" in system_prompt
    assert "[2.1]" in system_prompt


def test_blank_project_name_falls_back_to_untitled(architect_persona: Persona) -> None:
    """Empty/whitespace project_name → 'Untitled Project' in the prompt."""
    llm = FakeLLM(_resp(_VALID_DOC))
    run_architect(
        project_name="   ",
        answers={"2.1": "Android"},
        persona=architect_persona,
        llm=llm,
    )
    assert "Untitled Project" in llm.calls[0]["system"]


# ---------------------------------------------------------------------------
# Retry behavior
# ---------------------------------------------------------------------------


def test_retry_on_missing_sections(architect_persona: Persona) -> None:
    """First response missing a section → retry, second passes → success."""
    llm = FakeLLM(_resp(_DOC_MISSING_SECTION_4), _resp(_VALID_DOC))
    out = run_architect(
        project_name="Demo",
        answers={"1.1": "Demo"},
        persona=architect_persona,
        llm=llm,
    )
    assert out == _VALID_DOC
    assert len(llm.calls) == 2
    # Retry prompt should explicitly mention the missing section number
    second_user = llm.calls[1]["user"]
    assert isinstance(second_user, str)
    assert "## 4." in second_user


def test_hard_error_after_retry_still_missing(architect_persona: Persona) -> None:
    """Two malformed responses in a row → ArchitectError."""
    llm = FakeLLM(_resp(_DOC_MISSING_SECTION_4), _resp(_DOC_MISSING_SECTION_4))
    with pytest.raises(ArchitectError, match=r"missing required section"):
        run_architect(
            project_name="Demo",
            answers={},
            persona=architect_persona,
            llm=llm,
        )
    assert len(llm.calls) == 2


# ---------------------------------------------------------------------------
# Misconfiguration
# ---------------------------------------------------------------------------


def test_persona_with_output_schema_rejected(architect_persona: Persona, tmp_path: Path) -> None:
    """Architect persona with a non-null output_schema → ArchitectError.

    We can't mutate the frozen dataclass, so build a fresh Persona
    with the same fields except output_schema=Plan.
    """
    from forge.schemas import Plan

    bad = Persona(
        name=architect_persona.name,
        output_schema=Plan,
        required_vars=architect_persona.required_vars,
        references=architect_persona.references,
        body=architect_persona.body,
        source_path=architect_persona.source_path,
    )
    llm = FakeLLM(_resp(_VALID_DOC))
    with pytest.raises(ArchitectError, match=r"output_schema=null"):
        run_architect(
            project_name="x",
            answers={},
            persona=bad,
            llm=llm,
        )


def test_event_log_without_run_id_rejected(architect_persona: Persona, tmp_path: Path) -> None:
    """Passing event_log but no run_id is a programmer error → reject."""
    log = EventLog(tmp_path / "events.jsonl")
    try:
        with pytest.raises(ArchitectError, match=r"event_log provided without run_id"):
            run_architect(
                project_name="x",
                answers={},
                persona=architect_persona,
                llm=FakeLLM(_resp(_VALID_DOC)),
                event_log=log,
                run_id=None,
            )
    finally:
        log.close()


def test_non_string_content_rejected(architect_persona: Persona) -> None:
    """If the provider returns a model instance, architect raises."""

    class WeirdResp:
        pass

    llm = FakeLLM(
        LLMResponse(
            content=WeirdResp(),  # type: ignore[arg-type]
            tokens_in=10,
            tokens_out=10,
            cost_usd=0.0,
            duration_ms=1,
            model="fake",
            provider="fake",
            finish_reason="end_turn",
        )
    )
    with pytest.raises(ArchitectError, match=r"non-string content"):
        run_architect(
            project_name="x",
            answers={},
            persona=architect_persona,
            llm=llm,
        )


# ---------------------------------------------------------------------------
# Internals — answer formatting and section detection
# ---------------------------------------------------------------------------


def test_format_answers_drops_empty() -> None:
    out = _format_answers({"1.1": "Demo", "1.2": "", "1.3": "  "})
    assert "[1.1]" in out
    assert "[1.2]" not in out
    assert "[1.3]" not in out


def test_format_answers_empty_dict_message() -> None:
    out = _format_answers({})
    assert out == "(no answers provided)"


def test_format_answers_all_empty_message() -> None:
    out = _format_answers({"1.1": "", "1.2": "  "})
    assert "(no non-empty answers)" in out


def test_sort_key_numeric_order() -> None:
    """Question IDs must sort numerically, not lexicographically."""
    keys = ["1.10", "1.2", "1.1", "2.3"]
    sorted_keys = sorted(keys, key=_sort_key)
    assert sorted_keys == ["1.1", "1.2", "1.10", "2.3"]


def test_missing_sections_detects_all_five_when_blank() -> None:
    assert _missing_sections("") == ["## 1.", "## 2.", "## 3.", "## 4.", "## 5."]


def test_missing_sections_returns_empty_when_valid() -> None:
    assert _missing_sections(_VALID_DOC) == []


def test_missing_sections_partial() -> None:
    assert _missing_sections(_DOC_MISSING_SECTION_4) == ["## 4."]
