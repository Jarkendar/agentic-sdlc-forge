"""Reporter agent — produces RUN_REPORT.md from the event log.

Stage 7. The Reporter is the only persona that produces free-text
output: a markdown document for human consumption, written to
`.forge/runs/<run_id>/RUN_REPORT.md`.

Design:
- Cost is pre-aggregated here, not by the LLM. The persona contract
  explicitly says "Cost numbers come verbatim from `cost_summary`. Do
  not recompute." We compute the table once, pass it in.
- The events JSONL is passed verbatim. We don't pre-filter or summarize
  — the persona is allowed to skim and we let it. Truncation only
  kicks in if the log gets very long (multi-MB), at which point we
  keep head + tail and tell the persona we did.
- The output is written verbatim. We do not parse, validate, or
  reformat. Reporter speaks markdown to humans, not JSON to agents.

Reporter is called once per run, at terminal status (DONE / ESCALATED
/ FAILED). Re-running it is supported via the future `forge report
<run_id>` standalone command (added as a CLI subcommand in this same
stage).
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path

from forge.event_log import Event, EventLog
from forge.llm.base import LLMClient
from forge.personas import Persona
from forge.state import events_path, run_dir

# When the events.jsonl exceeds this many bytes, we truncate the
# middle and tell the persona about the elision. 256 KiB is enough
# for ~1500 events at the current shape; well past anything we see
# in a typical run, but a hostile / debug-spam run could hit it.
_MAX_EVENTS_PAYLOAD_BYTES = 256 * 1024


class ReporterError(Exception):
    """Raised on hard preconditions (no events file, bad persona)."""


def run_reporter(
    *,
    run_id: str,
    user_story: str,
    forge_root: Path,
    persona: Persona,
    llm: LLMClient,
    event_log: EventLog,
) -> Path:
    """Run the Reporter and write RUN_REPORT.md.

    Args:
        run_id: The run to report on.
        user_story: Original user story — passed verbatim into the
            persona body. We could read it from the event log too, but
            taking it as an arg keeps Reporter simple and lets a CLI
            `forge report` invocation supply it independently of any
            log scraping.
        forge_root: The `.forge` directory. Used to resolve both the
            events file and the report output path.
        persona: Loaded Reporter persona. `output_schema` must be None
            (free-text markdown).
        llm: LLMClient bound to the reporter model.
        event_log: The OPEN event log for this run. We use it to log
            Reporter's own events; the markdown content comes from
            re-reading the events.jsonl file via `EventLog.read`
            (we can't reuse the open writer to read its own file).

    Returns:
        Path to the written RUN_REPORT.md.

    Raises:
        ReporterError: If the persona is misconfigured or the events
            file is missing.
    """
    # Persona sanity. Reporter is one of two personas with output_schema
    # = None (the other is yet-to-build Documentalist). Catching the
    # mismatch here saves a paid LLM call.
    if persona.output_schema is not None:
        raise ReporterError(
            f"Reporter persona must declare output_schema=null, "
            f"got {persona.output_schema!r}. Check {persona.source_path}."
        )

    log_path = events_path(forge_root, run_id)
    if not log_path.exists():
        raise ReporterError(
            f"Events file not found at {log_path}. "
            f"Reporter cannot run without a log."
        )

    # ---- Read events ----
    events = list(EventLog.read(log_path))

    event_log.log(
        agent="reporter",
        phase="start",
        run_id=run_id,
        payload={
            "events_read": len(events),
            "events_file": str(log_path),
        },
    )

    # ---- Aggregate cost per agent ----
    cost_summary_md = _format_cost_summary(events)

    # ---- Format events for the prompt ----
    events_payload = _format_events_for_prompt(log_path)

    # ---- Call LLM ----
    system_prompt = persona.render(
        run_id=run_id,
        user_story=user_story,
        events_jsonl=events_payload,
        cost_summary=cost_summary_md,
    )
    response = llm.complete(
        system=system_prompt,
        user="Generate the run report now, per the structure above.",
        schema=None,
    )

    event_log.log(
        agent="reporter",
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
        },
    )

    # When `schema=None` is passed, LLMClient returns response.content
    # as a string. Defence in depth: validate that here, instead of
    # crashing on `.write_text` with a confusing TypeError.
    if not isinstance(response.content, str):
        raise ReporterError(
            f"Reporter LLM returned non-string content of type "
            f"{type(response.content).__name__}. Provider regression?"
        )

    # ---- Write report ----
    out_path = run_dir(forge_root, run_id) / "RUN_REPORT.md"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(response.content, encoding="utf-8")

    event_log.log(
        agent="reporter",
        phase="report_written",
        run_id=run_id,
        payload={
            "report_path": str(out_path),
            "report_chars": len(response.content),
        },
    )
    return out_path


# ---------------------------------------------------------------------------
# Cost aggregation
# ---------------------------------------------------------------------------


def _format_cost_summary(events: list[Event]) -> str:
    """Aggregate tokens and cost per agent into a markdown table.

    Per the persona contract, we hand the LLM a pre-built table so it
    can't make up numbers. Format mirrors what `reporter.md` expects
    in its 'Cost' section (same column order), making the persona's
    job a copy-paste rather than a transformation.

    Events with None for any of tokens_in/tokens_out/cost_usd are
    ignored for the sum (deterministic/no-LLM events fall here). This
    means the totals reflect *paid* work only — exactly what the cost
    table should report.
    """
    totals_in: dict[str, int] = defaultdict(int)
    totals_out: dict[str, int] = defaultdict(int)
    totals_cost: dict[str, float] = defaultdict(float)

    for ev in events:
        if ev.tokens_in is None and ev.tokens_out is None and ev.cost_usd is None:
            continue
        if ev.tokens_in:
            totals_in[ev.agent] += ev.tokens_in
        if ev.tokens_out:
            totals_out[ev.agent] += ev.tokens_out
        if ev.cost_usd:
            totals_cost[ev.agent] += ev.cost_usd

    if not totals_cost and not totals_in and not totals_out:
        return "| Agent | Input tokens | Output tokens | Cost (USD) |\n|---|---|---|---|\n| (no LLM calls recorded) | 0 | 0 | 0.0000 |\n| **Total** | 0 | 0 | 0.0000 |\n"

    lines = [
        "| Agent | Input tokens | Output tokens | Cost (USD) |",
        "|---|---|---|---|",
    ]
    grand_in = 0
    grand_out = 0
    grand_cost = 0.0
    for agent in sorted(set(totals_in) | set(totals_out) | set(totals_cost)):
        ti = totals_in[agent]
        to = totals_out[agent]
        c = totals_cost[agent]
        grand_in += ti
        grand_out += to
        grand_cost += c
        lines.append(f"| {agent} | {ti} | {to} | {c:.4f} |")
    lines.append(f"| **Total** | {grand_in} | {grand_out} | {grand_cost:.4f} |")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Events-for-prompt formatting
# ---------------------------------------------------------------------------


def _format_events_for_prompt(log_path: Path) -> str:
    """Read the JSONL file as text and return it, truncating if huge.

    We pass the raw JSONL so the persona sees the canonical shape its
    contract refers to. Truncation strategy: if the file exceeds
    `_MAX_EVENTS_PAYLOAD_BYTES`, keep the first ~40% and the last ~40%,
    insert an explicit "[ELIDED: N lines]" marker between. This
    preserves the run's beginning (plan, first tasks) and end
    (failures, terminal status) — typically what Reporter cares about.

    Why bytes and not lines? JSONL line lengths vary wildly (an Aider
    stdout dump can be 50 KB on one line). Bytes match the token
    budget better.
    """
    raw = log_path.read_bytes()
    if len(raw) <= _MAX_EVENTS_PAYLOAD_BYTES:
        return raw.decode("utf-8", errors="replace")

    head_size = int(_MAX_EVENTS_PAYLOAD_BYTES * 0.4)
    tail_size = int(_MAX_EVENTS_PAYLOAD_BYTES * 0.4)
    head = raw[:head_size].decode("utf-8", errors="replace")
    tail = raw[-tail_size:].decode("utf-8", errors="replace")
    # Snap head/tail to line boundaries so the persona doesn't get
    # half a JSON object — those are useless and confusing.
    head = head.rsplit("\n", 1)[0]
    tail = tail.split("\n", 1)[-1] if "\n" in tail else tail
    elided_bytes = len(raw) - len(head.encode("utf-8")) - len(tail.encode("utf-8"))
    nl = b"\n"
    elided_events = raw.count(nl) - head.count("\n") - tail.count("\n")
    marker = (
        f"\n[ELIDED: {elided_bytes} bytes / approximately "
        f"{elided_events} events omitted for length]\n"
    )
    return head + marker + tail
