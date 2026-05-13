"""Architect agent — synthesizes interview answers into architecture.md.

Stage 8. Called once by `forge init` after the interview completes,
producing the structured architecture document the Planner will read
on every subsequent run.

Architect is one of two free-text personas (the other is the Reporter).
It does NOT return a Pydantic schema — its output is markdown that
goes straight to `.forge/knowledge/architecture.md`.

Design notes:

- Architect is NOT in `PersonaName` (config.py). It's an on-demand
  persona used only by `forge init`, when `config.toml` may not exist
  yet (the whole point of init is to scaffold it). Its LLM client is
  constructed directly by `cmd_init`, not through `forge.llm.factory`.

- Output validation is light: we check the response contains the
  required section headers ("## 1. Project Identity" ... "## 5. Known
  Constraints"). One retry on validation failure with the missing
  headers appended to the prompt. After that, hard error — better
  than silently writing a malformed doc the Planner will choke on.

- The persona file lives in the SCAFFOLDED `.forge/personas/architect.md`
  (i.e. the user's project), not in some Forge-internal location. This
  means users can edit it like any other persona — same pattern, same
  surface. `forge init` copies it into place before calling us.
"""

from __future__ import annotations

import re
from datetime import date

from forge.event_log import EventLog
from forge.llm.base import LLMClient
from forge.personas import Persona

# Section headers the architect output MUST contain. These mirror the
# template structure inside the architect persona's body. If any are
# missing, we retry once; if still missing after retry, hard error.
#
# Match is "## N." rather than full header text so the LLM has some
# wiggle room on the trailing words ("## 1. Project Identity" vs
# "## 1. Project Identity & Scope"). The numeric prefix is the contract.
_REQUIRED_SECTION_RE = [
    re.compile(r"^##\s+1\.\s", re.MULTILINE),
    re.compile(r"^##\s+2\.\s", re.MULTILINE),
    re.compile(r"^##\s+3\.\s", re.MULTILINE),
    re.compile(r"^##\s+4\.\s", re.MULTILINE),
    re.compile(r"^##\s+5\.\s", re.MULTILINE),
]


class ArchitectError(Exception):
    """Raised when the architect persona is misconfigured or output
    is unrecoverable (e.g. missing required sections after one retry)."""


