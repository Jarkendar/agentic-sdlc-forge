# Implementation Plan — Agentic SDLC Forge Runtime

> **Living document.** Update checkboxes as you ship. Each stage has a Definition of Done — do not move on until DoD is met. Stages are ordered so that each one is independently testable.

---

## 0. Context & Decisions

### 0.1 What exists today

- ✅ `README.md` — describes the multi-agent vision (4 personas)
- ✅ `.forge/git_flow.md` — hard rules for agents (commit format, branch naming)
- ✅ `.forge/architecture_map.md` — interview template + LLM synthesis prompt (draft, not yet wired into a CLI)
- ✅ `.githooks/pre-commit` — secret-leak detection (emails, tokens, passwords)
- ✅ `docker-compose.yml` + `start_llm.sh` — local Ollama setup (Qwen, Gemma, Llama) for offline dev
- ✅ `migrations/migrate_claude_to_aider.py` — one-shot migration tool (side concern, not part of runtime)

### 0.2 What is missing (this plan fills it)

- ❌ Runtime that actually orchestrates agents
- ❌ CLI (`forge init`, `forge run`) promised by README
- ❌ Architecture interview wired into the CLI
- ❌ Persona prompts as files (README describes them, repo doesn't ship them)
- ❌ Event log + reporting

### 0.3 Persona naming (locked decisions)

| Role in runtime | Name | Model class | Notes |
|---|---|---|---|
| State router | **Orchestrator** | weak (Haiku-class) | Pure routing, no semantic judgment |
| User story → task list | **Planner** | strong (Sonnet/Opus) | Replaces README's "Architect" — narrower scope (atomic task decomposition) |
| Per-task Aider runner | **Executor** | weak | Formats prompt + invokes `aider` subprocess |
| Test run + failure classification | **Verifier** | strong | Replaces README's "Tester" — broader (lint, build, contract checks too) |
| Final run summary | **Reporter** | strong | Reads event log, produces markdown report |
| Append-only event sink | **EventLog** | — (infra, no LLM) | Sidecar component, dependency-injected into every agent |
| Auto-update KB + MR | **Documentalist** | strong | **Future work** — README's original Documentalist split into Reporter (now) + Documentalist (later) |

### 0.4 Locked architectural decisions

1. **Rules and Knowledge Base stay markdown.** Runtime is Python. The "tool-agnostic" promise from README applies to KB, not to runtime.
2. **Event log is JSONL on disk** at `.forge/runs/<run_id>/events.jsonl`. SQLite migration is future work — re-evaluate after 10+ real runs.
3. **Structured outputs are mandatory.** Every agent returns a pydantic-validated schema. Free-text outputs are forbidden between agents (only Reporter outputs free text, and only for humans).
4. **Hard limits on retries** prevent budget runaway: max 3 retries per task, max 10 retries per run.
5. **Orchestrator never makes semantic decisions.** If a routing decision requires understanding code, that decision belongs to a strong-model persona.

### 0.5 Open questions (resolved — see 0.6 for outcomes)

- [x] **API keys / model config** — env vars vs config file vs both? → see 0.6.1
- [x] **Aider invocation** — call `aider --message` per task, or keep one Aider session alive across tasks? → see 0.6.2
- [x] **Parallel task execution** — sequential MVP, or allow non-conflicting tasks in parallel? → see 0.6.3
- [x] **Cost tracking** — track tokens per agent and surface in Reporter? → see 0.6.4

### 0.6 Resolved decisions

#### 0.6.1 API keys / model config — **hybrid: env for secrets, TOML for config**

- Secrets (API keys) live in `.env` (already covered by `.gitignore`).
- Per-persona model assignments and runtime limits live in `.forge/config.toml` (commitable, no secrets).
- A `.forge/config.example.toml` ships in the repo as a template.

Reference shape:

```toml
# .forge/config.toml
[models.orchestrator]
provider = "anthropic"
model = "claude-haiku-4-5"

[models.planner]
provider = "anthropic"
model = "claude-opus-4-7"

[models.executor]
provider = "ollama"
model = "qwen2.5-coder:7b"
base_url = "http://localhost:11434"

[models.verifier]
provider = "anthropic"
model = "claude-sonnet-4-6"

[models.reporter]
provider = "anthropic"
model = "claude-sonnet-4-6"

[limits]
max_retries_per_task = 3
max_retries_per_run = 10
task_timeout_seconds = 600
```

```bash
# .env (gitignored)
ANTHROPIC_API_KEY=sk-ant-...
```

**Rationale:** swapping a model is a one-line TOML edit; secrets never leak into git; the existing `.githooks/pre-commit` enforces the boundary.

#### 0.6.2 Aider invocation — **per-task subprocess**

Each task spawns a fresh `aider` subprocess and exits when the task ends. No persistent session in MVP.

**Why:**
- **Isolation** — fresh context per task; Aider cannot cross-contaminate task A's reasoning into task B.
- **Crash safety** — a failing Aider kills one task, not the whole run.
- **Debuggability** — clean 1:1 mapping between `task_id` and a single stdout/stderr stream in the event log.
- **Resumability** — kill mid-run, restart, continue from the next task without rebuilding session state.

**Cost:** ~1–2s startup overhead per task (process spawn + repo re-index). Negligible vs. generation time. Persistent session is reconsidered in Stage 9 only if this overhead becomes painful.

#### 0.6.3 Parallel task execution — **sequential in MVP**

Tasks run one at a time. Parallel execution is deferred to Stage 9 because it requires:

- git worktree per task (otherwise concurrent commits race on the branch),
- a conflict analyzer in Planner (declared-independent tasks may still both touch `build.gradle`, shared imports, etc.),
- per-task verification scoping (otherwise a failing test cannot be attributed to a specific task).

That is a separate body of work, not a switch to flip.

#### 0.6.4 Cost tracking — **enabled from Stage 1**

Every event in the JSONL log carries `tokens_in`, `tokens_out`, `cost_usd`, `duration_ms`. Cost is computed inside `LLMClient` from a static price table at `forge/pricing.py`. Reporter aggregates `cost_usd` by `agent`.

**Why now, not later:** the data is already returned by every provider's API; capturing it costs ~5 lines of code. Backfilling cost into historical runs is impossible. Without per-agent cost data, the "weak vs. strong models per persona" architecture has no measurable basis.

#### 0.6.5 Orchestrator routing — **deterministic FSM, LLM as optional shadow** (D1, Stage 7)

The Orchestrator's routing is a deterministic finite state machine in `forge/router.py`. The decision table in `.forge/personas/orchestrator.md` is the canonical contract; `tests/test_router_contract.py` parses the table and asserts the runtime agrees row-for-row.

The persona prompt stays as documentation and as the surface for an **optional shadow LLM** (config knob, default OFF). When shadow is enabled, the LLM is called with the same inputs the deterministic router gets, its answer is validated against `legal_actions`, and the comparison is logged. The LLM never affects the actual route.

**Rationale:** the decision table is closed and simple; LLM adds no value, costs tokens per turn, and adds a failure mode. Stage 6 already introduced three LLM-dependent paths (Planner, Verifier, Reporter); a fourth without a benefit was an anti-pattern. Shadow can be enabled later with one flag if prompt-tuning data becomes useful.

---

## Stage 1 — Foundations: schemas, state, event log

> No LLM calls yet. Pure plumbing. Get this wrong and every later stage suffers.

### 1.1 Tasks

- [x] Create `forge/` Python package (project root or under `src/`, decide once)
- [x] Define pydantic schemas in `forge/schemas.py`:
  - `Task` — `id`, `goal`, `files: list[Path]`, `acceptance_criteria: list[str]`, `depends_on: list[str]`
  - `Plan` — `run_id`, `user_story`, `tasks: list[Task]`, `created_at`
  - `ExecutionResult` — `task_id`, `status: Literal["success","failed","skipped"]`, `aider_stdout`, `aider_stderr`, `files_changed: list[Path]`
  - `TestReport` — `task_id`, `passed: bool`, `failures: list[Failure]`, `severity: Literal["critical","warning","flaky"]`
  - `RunState` — full run snapshot (current task, completed tasks, retry counts, status)
  - `Event` — `run_id`, `timestamp`, `agent`, `phase`, `duration_ms`, `tokens_in`, `tokens_out`, `cost_usd`, `payload` (per decision 0.6.4 — cost fields are first-class from day 1)
- [x] Implement `forge/event_log.py`:
  - `EventLog` class, append-only JSONL writer
  - `log(agent, phase, payload, **metadata)` — auto-adds `run_id`, `timestamp`, `tokens_in/out`, `duration_ms`, `cost_usd`
  - `fsync` after every write (crash-safety)
  - Reader helper: `EventLog.read_run(run_id) -> Iterator[Event]`
- [x] Implement `forge/state.py`:
  - `RunState.load(run_id)` / `RunState.save()` — JSON file at `.forge/runs/<run_id>/state.json`
  - State transitions are explicit methods (`mark_task_complete`, `increment_retry`, etc.) — no raw field mutation
- [x] Implement `forge/config.py` (per decision 0.6.1):
  - Load `.forge/config.toml` (model assignments + limits) and `.env` (API keys via `python-dotenv` or `os.environ`)
  - Pydantic-validated config schema; fail fast on missing required fields
  - Ship `.forge/config.example.toml` as a template
- [x] Implement `forge/pricing.py` (per decision 0.6.4):
  - Static price table: `{provider: {model: {input_per_1m_usd, output_per_1m_usd}}}`
  - `cost_for(provider, model, tokens_in, tokens_out) -> float` helper used by `LLMClient` in Stage 3
  - Unit test that all models referenced in `config.example.toml` exist in the price table

### 1.2 Definition of Done

- Unit tests for schemas (round-trip serialization)
- Unit tests for `EventLog` (concurrent writes, crash recovery — kill process mid-write, verify last complete event survives)
- `RunState` can be saved, killed, reloaded, resumed
- `forge/config.py` loads `config.example.toml` + a fake `.env` and validates; missing API key for a configured provider fails loudly
- `forge/pricing.py` returns correct cost for a known (provider, model) pair; raises on unknown model
- `pytest` green, `ruff` clean

### 1.3 What can go wrong

- **Schema churn.** Once Stage 2+ depend on these schemas, changes get expensive. Spend time here. Add fields only when they are needed by something concrete — but think about what fields each downstream stage will need before locking.
- **JSONL corruption.** If you skip the fsync test, you will lose data the first time something crashes mid-run, and you will not know until you try to debug a run.

---

## Stage 2 — Persona prompts as files

> README describes personas, but they need to live as actual files the runtime loads.

### 2.1 Tasks

- [x] Create `.forge/personas/` directory
- [x] Write `orchestrator.md` — system prompt for state routing. Output schema: `{"next_action": "...", "reasoning": "..."}`. Include the legal state machine (PLANNING → EXECUTING → VERIFYING → FIX_LOOP | NEXT_TASK | DONE) directly in the prompt.
- [x] Write `planner.md` — system prompt that produces a `Plan`. Include atomicity rules ("one file, one purpose, ≤200 lines of change, testable in isolation"). Reference `architecture_map.md` and `git_flow.md` as required reading.
- [x] Write `executor.md` — system prompt that takes a `Task` and produces an Aider invocation (which files to `/add`, what message to send). For MVP this can be a deterministic template; the LLM-driven version is Stage 5b.
- [x] Write `verifier.md` — system prompt that reads test/lint/build output and returns a `TestReport` with severity classification. Include severity criteria explicitly ("CRITICAL = compilation error or test failure on touched code; WARNING = lint or unrelated flake; FLAKY = test passed on retry").
- [x] Write `reporter.md` — system prompt that reads `events.jsonl` and produces a markdown summary for humans.
- [x] `forge/personas.py` — loader that reads prompt files, supports variable interpolation (`{{architecture_map}}`, `{{file_tree}}`)

### 2.2 Definition of Done

- Each persona file exists with: role description, input contract, output schema, hard rules
- Loader has unit tests
- Manual smoke test: render each persona prompt with example variables, eyeball it

### 2.3 What can go wrong

- **Prompts and code drift.** When you change the schema in `forge/schemas.py`, the persona prompts that describe the schema in natural language will lag. Mitigation: in each persona file, include a `<!-- AUTO-GENERATED FROM forge/schemas.py -->` block, generate it from the pydantic schema. Or at minimum: a test that fails when the persona file references a field that no longer exists in the schema.

---

## Stage 3 — LLM client abstraction

> Don't bind to one provider. Don't over-engineer either.

### 3.1 Tasks

- [x] `forge/llm.py` — abstract `LLMClient` with `complete(prompt, schema=None) -> Response`
- [x] Two implementations:
  - `AnthropicClient` (Haiku, Sonnet, Opus)
  - `OllamaClient` (uses local Docker setup) — for Executor in offline mode and for cheap dev
- [x] Structured output support — when `schema` is passed, validate response against pydantic schema, retry once on validation failure with the validation error appended to the prompt
- [x] Token + duration accounting — return alongside response, EventLog consumes it
- [x] Config: `forge/config.py` reads `.forge/config.toml` for model assignments per persona, env vars for API keys

### 3.2 Definition of Done

- ✅ Both clients return same `LLMResponse` shape
- ✅ Schema validation + retry tested (Anthropic: `test_validation_retries_once_on_invalid_first_response`, Ollama: `test_structured_validation_retries_once_on_bad_json`)
- ✅ Switching a persona's model is a single config edit (`get_client()` reads from `ForgeConfig.models[persona]`)
- ✅ 32 tests passing, ruff clean

### 3.3 What can go wrong

- **Premature abstraction.** Resist adding a third provider until you actually need it. Two providers is enough to prove the abstraction holds; three+ providers without a real use case is yak-shaving.

---

## Stage 4 — Planner (first real LLM agent)

> Validate the contract before plumbing the orchestrator. Build Planner standalone.

### 4.1 Tasks

- [x] `forge/agents/planner.py` — takes user story + paths to KB files + file tree, returns validated `Plan`
- [x] CLI entry point: `forge plan "user story here"` — outputs the plan as JSON and as human-readable markdown
- [x] Logs to EventLog throughout

### 4.2 Definition of Done

- Run Planner on 3 real user stories from your own backlog
- Manually inspect: are tasks actually atomic? Are file lists tight? Are acceptance criteria checkable?
- If quality is bad, iterate on `planner.md` prompt **before moving on**. The downstream stages assume Planner output is good. Garbage in, garbage out — and Stage 5 is much harder to debug if Planner is the actual problem.

### 4.3 What can go wrong

- **Tasks that are too big.** Most common Planner failure mode. Mitigation: add a hard rule in the prompt ("if a task touches more than 3 files, split it") and a post-hoc validator that flags oversized tasks.
- **Tasks with hidden dependencies.** Planner says task B is independent of A, but B's acceptance criterion implicitly requires A's output. Mitigation: have Planner explicitly fill `depends_on`, and include "if you cannot test task X without task Y existing first, list Y as a dependency" in the prompt.

---

## Stage 5 — Executor + Aider integration

### 5.1 Tasks

- [x] `forge/agents/executor.py` — takes a `Task`, orchestrates aider + git ops, returns `ExecutionResult`. Deterministic (no LLM call); persona file is the human-readable contract, the module is the executable version.
- [x] `forge/aider_runner.py` — subprocess wrapper around `aider --message ... --yes --no-stream <files>`. Captures stdout/stderr, enforces 600s timeout via `start_new_session=True` + `os.killpg(SIGKILL)` for the whole process group (Linux-first; Windows path is future work).
- [x] `forge/git_ops.py` — owns all git operations: `ensure_clean_worktree`, `ensure_run_branch` (idempotent), `create_task_branch`, `current_head_sha`, `diff_files_since`, `squash_task_commits`, `merge_task_into_run`, plus `OutOfScopeEdit` detection. Stage 7's Orchestrator will reuse these primitives.
- [x] **Decided** (open question 0.5): per-task subprocess vs. persistent session. **Per-task subprocess** in MVP. Simpler, isolated, easier to debug. Persistent session is a Stage 9+ optimization.
- [x] **Per-task branch model.** Each task runs on `forge/task/<run_id>/<task_id>` branched from `forge/run/<run_id>` tip. On `success` the task branch is squashed into one conventional commit (with `forge-task-id:` / `forge-run-id:` footer) and merged via `git merge --no-ff` into the run branch. On `failed` / `no_changes` / out-of-scope the task branch is preserved as-is for inspection — no squash, no merge. End-of-task invariant: HEAD is on the run branch regardless of status.
- [x] **External aider binary** (D9). `aider` must be on `PATH` — runtime fails fast at construction with `AiderNotFoundError`. Pinning the version in pyproject would couple us to upstream CLI changes.
- [x] **Conventional commit type heuristic.** First word of `task.goal` maps to `feat` / `fix` / `refactor` / `test` / `docs` / `chore`, with `chore` as fallback. Replacing this with an explicit `commit_type` field on `Task` is tracked in Stage 9.
- [x] CLI: `forge execute <task_id> --plan plan.json [--repo .]` — loads plan from disk, runs one task, prints `ExecutionResult` JSON to stdout. Exit codes: `0` success, `1` pre-flight error, `2` task failed/no_changes.
- [x] `run_id` for `forge execute` comes from `plan.run_id`, not regenerated. Events append to `.forge/runs/<plan.run_id>/events.jsonl`, keeping planning + executions in one trail.

### 5.2 Definition of Done

- [x] Run Executor against tasks from a real Plan (covered by 32 unit tests using fake AiderRunner + real tmp git repos)
- [x] Files actually change on disk (verified in tests)
- [x] `git diff` is sensible — `--no-ff` merge commits visible on run branch, squashed conventional commits on task branches
- [x] EventLog captures full Aider stdout/stderr (`aider_complete` event payload)
- [ ] Manual smoke test: run on a real tiny repo, confirm aider actually edits files and the merge structure looks right (deferred to first real run after `forge plan` is exercised end-to-end)

### 5.3 What can go wrong

- **Aider hangs / asks for input.** Use `--yes` and `--no-stream`. Set a timeout (5–10 min per task).
- **Aider "succeeds" but nothing changed.** Aider sometimes reports success while having made no edits. Mitigation: verify `git diff` is non-empty, treat empty diff as failure (or as a separate `no_changes` status worth flagging).
- **Wrong files in scope.** Planner said "edit `Foo.kt`", Aider also touches `Bar.kt`. Mitigation: snapshot file list before, diff after, flag unexpected files in `ExecutionResult`.
- **Status `skipped` is reserved for Stage 7.** The standalone `forge execute` does not know about run history (which tasks have completed vs. failed) and therefore never emits `skipped`. The Orchestrator (Stage 7) is the first emitter — it checks `RunState.completed_task_ids` against `task.depends_on` *before* invoking the Executor.

---

## Stage 6 — Verifier + fix loop

### 6.1 Tasks

- [x] `forge/agents/verifier.py` — runs configured commands (e.g. `./gradlew test`, `pytest`, `ruff check`), captures output, sends to LLM for classification, returns `TestReport`
- [x] `forge/runner.py` — implements the fix loop: Executor → Verifier → if CRITICAL, feed failure back to Executor with `task.goal += "Previous attempt failed: {failure_summary}. Fix it."` → max 3 attempts → escalate
- [x] Per-project config for verification commands in `.forge/config.toml`

### 6.2 Definition of Done

- [x] Deliberately introduce a failing change, verify Verifier catches it and classifies as CRITICAL (covered by `tests/test_verifier.py` + `tests/test_runner.py`)
- [x] Verify fix loop runs, hits limit, escalates cleanly (with a clear "human needed" event in the log)
- [x] Flaky test (random pass/fail) is correctly classified as FLAKY on second attempt (Verifier's internal re-run path)

### 6.3 What can go wrong

- **Verifier hallucinates a critical failure that is actually flaky** → wastes retries. Mitigation: make Verifier always re-run the failing command once before classifying, and only label as CRITICAL if it fails on both runs.
- **Misclassifying compile errors as warnings.** Mitigation: explicit rule in `verifier.md` — "compilation/syntax errors are always CRITICAL".

---

## Stage 7 — Orchestrator + Reporter

### 7.1 Tasks

- [x] **Deterministic FSM router** in `forge/router.py` mirroring the decision table in `.forge/personas/orchestrator.md` (D1 — see 0.6.5). Pure function: `(current_state, last_event_kind, retry_caps_exhausted, more_tasks) → OrchestratorAction`. The shadow-LLM path stays as a future config knob; the persona file remains the canonical contract.
- [x] **Contract test** `tests/test_router_contract.py` — parses the decision table out of `orchestrator.md` and asserts the deterministic router agrees row-for-row. Drift between MD and code fails CI.
- [x] `forge/agents/orchestrator.py` — state machine driver. Pre-flight (clean worktree, run branch), Planner invocation, per-task loop, skipped propagation, retry-cap enforcement, Reporter handoff. **No LLM call** on the routing path (per D1); `OrchestratorDecision` schema retained for the future shadow path.
- [x] `forge/agents/reporter.py` — reads `events.jsonl`, aggregates per-agent tokens & `cost_usd` into a pre-built markdown table (so the LLM cannot invent numbers), calls Reporter persona, writes `RUN_REPORT.md` verbatim to `.forge/runs/<run_id>/RUN_REPORT.md`. Truncates very large logs (head + tail with `[ELIDED: N bytes]` marker) to protect the prompt's token budget.
- [x] CLI: `forge run "user story"` — full pipeline end to end. Exit codes: `0` DONE, `1` pre-flight error, `2` FAILED/ESCALATED.
- [x] CLI: `forge run --resume <run_id>` — resume from saved `state.json`. Rejects terminal-status runs (DONE/FAILED/ESCALATED) loudly.
- [x] CLI: `forge report <run_id>` — re-render `RUN_REPORT.md` from an existing event log without re-running anything else. Useful when iterating on `reporter.md`.
- [x] **Orchestrator emits `skipped`** for tasks whose `depends_on` references unfinished tasks (checked against `RunState.completed_task_ids` *before* invoking the Executor). Skipped tasks do not create a task branch — `Executor.run` is not called. (D7). The skipped event carries a `skip_reason` mentioning the upstream task ID and its status (`failed` / `skipped` / `unknown`), and a synthetic `executor:validated` event is emitted alongside so Reporter's task-table aggregation treats skipped uniformly.
- [x] **Skipped propagates** through dependency chains: t1 fails → t2 deps t1 → skipped → t3 deps t2 → skipped.
- [x] **Task order = Planner's order** (D4). `depends_on` is checked only to decide skip-vs-execute, not to topo-sort.
- [x] **State persisted at every transition.** 6 checkpoints: pre-PLAN, post-PLAN, before each task, after each task, after each skip, before/after Reporter. Crash mid-task → `forge run --resume <run_id>` picks up at the next unprocessed task.
- [x] **ESCALATED is sticky.** Once any task escalates (per-task cap exhausted) or the run-wide cap fires, the run's status stays ESCALATED even if subsequent tasks succeed — the "run is broken" signal must reach Reporter.
- [x] **Reporter always runs** for every terminal status (DONE / ESCALATED / FAILED). Reporter exceptions are non-fatal — logged but don't change the terminal status. Re-run with `forge report <run_id>` if needed.
- [x] **Orchestrator calls `git_ops.ensure_run_branch(run_id)` once at the start of a run.** Subsequent `Executor.run(...)` calls assume the run branch exists. The `forge execute` standalone path does this itself (idempotent), so the same `git_ops` primitive serves both call sites.
- [x] **End-of-task HEAD invariant.** After every `Executor.run(...)` HEAD is on the run branch — Orchestrator can invoke the next task without an explicit `git checkout`. This contract holds for every status: success, failed, no_changes, skipped.
- [x] **Failed tasks leave their branches behind on purpose.** `forge/task/<run_id>/<task_id>` with raw Aider commits (including out-of-scope edits, if any) persists for post-mortem inspection. Orchestrator does not clean these up; cleanup is a separate `forge clean --run-id <id>` concern.

### 7.2 Definition of Done

- [x] End-to-end run on a real (small) user story produces: a plan, executed tasks, passing tests, a markdown report (covered by orchestrator + reporter unit tests; **manual smoke test on a real repo deferred** — carries the Stage 5 smoke test too)
- [x] Report includes: tasks completed, tasks failed, total tokens per agent, total cost estimate, time per stage, escalations (cost table is pre-built by Reporter; persona prompt covers the narrative sections)
- [x] Run is fully resumable: kill the process mid-run, `forge run --resume <run_id>` picks up where it stopped (covered by `tests/test_orchestrator.py::test_state_persists_after_partial_run` + `tests/test_cli_stage7.py`)
- [x] 63 new tests passing on top of the existing suite. Ruff clean.

### 7.3 What can go wrong

- **Orchestrator "creativity"** — moot under D1 because the deterministic router has no LLM in the loop. When shadow is enabled later, illegal LLM choices are logged but ignored.
- **Infinite loops** — bounded by `max_retries_per_task=3` (per-task) and `max_retries_per_run=10` (run-wide). Run-wide cap fires before per-task chain can spend the whole budget.
- **MD / router drift** — caught by `tests/test_router_contract.py` at CI time.

---

## Stage 8 — CLI: `forge init` + interview

> Now that runtime works, deliver the README's other promise: scaffold any new project.

### 8.1 Tasks

- [ ] `forge/cli.py` — Click or Typer based CLI: `init`, `run`, `plan`, `execute`, `report`
- [ ] `forge init` — copies `.forge/` template into target dir, optionally runs the architecture interview
- [ ] Wire `architecture_map.md` interview from existing draft: ask sections 1–5, collect answers, send to LLM with synthesis prompt, write `.forge/architecture_map.md`
- [ ] `forge init --no-interview` to skip (for repos that already have an architecture map)

### 8.2 Definition of Done

- `forge init` on a clean directory produces a usable `.forge/` setup
- Interview produces a non-trivial `architecture_map.md` from realistic answers
- `forge run` works in the freshly-initialized project

---

## Stage 9 — Polish (optional, post-MVP)

- [ ] Cost dashboard / per-run summary table
- [ ] Persistent Aider session (perf optimization, see Stage 5)
- [ ] Parallel task execution for non-conflicting tasks
- [ ] Documentalist persona — auto-update KB and open MR (the original README promise)
- [ ] SQLite migration for event log (revisit decision 0.4.2 after real usage)
- [ ] Web UI to view past runs
- [ ] **Explicit `commit_type` field on `Task`.** Replace the heuristic in `forge/agents/executor.py` (`detect_commit_type`) with an explicit `commit_type: Literal["feat","fix","refactor","test","docs","chore","perf","style"]` field set by the Planner. Requires a SCHEMA_VERSION bump and a migrator for older state.json files. The heuristic is good enough for MVP but produces wrong types for goals that don't lead with one of the recognized verbs.
- [ ] **Windows support for the Aider subprocess wrapper.** MVP is Linux-only because timeout enforcement uses `start_new_session=True` + `os.killpg(SIGKILL)` to catch Aider's child processes. Windows needs a different path (`CREATE_NEW_PROCESS_GROUP` + `GenerateConsoleCtrlEvent`, or a Job Object).
- [ ] **`forge clean --run-id <id>`** to remove `forge/task/<run_id>/*` branches after a run is fully reviewed. Right now failed task branches accumulate; mass cleanup is `git branch -D $(git branch --list 'forge/*')` which is too blunt.
- [ ] **`forge replay <run_id> --only-failed`** — produce a fresh run whose plan consists of the failed and skipped tasks from a previous run, with the previous `RUN_REPORT.md` and last `TestReport`s injected into the Planner's input so it knows what to fix. Stage 7 already produces the data this needs (events.jsonl + RUN_REPORT.md + state.json with `failed_task_ids` / `skipped_task_ids`); this item is purely the consumption mechanism. Open questions: does replay reuse the original `run_id` (and append-write the same `events.jsonl`) or start a new one? Current bias: new run_id, with a `previous_run_id` field on the new RunState for traceability.
- [ ] **Orchestrator shadow LLM.** Flip the config knob `[orchestrator] shadow_llm = true` to call the Orchestrator persona alongside the deterministic router per turn, log the comparison, and ignore the LLM's answer. Use the data for prompt-tuning or to evaluate whether LLM-first routing is worth re-enabling. See 0.6.5.

---

## Risk register

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Planner produces non-atomic tasks | high | high | Stage 4 DoD requires manual quality check before proceeding |
| Aider hangs on input | medium | medium | `--yes --no-stream` flags + timeout |
| Cost runaway from retry loops | medium | high | Hard caps in fix loop; cost tracking in Reporter from day 1 |
| Schema changes break existing runs | medium | low | Schema versioning; runs tagged with schema version |
| Orchestrator misroutes | low | medium | Deterministic FSM router (D1); MD-vs-code contract test |
| Prompts and schemas drift | high | medium | Auto-generation or contract tests in Stage 2 |
| Local Ollama models too weak for Verifier | medium | medium | Verifier defaults to Anthropic; Ollama is optional fallback |

---

## Glossary

- **Agent / Persona** — a role with a system prompt, model assignment, and structured I/O contract
- **Task** — atomic unit of work the Executor sends to Aider (one file, one purpose)
- **Plan** — ordered list of Tasks produced by Planner
- **Run** — one full execution of `forge run` from user story to report
- **Event** — single line in the JSONL log
- **Fix loop** — Executor ↔ Verifier cycle bounded by retry caps

---

*Last updated: 2026-05-11 — Stage 7 complete (Orchestrator + Reporter + `forge run` with `--resume` + `forge report`). Deterministic FSM router (D1, 0.6.5) with contract test against `orchestrator.md`. 63 new tests on top of the existing suite. `forge replay --only-failed` and Orchestrator shadow LLM carried to Stage 9 backlog.*