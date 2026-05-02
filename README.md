# 🛠️ Agentic SDLC Forge

**Agentic SDLC Forge** is a CLI-driven initialization tool that injects a complete, multi-agent Software Development Life Cycle (SDLC) pipeline and a dynamic Knowledge Base into any existing or new project (Android, Kotlin Multiplatform, Python, etc.).

Instead of treating AI tools (like Aider or Claude) as simple autocomplete mechanisms, this forge sets up a structured, role-based workflow. It solves the biggest issue with modern LLMs—context overflow and hallucination—by heavily restricting the context window through a strict, multi-step pipeline.

## 🎯 The Problem

When given an entire codebase and a broad task, LLMs tend to lose focus, break architectural rules, or hallucinate dependencies. They need bounded context, clear rules, and a feedback loop.

## ⚙️ The Solution: A Role-Based Pipeline

This tool initializes a virtual development squad within your repository. Each role has a specific prompt, model class, and structured I/O contract. Weak/cheap models handle routing and dispatch; strong models handle planning and verification.

1. 🎯 **The Orchestrator** — *weak model (e.g. Haiku)*
   * **Role:** State router for the entire run.
   * **Action:** Drives the state machine (`PLANNING → EXECUTING → VERIFYING → FIX_LOOP | NEXT_TASK | DONE`). Makes no semantic decisions about code — only chooses the next legal transition based on the structured outputs of other agents.

2. 🗺️ **The Planner** — *strong model (e.g. Sonnet/Opus)*
   * **Role:** Decomposes a user story into atomic tasks.
   * **Action:** Reads the user story, the Knowledge Base, and the project file tree. Outputs a strict list of small, testable tasks — each with goal, target files, and acceptance criteria. Does not write implementation code.

3. 💻 **The Executor** — *weak model + Aider*
   * **Role:** Runs one task at a time.
   * **Action:** Takes a single Task from the Plan, formats it for Aider, and invokes `aider` as a subprocess against the bounded file set. The actual code generation happens inside Aider; the Executor is the dispatcher.

4. 🧪 **The Verifier** — *strong model*
   * **Role:** Quality assurance and failure triage.
   * **Action:** Runs configured commands (tests, lint, build), reads the output, and classifies failures (`CRITICAL` / `WARNING` / `FLAKY`). Critical failures bounce back to the Executor with structured feedback. The fix loop is bounded by hard retry limits.

5. 📰 **The Reporter** — *strong model*
   * **Role:** Final summary after the run completes.
   * **Action:** Reads the full event log produced during the run and generates a human-readable markdown report — tasks completed, failures, retries, token spend per agent, escalations.

### Sidecar infrastructure

🪵 **EventLog** — *not a persona, no LLM*
   * Append-only JSONL sink at `.forge/runs/<run_id>/events.jsonl`. Every agent writes structured events (start/end, tokens, payloads). The log is the single source of truth for resumability and reporting.

### Future work

📚 **The Documentalist** *(planned)* — will handle merge request generation and automatic Knowledge Base updates after a successful run. Currently out of scope; the Reporter covers per-run summaries until then.

## 🧠 The Knowledge Base (the living brain)

At the core of the pipeline is the Knowledge Base injected during initialization. It is not just a static `README`. It consists of:

* **Core Principles** — general coding standards, AI interaction language rules.
* **Domain Context** — auto-generated during setup via an AI-driven business interview (project goals, personas).
* **Architectural Rules** — platform-specific guidelines (e.g. KMP rules, Android Compose standards, Clean Architecture rules).
* **Git Flow Rules** — branch naming, conventional commits, atomic commits (see `.forge/git_flow.md`).
* **Dynamic File Tree** — a continuously updated map of the project used by the Planner to build context.

## 🚀 Getting Started

The runtime and CLI are under active development. The build plan is tracked in [`.forge/IMPLEMENTATION_PLAN.md`](.forge/IMPLEMENTATION_PLAN.md) — open it to see what is shipped and what is next.

For local LLM development, `docker-compose.yml` and `start_llm.sh` provide an Ollama-based setup (Qwen, Gemma, Llama) — useful for running the Executor and Verifier offline.

## 💡 Philosophy

* **Bounded context over big context.** Every agent sees only what it needs. The Planner never sees raw code; the Executor never sees the full codebase.
* **Structured I/O between agents.** Free text is for humans (the Reporter's output). Between agents, every contract is a validated schema. No prompt-injection-by-accident.
* **Cheap models for routing, expensive models for judgment.** The Orchestrator runs constantly and must be cheap. The Verifier runs less often and must be smart.
* **Hard limits on retries.** Every loop has a budget. No agent can spend your money in its sleep.
* **YAGNI for tooling, not for rules.** Rules and Knowledge Base stay in pure Markdown — tool-agnostic. The runtime is Python and binds to specific LLM providers, but the rules survive any change of tooling.

## 📜 License

MIT — see [`LICENSE`](LICENSE).