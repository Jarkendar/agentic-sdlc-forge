# 🛠️ Agentic SDLC Forge

**Agentic SDLC Forge** is a CLI-driven initialization tool that injects a complete, Multi-Agent Software Development Life Cycle (SDLC) pipeline and a dynamic Knowledge Base into any existing or new project (Android, Kotlin Multiplatform, Python, etc.).

Instead of treating AI tools (like Aider or Claude) as simple autocomplete mechanisms, this forge sets up a structured, role-based workflow. It solves the biggest issue with modern LLMs—context overflow and hallucination—by heavily restricting the context window through a strict, multi-step pipeline.

## 🎯 The Problem
When given an entire codebase and a broad task, LLMs tend to lose focus, break architectural rules, or hallucinate dependencies. They need bounded context, clear rules, and a feedback loop.

## ⚙️ The Solution: A Role-Based Pipeline
This tool initializes a virtual development squad within your repository. The workflow is divided into 4 distinct AI personas, each with a specific prompt, context limit, and responsibility:

1. 🏗️ **The Architect (Planning & Context Narrowing)**
   * **Role:** Discusses the task with the human developer.
   * **Action:** Scans the dynamic project file tree, understands the business goals, and outputs a *precise, minimal list of files* required for the task. It does not write the implementation code.
2. 💻 **The Executor (Implementation)**
   * **Role:** Writes the actual code.
   * **Action:** Receives the exact task and the strictly bounded file context provided by the Architect. It strictly adheres to the project's architectural guidelines (e.g., MVI, Clean Architecture).
3. 🧪 **The Tester (Verification & Feedback Loop)**
   * **Role:** Quality Assurance.
   * **Action:** Verifies the implemented code or writes tests. If the logic fails, it bounces the task back to the Executor. If it passes, the pipeline moves forward.
4. 📚 **The Documentalist (Merge & Learn)**
   * **Role:** CI/CD and Knowledge Management.
   * **Action:** Generates the Merge Request and, crucially, updates the project's living Knowledge Base based on the newly introduced concepts or architectural decisions.

## 🧠 The Knowledge Base (The Living Brain)
At the core of the pipeline is the Knowledge Base injected during initialization. It's not just a static `README`. It consists of:
* **Core Principles:** General coding standards, AI interaction language rules.
* **Domain Context:** Auto-generated during setup via an AI-driven business interview (project goals, personas).
* **Architectural Rules:** Platform-specific guidelines (e.g., KMP rules, Android Compose standards).
* **Dynamic File Tree:** A continuously updated map of the project used by the Architect to build context.

## 🚀 Getting Started
*(Coming soon - Python CLI initialization script details)*

## 💡 Philosophy
**YAGNI (You Aren't Gonna Need It):** This tool is built to be tool-agnostic at its core. While currently optimized for tools like Aider and models like Claude, the underlying Knowledge Base relies on pure Markdown. If the AI tooling landscape changes, your rules and SDLC pipeline remain intact.
