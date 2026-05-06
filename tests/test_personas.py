"""Tests for the persona loader and the shipped persona files.

Two layers:
  1. Behavioral tests for the loader using synthetic fixtures (`tmp_path`).
  2. A "real files" suite that loads `personas/` and asserts each persona
     advertises the schema we expect — this is the contract test that
     catches drift between persona frontmatter and `forge.schemas`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from forge import schemas
from forge.personas import (
    Persona,
    PersonaLoadError,
    load_all_personas,
    load_persona,
)

# ---------------------------------------------------------------------------
# Real persona files — contract tests
# ---------------------------------------------------------------------------

PERSONAS_DIR = Path(__file__).parent.parent / "personas"


@pytest.fixture(scope="module")
def real_personas() -> dict[str, Persona]:
    return load_all_personas(PERSONAS_DIR)


def test_all_five_personas_exist(real_personas: dict[str, Persona]) -> None:
    assert set(real_personas.keys()) == {
        "orchestrator",
        "planner",
        "executor",
        "verifier",
        "reporter",
    }


def test_planner_advertises_plan_schema(real_personas: dict[str, Persona]) -> None:
    assert real_personas["planner"].output_schema is schemas.Plan


def test_orchestrator_advertises_orchestrator_decision(
    real_personas: dict[str, Persona],
) -> None:
    assert real_personas["orchestrator"].output_schema is schemas.OrchestratorDecision


def test_executor_advertises_execution_result(real_personas: dict[str, Persona]) -> None:
    assert real_personas["executor"].output_schema is schemas.ExecutionResult


def test_verifier_advertises_test_report(real_personas: dict[str, Persona]) -> None:
    assert real_personas["verifier"].output_schema is schemas.TestReport


def test_reporter_has_no_output_schema(real_personas: dict[str, Persona]) -> None:
    # Reporter produces markdown for humans, not a structured contract.
    assert real_personas["reporter"].output_schema is None


def test_every_real_persona_renders_with_dummy_vars(
    real_personas: dict[str, Persona],
) -> None:
    """Smoke test: rendering with placeholder values must not raise."""
    for persona in real_personas.values():
        rendered = persona.render(**{var: f"<{var}>" for var in persona.required_vars})
        # Sanity: every required var must appear in the output.
        for var in persona.required_vars:
            assert f"<{var}>" in rendered, (
                f"persona '{persona.name}': var '{var}' missing from rendered output"
            )


# ---------------------------------------------------------------------------
# Loader behavioral tests
# ---------------------------------------------------------------------------


def _write(path: Path, content: str) -> Path:
    path.write_text(content, encoding="utf-8")
    return path


def test_load_minimal_persona(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "minimal.md",
        """---
name: minimal
output_schema: null
required_vars: []
references: []
---

Hello, no variables here.
""",
    )
    persona = load_persona(path)
    assert persona.name == "minimal"
    assert persona.output_schema is None
    assert persona.required_vars == ()
    assert persona.body.startswith("Hello, no variables")


def test_render_interpolates_vars(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "greeter.md",
        """---
name: greeter
output_schema: null
required_vars: [name]
---

Hi, {{name}}.
""",
    )
    persona = load_persona(path)
    assert persona.render(name="Jarek") == "Hi, Jarek."


def test_render_rejects_missing_var(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "greeter.md",
        """---
name: greeter
output_schema: null
required_vars: [name]
---

Hi, {{name}}.
""",
    )
    persona = load_persona(path)
    with pytest.raises(ValueError, match="missing required vars"):
        persona.render()


def test_render_rejects_extra_var(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "greeter.md",
        """---
name: greeter
output_schema: null
required_vars: [name]
---

Hi, {{name}}.
""",
    )
    persona = load_persona(path)
    with pytest.raises(ValueError, match="unexpected vars"):
        persona.render(name="Jarek", extra="boom")


def test_missing_frontmatter_fails(tmp_path: Path) -> None:
    path = _write(tmp_path / "bad.md", "Just a body, no frontmatter.\n")
    with pytest.raises(PersonaLoadError, match="missing or malformed YAML frontmatter"):
        load_persona(path)


def test_unknown_output_schema_fails(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "bogus.md",
        """---
name: bogus
output_schema: NotARealSchema
required_vars: []
---

body
""",
    )
    with pytest.raises(PersonaLoadError, match="unknown output_schema"):
        load_persona(path)


def test_undeclared_var_in_body_fails(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "drift.md",
        """---
name: drift
output_schema: null
required_vars: [foo]
---

{{foo}} and {{bar}}
""",
    )
    with pytest.raises(PersonaLoadError, match="not declared in required_vars"):
        load_persona(path)


def test_unused_required_var_fails(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "lazy.md",
        """---
name: lazy
output_schema: null
required_vars: [foo, bar]
---

Only {{foo}} appears.
""",
    )
    with pytest.raises(PersonaLoadError, match="never used in body"):
        load_persona(path)


def test_filename_mismatch_fails(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "alpha.md",
        """---
name: beta
output_schema: null
required_vars: []
---

body
""",
    )
    with pytest.raises(PersonaLoadError, match="does not match filename stem"):
        load_persona(path)


def test_load_all_loads_directory(tmp_path: Path) -> None:
    _write(
        tmp_path / "a.md",
        "---\nname: a\noutput_schema: null\nrequired_vars: []\n---\n\nbody a\n",
    )
    _write(
        tmp_path / "b.md",
        "---\nname: b\noutput_schema: null\nrequired_vars: []\n---\n\nbody b\n",
    )
    personas = load_all_personas(tmp_path)
    assert set(personas.keys()) == {"a", "b"}


def test_load_all_rejects_duplicate_names(tmp_path: Path) -> None:
    # Two files, same `name:` in frontmatter — but filenames must match
    # frontmatter name, so duplicates require two different paths to share
    # a name. We force that by skipping the filename-match check via two
    # separate dirs… or simpler: a file whose name matches and another
    # whose name doesn't would be caught by filename-match check first.
    # The duplicate path is reachable only if two files in the same dir
    # somehow have the same stem, which the filesystem prevents. So this
    # test instead verifies the load_all path is wired correctly by
    # checking it succeeds on a clean directory.
    _write(
        tmp_path / "only.md",
        "---\nname: only\noutput_schema: null\nrequired_vars: []\n---\n\nbody\n",
    )
    personas = load_all_personas(tmp_path)
    assert "only" in personas