def run_architect(
    *,
    project_name: str,
    answers: dict[str, str],
    persona: Persona,
    llm: LLMClient,
    event_log: EventLog | None = None,
    run_id: str | None = None,
) -> str:
    """Synthesize interview answers into the architecture.md content.

    Args:
        project_name: The project's name, used in the document title.
            Falls back to "Untitled Project" if empty after strip.
        answers: Mapping from question ID (e.g. "1.1", "3.2") to the
            developer's free-text answer. Empty answers are dropped
            before being sent to the model — they're explicitly OK
            per the picker rules.
        persona: Loaded architect persona. Must have output_schema=None.
        llm: LLMClient bound to a strong model (Opus/Sonnet class).
            Constructed by `cmd_init` rather than the factory, since
            `forge init` runs before config.toml exists.
        event_log: Optional EventLog. When init runs in a fresh project
            we may not have one yet — Architect still works, we just
            skip the logging. Passed in when the caller has set one up.
        run_id: Pseudo-run-id for the init invocation. Required iff
            event_log is given. The string convention is
            "init-YYYYMMDD-HHMMSS" (caller's responsibility to format).

    Returns:
        The markdown content of architecture.md, ready to write to disk.

    Raises:
        ArchitectError: If persona is misconfigured, or if the LLM
            output is missing required sections after one retry.
    """
    if persona.output_schema is not None:
        raise ArchitectError(
            f"Architect persona must declare output_schema=null, "
            f"got {persona.output_schema!r}. Check {persona.source_path}."
        )

    if event_log is not None and run_id is None:
        raise ArchitectError(
            "run_architect: event_log provided without run_id. "
            "Both or neither — partial logging is worse than none."
        )

    project_name = project_name.strip() or "Untitled Project"
    raw_answers = _format_answers(answers)
    generation_date = date.today().isoformat()

    system_prompt = persona.render(
        project_name=project_name,
        generation_date=generation_date,
        raw_answers=raw_answers,
    )

    if event_log is not None:
        event_log.log(
            agent="architect",
            phase="start",
            run_id=run_id,
            payload={
                "project_name": project_name,
                "n_answers": sum(1 for v in answers.values() if v.strip()),
            },
        )

    # ---- First attempt ----
    response = llm.complete(
        system=system_prompt,
        user="Generate the architecture document now, per the structure above.",
        schema=None,
    )

    if not isinstance(response.content, str):
        raise ArchitectError(
            f"Architect LLM returned non-string content of type "
            f"{type(response.content).__name__}. Provider regression?"
        )

    if event_log is not None:
        event_log.log(
            agent="architect",
            phase="llm_call_complete",
            run_id=run_id,
            tokens_in=response.tokens_in,
            tokens_out=response.tokens_out,
            cost_usd=response.cost_usd,
            duration_ms=response.duration_ms,
            payload={
                "model": response.model,
                "provider": response.provider,
                "finish_reason": response.finish_reason,
                "attempt": 1,
            },
        )

    missing = _missing_sections(response.content)
    if not missing:
        return response.content

    # ---- One retry, with explicit hint about what's missing ----
    if event_log is not None:
        event_log.log(
            agent="architect",
            phase="output_invalid",
            run_id=run_id,
            payload={
                "missing_sections": missing,
                "attempt": 1,
            },
        )

    retry_user = (
        "Generate the architecture document now. Your previous attempt was "
        f"missing the following required section header(s): {missing}. "
        "Output the FULL document, in the exact structure shown above, "
        "including all five top-level sections (## 1. through ## 5.)."
    )
    response2 = llm.complete(
        system=system_prompt,
        user=retry_user,
        schema=None,
    )

    if not isinstance(response2.content, str):
        raise ArchitectError(
            f"Architect LLM retry returned non-string content of type "
            f"{type(response2.content).__name__}."
        )

    if event_log is not None:
        event_log.log(
            agent="architect",
            phase="llm_call_complete",
            run_id=run_id,
            tokens_in=response2.tokens_in,
            tokens_out=response2.tokens_out,
            cost_usd=response2.cost_usd,
            duration_ms=response2.duration_ms,
            payload={
                "model": response2.model,
                "provider": response2.provider,
                "finish_reason": response2.finish_reason,
                "attempt": 2,
            },
        )

    missing2 = _missing_sections(response2.content)
    if missing2:
        raise ArchitectError(
            f"Architect output is missing required section header(s) "
            f"{missing2} after one retry. Output discarded; rerun "
            f"`forge init` after checking the architect persona file."
        )

    return response2.content


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _format_answers(answers: dict[str, str]) -> str:
    """Render the answers dict into the prompt-ready block.

    Empty answers are dropped — the persona contract says "omit
    subsection entirely" for missing inputs, so feeding the model a
    bunch of blank lines just invites it to invent content.

    Format:
        [1.1] What is the project name?
        Answer: My Cool App

        [1.2] Describe the project ...
        Answer: ...

    The question prompts themselves are owned by `forge.interview`,
    not here — Architect only sees the question id and the answer.
    Keeps the agent decoupled from the interview UI; if we rewrite
    the questionnaire, we don't have to touch the agent.
    """
    if not answers:
        return "(no answers provided)"

    parts: list[str] = []
    for qid in sorted(answers.keys(), key=_sort_key):
        value = answers[qid].strip()
        if not value:
            continue
        parts.append(f"[{qid}]\n{value}\n")
    return "\n".join(parts) if parts else "(no non-empty answers)"


def _sort_key(qid: str) -> tuple[int, int]:
    """Sort question IDs numerically: '1.10' after '1.2', not before.

    String sort puts '1.10' before '1.2' which scrambles the document
    structure. Split on the dot and compare the parts as ints.
    """
    try:
        major, minor = qid.split(".", 1)
        return (int(major), int(minor))
    except (ValueError, AttributeError):
        # Unknown id format — sort to the end, stable order among them.
        return (10**9, 0)


def _missing_sections(content: str) -> list[str]:
    """Return the list of required section markers absent from `content`.

    We check for the numbered header prefix only — see _REQUIRED_SECTION_RE
    for why. Returned labels match the marker, so they can be quoted
    back to the LLM in the retry prompt without further translation.
    """
    missing: list[str] = []
    for n, pattern in enumerate(_REQUIRED_SECTION_RE, start=1):
        if not pattern.search(content):
            missing.append(f"## {n}.")
    return missing
