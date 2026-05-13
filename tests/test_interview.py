"""Tests for forge.interview.

Drives the Interview class via injected ask/output/file_tree_fn fakes —
no real TTY. The picker (3.1) has its own focused tests since it's the
one part with non-trivial parsing.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from forge.interview import (
    Interview,
    _ask_picker_3_1,
    _parse_picks,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _scripted_ask(replies: list[str]):
    """Build an ask() that returns the next reply on each call.

    Raises StopIteration if the interview asks more questions than the
    script has answers for — turns "test forgot a reply" into a loud
    failure instead of a hang.
    """
    it: Iterator[str] = iter(replies)

    def ask(_prompt: str) -> str:
        return next(it)

    return ask


# A canonical 23-answer script that fills every question. Question 3.1
# is the picker, so its reply is in picker format ("1,4" -> MVI + Clean).
_FULL_SCRIPT = [
    "My Cool App",            # 1.1
    "Notes app.",             # 1.2
    "End-users",              # 1.3
    "Android",                # 2.1
    "Kotlin 2.0",             # 2.2
    "Gradle",                 # 2.3
    "Compose, Koin",          # 2.4
    "XML layouts",            # 2.5
    "1,4",                    # 3.1 picker -> MVI + Clean Architecture
    "feature modules",        # 3.2
    "UI -> VM -> UseCase",    # 3.3
    "StateFlow in VM",        # 3.4
    "Validator classes",      # 3.5
    "Koin",                   # 3.6
    "sealed class",           # 3.7
    "no presentation->data",  # 3.8
    "REST OpenAPI",           # 4.1
    "Room",                   # 4.2
    "Firebase",               # 4.3
    "",                       # 4.4 empty
    "",                       # 4.5 empty
    "",                       # 5.1 empty
    "",                       # 5.2 empty
    "",                       # 5.3 empty
]


# ---------------------------------------------------------------------------
# Full-flow tests
# ---------------------------------------------------------------------------


def test_run_collects_all_answers(tmp_path: Path) -> None:
    """Happy path: full script returns the expected answers dict."""
    output: list[str] = []
    iv = Interview(
        ask=_scripted_ask(_FULL_SCRIPT),
        output=output.append,
        file_tree_fn=lambda _: "src/foo.py\nsrc/bar.py\n",
    )
    result = iv.run(repo=tmp_path, default_project_name="default-name")

    assert result.project_name == "My Cool App"
    assert result.answers["1.1"] == "My Cool App"
    assert result.answers["2.1"] == "Android"
    assert result.answers["3.1"] == "MVI, Clean Architecture"
    assert result.answers["3.2"] == "feature modules"
    # Trailing empties are preserved as keys with "" values
    assert result.answers["5.3"] == ""


def test_project_name_default_used_when_blank(tmp_path: Path) -> None:
    """User hits enter at 1.1 → falls back to default_project_name."""
    script = [""] + _FULL_SCRIPT[1:]
    iv = Interview(
        ask=_scripted_ask(script),
        output=lambda _l: None,
        file_tree_fn=lambda _: "",
    )
    result = iv.run(repo=tmp_path, default_project_name="my-folder")
    assert result.project_name == "my-folder"
    assert result.answers["1.1"] == "my-folder"


def test_section_headers_printed_once_each(tmp_path: Path) -> None:
    """Each SECTION header is printed exactly once during the run."""
    output: list[str] = []
    iv = Interview(
        ask=_scripted_ask(_FULL_SCRIPT),
        output=output.append,
        file_tree_fn=lambda _: "",
    )
    iv.run(repo=tmp_path, default_project_name="d")

    joined = "\n".join(output)
    for n in range(1, 6):
        assert joined.count(f"SECTION {n}") == 1, f"section {n} appeared wrong count"


def test_file_tree_hint_shown_before_3_2(tmp_path: Path) -> None:
    """File tree hint is rendered before question 3.2 is asked."""
    output: list[str] = []
    tree_text = "src/main.py\nsrc/helpers.py\n"
    iv = Interview(
        ask=_scripted_ask(_FULL_SCRIPT),
        output=output.append,
        file_tree_fn=lambda _: tree_text,
    )
    iv.run(repo=tmp_path, default_project_name="d")

    # Find the index of the line containing "3.2 How are directories"
    q32_idx = next(i for i, line in enumerate(output) if "3.2" in line and "directories" in line)
    # The tree hint marker appears in an earlier line
    earlier = "\n".join(output[:q32_idx])
    assert "Detected file tree" in earlier
    assert "src/main.py" in earlier


def test_file_tree_hint_skipped_when_empty(tmp_path: Path) -> None:
    """No files in repo → no 'Detected file tree' block."""
    output: list[str] = []
    iv = Interview(
        ask=_scripted_ask(_FULL_SCRIPT),
        output=output.append,
        file_tree_fn=lambda _: "   \n",
    )
    iv.run(repo=tmp_path, default_project_name="d")
    assert "Detected file tree" not in "\n".join(output)


def test_file_tree_hint_truncated(tmp_path: Path) -> None:
    """Trees longer than the cap show only the first N lines plus '... more'."""
    output: list[str] = []
    long_tree = "\n".join(f"file{i}.py" for i in range(100))
    iv = Interview(
        ask=_scripted_ask(_FULL_SCRIPT),
        output=output.append,
        file_tree_fn=lambda _: long_tree,
    )
    iv.run(repo=tmp_path, default_project_name="d")

    joined = "\n".join(output)
    # file0..file39 should appear (cap = 40); file40+ should not (some lines, anyway)
    assert "file0.py" in joined
    assert "file39.py" in joined
    assert "file50.py" not in joined
    assert "more)" in joined  # "... (N more)" line


def test_empty_answers_preserved_as_empty_strings(tmp_path: Path) -> None:
    """Skipped answers stay in the dict with '' so architect can omit them."""
    iv = Interview(
        ask=_scripted_ask(_FULL_SCRIPT),
        output=lambda _l: None,
        file_tree_fn=lambda _: "",
    )
    result = iv.run(repo=tmp_path, default_project_name="d")

    for qid in ("4.4", "4.5", "5.1", "5.2", "5.3"):
        assert qid in result.answers
        assert result.answers[qid] == ""


def test_picker_for_3_1_invoked_after_section_2(tmp_path: Path) -> None:
    """The picker UI strings (numbered options) appear before 3.2."""
    output: list[str] = []
    iv = Interview(
        ask=_scripted_ask(_FULL_SCRIPT),
        output=output.append,
        file_tree_fn=lambda _: "",
    )
    iv.run(repo=tmp_path, default_project_name="d")
    joined = "\n".join(output)

    # The picker prints all _PATTERN_OPTIONS as a numbered list
    assert "3.1 What architectural pattern" in joined
    assert "MVI" in joined
    assert "Clean Architecture" in joined
    assert "Other" in joined


# ---------------------------------------------------------------------------
# Picker unit tests
# ---------------------------------------------------------------------------


def _picker(replies: list[str]) -> str:
    """Run _ask_picker_3_1 with scripted input. Returns the .text field."""
    it = iter(replies)
    out: list[str] = []
    r = _ask_picker_3_1(out.append, lambda _: next(it))
    return r.text


def test_picker_single_number() -> None:
    assert _picker(["1"]) == "MVI"


def test_picker_multi_number() -> None:
    assert _picker(["1,4"]) == "MVI, Clean Architecture"


def test_picker_name_case_insensitive() -> None:
    assert _picker(["clean architecture, mvi"]) == "Clean Architecture, MVI"


def test_picker_mixed_numbers_and_names() -> None:
    assert _picker(["1, Hexagonal"]) == "MVI, Hexagonal"


def test_picker_other_with_free_text() -> None:
    assert _picker(["Other", "Event Sourcing + CQRS"]) == "Other: Event Sourcing + CQRS"


def test_picker_other_combined_with_picks() -> None:
    assert _picker(["1, Other", "saga + actors"]) == "MVI; Other: saga + actors"


def test_picker_other_with_blank_free_text() -> None:
    # User picked Other and then gave nothing — explicit non-answer
    assert _picker(["Other", ""]) == ""


def test_picker_empty_input_returns_empty() -> None:
    assert _picker([""]) == ""


def test_picker_drops_invalid_tokens() -> None:
    """'99' (out of range) and 'xyz' (unknown) are silently dropped."""
    assert _picker(["99, MVI, xyz"]) == "MVI"


def test_picker_dedupes() -> None:
    assert _picker(["MVI, 1, MVI"]) == "MVI"


def test_parse_picks_helper_directly() -> None:
    """_parse_picks is what the picker leans on for tokenization."""
    assert _parse_picks("") == []
    assert _parse_picks("  ,  ,") == []
    assert _parse_picks("1") == ["MVI"]
    assert _parse_picks("MVI,1") == ["MVI"]  # dedupe across forms
