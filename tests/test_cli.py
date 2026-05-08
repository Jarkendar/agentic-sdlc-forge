"""Tests for forge.cli.

We drive `cmd_plan` directly (not via subprocess) so we can monkeypatch
`get_client` to return a FakeLLMClient. This keeps tests fast and offline.

Argument parsing and the handler are tested separately:
- `build_parser` smoke-tested for required args.
- `cmd_plan` end-to-end with all dependencies stubbed.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest
from pydantic import BaseModel

from forge import cli
from forge.llm.base import LLMClient, LLMResponse
from forge.schemas import Plan, Task

# ---------------------------------------------------------------------------
# Local fakes — duplicated from tests/test_planner.py rather than imported.
# Cross-test-module imports require either tests/__init__.py (turns tests
# into a package, has its own side effects) or sys.path tweaks. The
# duplication is small and the coupling cost is lower than the alternatives.
# ---------------------------------------------------------------------------


class FakeLLMClient(LLMClient):
    """Returns whatever LLMResponse the test injects."""

    provider = "fake"

    def __init__(self, response: LLMResponse) -> None:
        self._response = response
        self.calls: list[dict[str, object]] = []

    def complete(
        self,
        *,
        system: str,
        user: str,
        schema: type[BaseModel] | None = None,
    ) -> LLMResponse:
        self.calls.append({"system": system, "user": user, "schema": schema})
        return self._response


def _make_response(content: BaseModel | str, **overrides: object) -> LLMResponse:
    defaults: dict[str, object] = {
        "content": content,
        "tokens_in": 100,
        "tokens_out": 200,
        "cost_usd": 0.001,
        "duration_ms": 500,
        "model": "fake-model",
        "provider": "fake",
        "finish_reason": "end_turn",
        "retried_validation": False,
    }
    defaults.update(overrides)
    return LLMResponse(**defaults)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_minimal_repo(repo: Path) -> None:
    """Set up the bare minimum for `cmd_plan` to run successfully.

    Layout:
        repo/
          .forge/
            config.toml
            personas/<all five>.md
            knowledge/architecture.md
          .env  (so validate_credentials passes)
          src/forge/...  (the project being planned, irrelevant content)
    """
    forge = repo / ".forge"
    (forge / "personas").mkdir(parents=True)
    (forge / "knowledge").mkdir(parents=True)

    # Architecture map — anything non-empty
    (forge / "knowledge" / "architecture.md").write_text("# arch\nFastAPI app.")

    # Config — point all personas at Ollama (no API key required)
    (forge / "config.toml").write_text(
        "[models.orchestrator]\n"
        'provider = "ollama"\n'
        'model = "llama3.1:8b"\n'
        "\n"
        "[models.planner]\n"
        'provider = "ollama"\n'
        'model = "llama3.1:8b"\n'
        "\n"
        "[models.executor]\n"
        'provider = "ollama"\n'
        'model = "llama3.1:8b"\n'
        "\n"
        "[models.verifier]\n"
        'provider = "ollama"\n'
        'model = "llama3.1:8b"\n'
        "\n"
        "[models.reporter]\n"
        'provider = "ollama"\n'
        'model = "llama3.1:8b"\n'
    )

    # Five persona files. Only `planner` matters for `cmd_plan`, but
    # `load_all_personas` reads them all.
    for name in ("orchestrator", "executor", "verifier", "reporter"):
        (forge / "personas" / f"{name}.md").write_text(
            "---\n"
            f"name: {name}\n"
            "output_schema: null\n"
            "required_vars: []\n"
            "references: []\n"
            "---\n"
            f"# {name}\nNo body content.\n"
        )

    (forge / "personas" / "planner.md").write_text(
        "---\n"
        "name: planner\n"
        "output_schema: Plan\n"
        "required_vars:\n"
        "  - user_story\n"
        "  - run_id\n"
        "  - architecture_map\n"
        "  - file_tree\n"
        "references: []\n"
        "---\n"
        "# Planner\n"
        "User story: {{user_story}}\n"
        "Run ID: {{run_id}}\n"
        "Arch: {{architecture_map}}\n"
        "Tree: {{file_tree}}\n"
    )


def _good_plan_for_run(run_id: str) -> Plan:
    return Plan(
        run_id=run_id,
        user_story="story",
        tasks=[
            Task(
                id="task-001",
                goal="Do the thing.",
                files=[Path("src/x.py")],
                acceptance_criteria=["thing is done"],
            )
        ],
    )


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def test_parser_requires_user_story() -> None:
    parser = cli.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["plan"])  # missing user_story


def test_parser_accepts_minimal_args() -> None:
    parser = cli.build_parser()
    args = parser.parse_args(["plan", "do something"])
    assert args.user_story == "do something"
    assert args.repo == Path(".")
    assert args.config is None  # resolved later in cmd_plan


# ---------------------------------------------------------------------------
# cmd_plan happy path
# ---------------------------------------------------------------------------


def test_cmd_plan_writes_json_to_stdout(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_minimal_repo(tmp_path)

    # Patch get_client where cli imports it (forge.cli.get_client).
    # We capture the run_id the planner echoes back by patching after parse,
    # using a plan factory that reads the actual run_id from the system prompt.
    captured: dict[str, str] = {}

    def fake_get_client(persona, config):  # type: ignore[no-untyped-def]
        # We need the run_id at LLM-call time. The Planner interpolates it
        # into the system prompt; we extract it there.
        class _Client(FakeLLMClient):
            def complete(
                self, *, system: str, user: str, schema=None
            ) -> LLMResponse:
                # Extract run_id from "Run ID: <id>" line
                for line in system.splitlines():
                    if line.startswith("Run ID:"):
                        captured["run_id"] = line.split(":", 1)[1].strip()
                        break
                return _make_response(_good_plan_for_run(captured["run_id"]))

        return _Client(_make_response(_good_plan_for_run("placeholder")))

    monkeypatch.setattr(cli, "get_client", fake_get_client)

    args = argparse.Namespace(
        user_story="story",
        config=None,
        repo=tmp_path,
        architecture=None,
        out=None,
    )
    rc = cli.cmd_plan(args)
    assert rc == 0

    out = capsys.readouterr()
    parsed = json.loads(out.out)
    assert parsed["run_id"] == captured["run_id"]
    assert parsed["user_story"] == "story"
    assert len(parsed["tasks"]) == 1
    # Summary went to stderr
    assert "Plan for run" in out.err


def test_cmd_plan_writes_to_out_file_when_given(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_minimal_repo(tmp_path)

    out_path = tmp_path / "plan.json"

    def fake_get_client(persona, config):  # type: ignore[no-untyped-def]
        class _Client(FakeLLMClient):
            def complete(self, *, system: str, user: str, schema=None) -> LLMResponse:
                run_id = next(
                    line.split(":", 1)[1].strip()
                    for line in system.splitlines()
                    if line.startswith("Run ID:")
                )
                return _make_response(_good_plan_for_run(run_id))

        return _Client(_make_response(_good_plan_for_run("x")))

    monkeypatch.setattr(cli, "get_client", fake_get_client)

    args = argparse.Namespace(
        user_story="story",
        config=None,
        repo=tmp_path,
        architecture=None,
        out=out_path,
    )
    rc = cli.cmd_plan(args)
    assert rc == 0

    assert out_path.exists()
    parsed = json.loads(out_path.read_text())
    assert parsed["user_story"] == "story"

    # Stdout should NOT contain JSON when --out is given
    out = capsys.readouterr()
    assert "tasks" not in out.out


# ---------------------------------------------------------------------------
# Failure paths — fail fast with clear messages
# ---------------------------------------------------------------------------


def test_cmd_plan_missing_architecture_returns_error(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_minimal_repo(tmp_path)
    (tmp_path / ".forge" / "knowledge" / "architecture.md").unlink()

    args = argparse.Namespace(
        user_story="story",
        config=None,
        repo=tmp_path,
        architecture=None,
        out=None,
    )
    rc = cli.cmd_plan(args)
    assert rc == 1
    err = capsys.readouterr().err
    assert "architecture map not found" in err
    assert "forge init" in err


def test_cmd_plan_missing_config_returns_error(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_minimal_repo(tmp_path)
    (tmp_path / ".forge" / "config.toml").unlink()

    args = argparse.Namespace(
        user_story="story",
        config=None,
        repo=tmp_path,
        architecture=None,
        out=None,
    )
    rc = cli.cmd_plan(args)
    assert rc == 1
    err = capsys.readouterr().err
    assert "Config file not found" in err