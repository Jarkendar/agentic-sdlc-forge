"""Architecture interview — terminal Q&A driver for `forge init`.

Stage 8. Walks the developer through five sections of questions (the
ones documented in `.forge/architecture_map.md`) and collects answers
into a dict that the architect agent will synthesize.

Design points worth knowing:

- **No editor invocation in MVP.** Every answer is collected through
  `input()`. Multi-line answers are not directly supported; for long
  module-tree dumps, users can paste through their terminal (most
  modern terminals handle multi-line paste as one input call) or
  describe the layout in a single line. Stage 9 adds `$EDITOR` support.

- **Single picker, single question.** Only 3.1 (architectural pattern)
  uses a multi-select picker; everything else is free-text. Multi-pickers
  for other questions (platform, build system, DB) live in Stage 9.

- **Empty answers are OK.** The picker is the one exception — at least
  one option must be picked, or "Other" with a non-empty free-text
  reply. Other empty answers fall through to the architect, which
  is contracted to omit subsections with no input.

- **No persistence on Ctrl+C.** MVP-level simplicity: abort means abort.
  The user re-runs `forge init` to start over. Stage 9 adds
  `--resume-interview` and a draft file.

- **File-tree hint before 3.2.** We snapshot the repo via
  `forge.file_tree.build_file_tree` and show a truncated version
  (40 lines max) right before asking how directories are organized.
  Users see what already exists and don't have to retype it.

- **I/O is injectable.** The `Interview` class takes `input_fn`,
  `output_fn`, and `file_tree_fn` arguments. Defaults wire to the
  real terminal; tests pass fakes that drive the flow without a TTY.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from forge.file_tree import build_file_tree

# ---------------------------------------------------------------------------
# Question definitions
# ---------------------------------------------------------------------------
#
# Mirror of `.forge/architecture_map.md` SECTIONS 1-5. Keeping the
# canonical text here (rather than parsing the markdown file at runtime)
# makes the interview standalone — `forge init` works even if the user
# nukes their architecture_map.md or runs on a fresh checkout.
#
# Each Question carries:
#   - id: stable identifier the architect prompt sees (e.g. "1.1")
#   - prompt: the question shown to the user
#   - hint: optional extra line shown below the prompt
#
# Order matters: the dict is rendered in declaration order.


@dataclass(frozen=True)
class Question:
    """One interview question."""

    id: str
    prompt: str
    hint: str = ""


# The architectural-pattern picker. Options are mutually compatible
# (combinations are valid — Clean Architecture + MVI is common). User
# can pick multiple or fall back to "Other" with free-text. See
# `_ask_picker_3_1` for the parsing logic.
_PATTERN_OPTIONS: tuple[str, ...] = (
    "MVI",
    "MVVM",
    "MVP",
    "Clean Architecture",
    "Hexagonal",
    "Layered",
    "Onion",
    "Modular Monolith",
    "Microservices",
    "Pipeline / DAG",
    "Other",
)


_QUESTIONS: tuple[Question, ...] = (
    # Section 1 — Project Identity
    Question("1.1", "What is the project name?"),
    Question(
        "1.2",
        "Describe the project in 1-2 sentences — what does it do and what problem does it solve?",
    ),
    Question("1.3", "Who are the target users? (developers, end-users, both?)"),
    # Section 2 — Technology Stack
    Question(
        "2.1",
        "What platform is the project for?",
        hint="(Android / iOS / KMP / Backend JVM / Python / Web / other)",
    ),
    Question("2.2", "Primary programming language and version?"),
    Question(
        "2.3",
        "Build system?",
        hint="(Gradle / Maven / pip / uv / npm / pnpm / other)",
    ),
    Question(
        "2.4",
        "List the key libraries/frameworks that MUST be used.",
        hint="(e.g. Compose, Ktor, Koin, Room, Coroutines — comma-separated or one per line if your terminal supports it)",
    ),
    Question(
        "2.5",
        "Technology anti-patterns — what must NEVER be used?",
        hint="(e.g. XML layouts, RxJava, wildcard imports, inheritance for sharing UI logic)",
    ),
    # Section 3 — Architecture & Responsibilities
    # 3.1 is handled by the picker, not as a Question (different flow).
    Question(
        "3.2",
        "How are directories/modules organized? Describe the structure or paste a tree.",
    ),
    Question(
        "3.3",
        "How does data flow through the application?",
        hint="(e.g. UI -> ViewModel -> UseCase -> Repository -> DataSource)",
    ),
    Question(
        "3.4",
        "Who manages application state?",
        hint="(e.g. ViewModel with StateFlow, Redux store, MVI reducer — provide concrete class/pattern names)",
    ),
    Question(
        "3.5",
        "Where does validation logic live?",
        hint="(e.g. domain layer, dedicated Validator classes, in UI, in use cases)",
    ),
    Question(
        "3.6",
        "How does Dependency Injection work?",
        hint="(e.g. Koin modules, Dagger/Hilt, manual DI, service locator)",
    ),
    Question(
        "3.7",
        "How are errors handled?",
        hint="(e.g. Result wrapper, sealed class hierarchy, exceptions, Either)",
    ),
    Question(
        "3.8",
        "Are there communication rules between modules/layers?",
        hint="(e.g. 'presentation must not import data', 'domain has no framework dependencies')",
    ),
    # Section 4 — System Boundaries & Integrations
    Question(
        "4.1",
        "Does the project communicate with an external API? If so, what kind and where is the contract?",
        hint="(REST / GraphQL / gRPC / WebSocket; OpenAPI spec, proto files)",
    ),
    Question(
        "4.2",
        "Does the project use a local database? If so — which one?",
        hint="(Room / SQLDelight / Realm / raw SQLite / Postgres / other — leave blank if none)",
    ),
    Question(
        "4.3",
        "Does the project integrate with third-party SDKs?",
        hint="(e.g. Firebase, Analytics, Payment SDK, Maps — list them, or leave blank if none)",
    ),
    Question(
        "4.4",
        "Does the project have background processing?",
        hint="(WorkManager / Cron jobs / message queue / other — leave blank if none)",
    ),
    Question(
        "4.5",
        "Are there other systems/services the project communicates with?",
        hint="(e.g. message broker, shared preferences/datastore, file system, Bluetooth)",
    ),
    # Section 5 — Known Constraints & Conscious Trade-offs
    Question(
        "5.1",
        "Is there any known tech debt that AI agents should be aware of?",
        hint="(e.g. 'module X is legacy and awaiting refactor — do not extend')",
    ),
    Question(
        "5.2",
        "Are there any conscious architectural trade-offs?",
        hint="(e.g. 'we know the singleton DB is an anti-pattern but it stays until v2')",
    ),
    Question(
        "5.3",
        "Anything else an AI agent should know before touching the code?",
    ),
)


#: How many lines of the file tree to show as a hint before 3.2. Past this
#: the listing becomes noise, and the user is unlikely to want the AI
#: agent to know about every file anyway.
_FILE_TREE_HINT_MAX_LINES = 40


# ---------------------------------------------------------------------------
# Section headers — printed between question groups so the user sees
# progress and knows where they are.
# ---------------------------------------------------------------------------

_SECTION_HEADERS: dict[str, str] = {
    "1": "SECTION 1 — Project Identity",
    "2": "SECTION 2 — Technology Stack",
    "3": "SECTION 3 — Architecture & Responsibilities",
    "4": "SECTION 4 — System Boundaries & Integrations",
    "5": "SECTION 5 — Known Constraints & Conscious Trade-offs",
}


# ---------------------------------------------------------------------------
# Picker for 3.1
# ---------------------------------------------------------------------------


@dataclass
class _PickerResult:
    """Result of the architectural-pattern picker.

    `text` is the rendered answer string the architect agent receives.
    Format: "Clean Architecture, MVI" or "Other: <user free text>".
    """

    text: str


def _ask_picker_3_1(
    output: Callable[[str], None],
    ask: Callable[[str], str],
) -> _PickerResult:
    """Multi-select picker for question 3.1 (architectural pattern).

    Accepted input:
        - Comma-separated numbers: "1,3,4"
        - Comma-separated names: "MVI, Clean Architecture"
        - Mixed: "1, Hexagonal" — fine, we parse each token
        - "Other" → follow-up free-text prompt
        - Empty: re-asked once with a hint; if still empty, fall through
          to "Other" with empty value (architect omits the subsection)

    Why not single-select? Because real codebases combine patterns (Clean
    Architecture + MVI is the canonical Android example). Forcing a
    single pick would either lose information or push everyone to "Other".
    """
    output("")
    output("3.1 What architectural pattern does the project follow?")
    output("    (Combinations are fine — pick all that apply.)")
    output("")
    for i, opt in enumerate(_PATTERN_OPTIONS, start=1):
        output(f"  {i:2d}. {opt}")
    output("")

    raw = ask(
        "Enter numbers or names, comma-separated (e.g. '1,4' or 'MVI, Clean Architecture'): "
    ).strip()

    picks = _parse_picks(raw)

    if "Other" in picks:
        free = ask("Describe your architecture (free text): ").strip()
        # Remove "Other" from the structured list — we replace it with
        # the prose label so the architect sees something meaningful.
        rest = [p for p in picks if p != "Other"]
        if free:
            if rest:
                return _PickerResult(text=f"{', '.join(rest)}; Other: {free}")
            return _PickerResult(text=f"Other: {free}")
        if rest:
            return _PickerResult(text=", ".join(rest))
        # User picked Other and gave no text — explicit non-answer.
        return _PickerResult(text="")

    if not picks:
        # Empty pick is OK — architect contract handles missing answers.
        return _PickerResult(text="")

    return _PickerResult(text=", ".join(picks))


def _parse_picks(raw: str) -> list[str]:
    """Turn the picker's free-form input into a list of canonical option names.

    Tokens that match a number get mapped to _PATTERN_OPTIONS[n-1]. Tokens
    that match an option name (case-insensitive) get the canonical name.
    Anything else is silently dropped — the picker is not the place to
    surface typos, the user can rerun init if they messed up. We dedupe
    while preserving first-seen order.
    """
    seen: list[str] = []
    seen_set: set[str] = set()

    for token in raw.split(","):
        token = token.strip()
        if not token:
            continue
        canonical = _canonicalize_pick(token)
        if canonical is None:
            continue
        if canonical not in seen_set:
            seen.append(canonical)
            seen_set.add(canonical)
    return seen


def _canonicalize_pick(token: str) -> str | None:
    """Resolve a single token to a canonical _PATTERN_OPTIONS entry."""
    # Numeric reference
    if token.isdigit():
        idx = int(token) - 1
        if 0 <= idx < len(_PATTERN_OPTIONS):
            return _PATTERN_OPTIONS[idx]
        return None

    # Case-insensitive name match
    lowered = token.lower()
    for opt in _PATTERN_OPTIONS:
        if opt.lower() == lowered:
            return opt
    return None


# ---------------------------------------------------------------------------
# Interview driver
# ---------------------------------------------------------------------------


@dataclass
class InterviewResult:
    """Outcome of a completed interview, ready to feed to the architect.

    `project_name` is hoisted from answers["1.1"] for the architect's
    title field; we keep it in `answers` too so the synthesis sees the
    full Q&A as one block.
    """

    project_name: str
    answers: dict[str, str] = field(default_factory=dict)


class Interview:
    """Drives the terminal Q&A.

    Constructor args are I/O dependencies — defaults wire to the real
    terminal, tests replace them with fakes. This keeps the module
    importable in any environment (no `input()` at import time) and
    keeps tests fully deterministic.

    Attributes:
        ask: Function that returns a single line of input given a prompt.
            Default: `input`.
        output: Function that prints a line. Default: `print`.
        file_tree_fn: Function that returns the repo file tree string
            given the repo root. Default: `build_file_tree`. Injected
            so tests can return a fixed tree without touching disk.
    """

    def __init__(
        self,
        *,
        ask: Callable[[str], str] | None = None,
        output: Callable[[str], None] | None = None,
        file_tree_fn: Callable[[Path], str] | None = None,
    ) -> None:
        self.ask = ask or input
        self.output = output or print
        self.file_tree_fn = file_tree_fn or build_file_tree

    def run(self, *, repo: Path, default_project_name: str) -> InterviewResult:
        """Run the full interview against the given repo root.

        Args:
            repo: Path used to build the file-tree hint before 3.2.
                Doesn't need to be a git repo; `build_file_tree` falls
                back to a pathlib walk when .git/ is absent.
            default_project_name: Suggested project name for 1.1, shown
                in the prompt. Falls back to repo dir name if empty.
                If the user accepts the default by hitting enter, this
                value is recorded as the project name.

        Returns:
            An InterviewResult with answers indexed by question id.

        Raises:
            KeyboardInterrupt: Propagated up — the caller (cmd_init)
                handles the abort message. We don't trap it here because
                a half-finished interview is not a useful artifact in MVP.
        """
        answers: dict[str, str] = {}
        section_printed: set[str] = set()

        self.output("")
        self.output("=" * 60)
        self.output("Architecture interview — Forge `init`")
        self.output("=" * 60)
        self.output(
            "Answer each question in 1-3 sentences. Blank answers are OK "
            "(the corresponding subsection will be omitted from the output)."
        )
        self.output("Press Ctrl+C at any point to abort. Progress is NOT saved in MVP.")
        self.output("")

        # ---- 1.1: project name with default suggestion ----
        self._print_section_header("1", section_printed)
        suggested = default_project_name.strip() or "(none)"
        name_raw = self.ask(
            f"1.1 What is the project name? [default: {suggested}]\n  > "
        ).strip()
        answers["1.1"] = name_raw if name_raw else default_project_name.strip()

        # ---- The rest, by walking _QUESTIONS in order ----
        for q in _QUESTIONS:
            if q.id == "1.1":
                # Already handled above with a default prompt.
                continue

            section = q.id.split(".", 1)[0]
            self._print_section_header(section, section_printed)

            # 3.2 gets the file-tree hint right before it. The picker
            # for 3.1 is inserted at the start of section 3 (below).
            if q.id == "3.2":
                self._print_file_tree_hint(repo)

            answers[q.id] = self._ask_one(q)

            # After 2.5 (end of section 2), prompt for 3.1 BEFORE moving
            # on to 3.2. This is the one place where the picker breaks
            # the natural _QUESTIONS sequence — see _PATTERN_OPTIONS for
            # the rationale on a custom picker for this question.
            if q.id == "2.5":
                self._print_section_header("3", section_printed)
                picker_result = _ask_picker_3_1(self.output, self.ask)
                answers["3.1"] = picker_result.text

        project_name = answers.get("1.1", "").strip() or default_project_name.strip()
        return InterviewResult(project_name=project_name, answers=answers)

    # -------------------------------------------------------------------
    # Helpers
    # -------------------------------------------------------------------

    def _print_section_header(self, section: str, already: set[str]) -> None:
        if section in already:
            return
        already.add(section)
        header = _SECTION_HEADERS.get(section, f"SECTION {section}")
        self.output("")
        self.output("-" * 60)
        self.output(header)
        self.output("-" * 60)

    def _ask_one(self, q: Question) -> str:
        """Single-question prompt, free-text reply.

        Format on screen:
            3.3 How does data flow ...
                (hint, if any)
              >
        """
        self.output("")
        self.output(f"{q.id} {q.prompt}")
        if q.hint:
            self.output(f"    {q.hint}")
        reply = self.ask("  > ")
        return reply.strip()

    def _print_file_tree_hint(self, repo: Path) -> None:
        """Show the user a truncated view of their repo's file layout.

        We don't pre-fill the 3.2 answer — describing the layout in
        the user's own words is more useful than a raw tree dump.
        But seeing what's there saves the user from retyping or
        remembering everything.
        """
        try:
            tree = self.file_tree_fn(repo)
        except FileNotFoundError:
            # build_file_tree raises this if the repo path doesn't exist.
            # Don't crash the interview over a hint — just skip it.
            return

        if not tree.strip():
            return

        lines = tree.splitlines()
        truncated = lines[:_FILE_TREE_HINT_MAX_LINES]
        more = len(lines) - len(truncated)

        self.output("")
        self.output("    (Detected file tree — for reference only, not pre-filled:)")
        for line in truncated:
            self.output(f"    | {line}")
        if more > 0:
            self.output(f"    | ... ({more} more)")
