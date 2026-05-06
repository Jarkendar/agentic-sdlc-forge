"""Persona loader — reads markdown files with YAML frontmatter from .forge/personas/.

Each persona file is structured as:

    ---
    name: planner
    output_schema: Plan
    required_vars: [user_story, architecture_map, file_tree]
    references: [git_flow.md, architecture_map.md]
    ---

    # System prompt body in markdown
    Use {{user_story}} and {{architecture_map}} as interpolation points.

The loader:
    1. parses the frontmatter,
    2. resolves `output_schema` against forge.schemas (whitelist-only),
    3. validates that body's `{{var}}` references match `required_vars` exactly,
    4. interpolates variables on render.

`output_schema: null` is allowed (used by the Reporter, which produces free-text
markdown for humans, not a structured contract for another agent).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel

from forge import schemas

# Whitelist of schema names a persona's frontmatter may reference.
# This is an explicit allowlist — never resolve arbitrary attribute names
# from `forge.schemas`, since frontmatter is a config surface and a typo
# (or a malicious edit) shouldn't be able to summon, say, `_utcnow`.
_ALLOWED_OUTPUT_SCHEMAS: dict[str, type[BaseModel]] = {
    "Plan": schemas.Plan,
    "Task": schemas.Task,
    "ExecutionResult": schemas.ExecutionResult,
    "TestReport": schemas.TestReport,
    "OrchestratorDecision": schemas.OrchestratorDecision,
}

_FRONTMATTER_RE = re.compile(
    r"\A---\s*\n(?P<frontmatter>.*?)\n---\s*\n(?P<body>.*)\Z",
    re.DOTALL,
)
_INTERPOLATION_RE = re.compile(r"\{\{\s*(\w+)\s*\}\}")


@dataclass(frozen=True)
class Persona:
    """A loaded persona — frontmatter metadata + system prompt body.

    Immutable so callers can pass it around without worrying about
    accidental mutation between agent invocations.
    """

    name: str
    output_schema: type[BaseModel] | None
    required_vars: tuple[str, ...]
    references: tuple[str, ...]
    body: str
    source_path: Path

    def render(self, **vars: str) -> str:
        """Interpolate `{{var}}` placeholders in the body.

        Raises ValueError if any required variable is missing or any extra
        variable is supplied — strictness is on purpose, drift between
        prompt and call site is exactly what frontmatter exists to catch.
        """
        provided = set(vars.keys())
        required = set(self.required_vars)

        missing = required - provided
        if missing:
            raise ValueError(
                f"Persona '{self.name}': missing required vars: {sorted(missing)}"
            )
        extra = provided - required
        if extra:
            raise ValueError(
                f"Persona '{self.name}': unexpected vars: {sorted(extra)}"
            )

        def _replace(match: re.Match[str]) -> str:
            return vars[match.group(1)]

        return _INTERPOLATION_RE.sub(_replace, self.body)


class PersonaLoadError(Exception):
    """Raised when a persona file is malformed or inconsistent."""


def load_persona(path: Path) -> Persona:
    """Load and validate a single persona markdown file."""
    raw = path.read_text(encoding="utf-8")
    match = _FRONTMATTER_RE.match(raw)
    if match is None:
        raise PersonaLoadError(
            f"{path}: missing or malformed YAML frontmatter (expected leading '---' block)"
        )

    try:
        frontmatter: Any = yaml.safe_load(match.group("frontmatter"))
    except yaml.YAMLError as exc:
        raise PersonaLoadError(f"{path}: invalid YAML frontmatter: {exc}") from exc

    if not isinstance(frontmatter, dict):
        raise PersonaLoadError(f"{path}: frontmatter must be a YAML mapping")

    body = match.group("body").strip()
    name = frontmatter.get("name")
    if not isinstance(name, str) or not name:
        raise PersonaLoadError(f"{path}: 'name' is required and must be a non-empty string")

    if name != path.stem:
        raise PersonaLoadError(
            f"{path}: frontmatter name '{name}' does not match filename stem '{path.stem}'"
        )

    schema_name = frontmatter.get("output_schema")
    output_schema: type[BaseModel] | None
    if schema_name is None:
        output_schema = None
    elif not isinstance(schema_name, str):
        raise PersonaLoadError(f"{path}: 'output_schema' must be a string or null")
    elif schema_name not in _ALLOWED_OUTPUT_SCHEMAS:
        raise PersonaLoadError(
            f"{path}: unknown output_schema '{schema_name}'. "
            f"Allowed: {sorted(_ALLOWED_OUTPUT_SCHEMAS)}"
        )
    else:
        output_schema = _ALLOWED_OUTPUT_SCHEMAS[schema_name]

    required_vars = _coerce_str_list(frontmatter.get("required_vars", []), path, "required_vars")
    references = _coerce_str_list(frontmatter.get("references", []), path, "references")

    # Cross-check: every {{var}} in the body must be in required_vars,
    # and every required_var must appear at least once in the body.
    body_vars = set(_INTERPOLATION_RE.findall(body))
    declared = set(required_vars)
    undeclared = body_vars - declared
    if undeclared:
        raise PersonaLoadError(
            f"{path}: body uses {{{{var}}}} placeholders not declared in required_vars: "
            f"{sorted(undeclared)}"
        )
    unused = declared - body_vars
    if unused:
        raise PersonaLoadError(
            f"{path}: required_vars declared but never used in body: {sorted(unused)}"
        )

    return Persona(
        name=name,
        output_schema=output_schema,
        required_vars=tuple(required_vars),
        references=tuple(references),
        body=body,
        source_path=path,
    )


def load_all_personas(personas_dir: Path) -> dict[str, Persona]:
    """Load every *.md file under `personas_dir`, keyed by persona name.

    Raises PersonaLoadError on the first malformed file — partial loads are
    forbidden, since a half-loaded persona set is worse than no personas at all.
    """
    if not personas_dir.is_dir():
        raise PersonaLoadError(f"{personas_dir}: not a directory")

    personas: dict[str, Persona] = {}
    for path in sorted(personas_dir.glob("*.md")):
        persona = load_persona(path)
        if persona.name in personas:
            raise PersonaLoadError(
                f"{path}: duplicate persona name '{persona.name}' "
                f"(also defined in {personas[persona.name].source_path})"
            )
        personas[persona.name] = persona
    return personas


def _coerce_str_list(value: Any, path: Path, field: str) -> list[str]:
    """Accept a list of strings or an empty list; reject anything else."""
    if value is None:
        return []
    if not isinstance(value, list):
        raise PersonaLoadError(f"{path}: '{field}' must be a list of strings")
    for item in value:
        if not isinstance(item, str):
            raise PersonaLoadError(
                f"{path}: '{field}' must contain only strings, got {type(item).__name__}"
            )
    return list(value)
