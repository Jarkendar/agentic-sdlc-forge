# Architecture Interview — Draft 

## How It Works

1. A CLI script (Python) asks questions section by section
2. The developer answers in the terminal (free text per question)
3. After collecting all answers — raw responses are sent to the LLM
4. The LLM synthesizes responses into a structured `architecture_map.md`

---

## SECTION 1 — Project Identity

```
1.1 What is the project name?
1.2 Describe the project in 1-2 sentences — what does it do and what problem does it solve?
1.3 Who are the target users? (developers, end-users, both?)
```

## SECTION 2 — Technology Stack

```
2.1 What platform is the project for? (Android / iOS / KMP / Backend JVM / Python / Web / other)
2.2 Primary programming language and version?
2.3 Build system? (Gradle / Maven / pip / npm / other)
2.4 List the key libraries/frameworks that MUST be used in the project.
    (e.g. Compose, Ktor, Koin, Room, Coroutines — one per line)
2.5 Technology anti-patterns — what must NEVER be used?
    (e.g. XML layouts, RxJava, wildcard imports, inheritance for sharing UI logic)
```

## SECTION 3 — Architecture & Responsibilities

```
3.1 What architectural pattern does the project follow?
    (MVI / MVVM / MVP / Clean Architecture / Hexagonal / other — combinations are fine)
3.2 How are directories/modules organized?
    Describe the structure or provide an example directory tree.
3.3 How does data flow through the application?
    (e.g. UI -> ViewModel -> UseCase -> Repository -> DataSource)
3.4 Who manages application state?
    (e.g. ViewModel with StateFlow, Redux store, MVI reducer — provide concrete class/pattern names)
3.5 Where does validation logic live?
    (e.g. domain layer, dedicated Validator classes, in UI, in use cases)
3.6 How does Dependency Injection work?
    (e.g. Koin modules, Dagger/Hilt, manual DI, service locator)
3.7 How are errors handled?
    (e.g. Result wrapper, sealed class hierarchy, exceptions, Either)
3.8 Are there communication rules between modules/layers?
    (e.g. "presentation must not import data", "domain has no framework dependencies")
```

## SECTION 4 — System Boundaries & Integrations

```
4.1 Does the project communicate with an external API? If so:
    - What kind? (REST / GraphQL / gRPC / WebSocket)
    - Is there documentation/contract? (OpenAPI spec, proto files)
4.2 Does the project use a local database? If so — which one?
    (Room / SQLDelight / Realm / raw SQLite / other)
4.3 Does the project integrate with third-party SDKs?
    (e.g. Firebase, Analytics, Payment SDK, Maps — list them)
4.4 Does the project have background processing?
    (WorkManager / Cron jobs / message queue / other)
4.5 Are there other systems/services the project communicates with?
    (e.g. message broker, shared preferences/datastore, file system, Bluetooth)
```

## SECTION 5 — Known Constraints & Conscious Trade-offs

```
5.1 Is there any known tech debt that AI agents should be aware of?
    (e.g. "module X is legacy and awaiting refactor — do not extend")
5.2 Are there any conscious architectural trade-offs?
    (e.g. "we know the singleton DB is an anti-pattern but it stays until v2")
5.3 Anything else an AI agent should know before touching the code?
```

---
---

## SYNTHESIS PROMPT (sent to LLM after collecting all answers)

```
You are a senior software architect. Your task is to synthesize raw interview
answers into a structured architecture document.

RULES:
- Write in English
- Be concise but precise — every sentence must carry information
- Do NOT invent information that wasn't in the answers
- If an answer is empty or "N/A", omit that subsection entirely
- Use the EXACT template structure below
- For anti-patterns: phrase them as clear prohibitions ("NEVER", "DO NOT")
- For architectural rules: phrase them as clear mandates ("MUST", "ALWAYS")

RAW INTERVIEW ANSWERS:
---
{raw_answers}
---

Generate the document using this template:

# Architecture Map — {project_name}

> {one-line project description}

## 1. Project Identity

- **Problem:** {what problem does it solve}
- **Target Users:** {who uses it}
- **Platform:** {platform}
- **Language:** {language + version}
- **Build System:** {build system}

## 2. Technology Stack

### Required Technologies
{bulleted list of mandatory libraries/frameworks with one-line purpose each}

### Anti-patterns — PROHIBITED
{bulleted list of things to NEVER do, phrased as clear prohibitions}

## 3. Architecture & Responsibilities

### Pattern
{architectural pattern with brief explanation of how it's applied}

### Module/Directory Structure
{description or tree of how code is organized}

### Data Flow
{step-by-step data flow through layers}

### State Management
{who owns state, what pattern, concrete class names if given}

### Validation
{where validation lives, what pattern}

### Dependency Injection
{DI approach and structure}

### Error Handling
{error handling strategy}

### Layer Communication Rules
{what can import what, what is forbidden}

## 4. System Boundaries & Integrations

### External APIs
{API type, contracts, documentation location}

### Local Storage
{database, preferences, file system}

### Third-party SDKs
{list with purpose}

### Background Processing
{approach and tools}

### Other Integrations
{anything else}

## 5. Known Constraints & Tech Debt

{list of known issues, conscious trade-offs, and warnings for AI agents}

---
*Generated by Agentic SDLC Forge — {date}*
*Review and edit this document as your project evolves.*
```

---

## OPEN QUESTIONS (for discussion)

1. **Section 3.2 (directory structure)** — should the script auto-scan
   the project tree and show it to the developer as a hint?
   E.g. "I detected this structure: [tree]. Is this correct? Describe what is what."

2. **Answer validation** — should the script enforce minimum answers
   for critical questions (e.g. 3.1, 3.4) or allow empty responses?

3. **Developer answer format** — do we provide free text fields for everything,
   or offer choices for some questions? (e.g. 3.1 — list of patterns to pick from)

4. **Interview language** — questions in English or localized?
   Developer answers will likely be mixed. The LLM synthesizes in English regardless.