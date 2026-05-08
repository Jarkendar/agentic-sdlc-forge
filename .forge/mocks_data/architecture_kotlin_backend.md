# Architecture Map — TaskForge API

> Lightweight task tracking REST API for small dev teams; replaces a Trello board with a self-hosted, single-binary deployment.

## 1. Project Identity

- **Problem:** Small teams need a task board they can self-host without spinning up Postgres + Redis + a JIRA license; existing OSS options are either too heavy (Taiga) or too primitive (plaintext todo).
- **Target Users:** Backend developers running internal tools on a single VPS; no end-user UI in this repo (clients consume the REST API).
- **Platform:** Backend JVM (Linux, single-binary deploy via Shadow JAR)
- **Language:** Kotlin 2.0 (JVM target 21)
- **Build System:** Gradle 8.10 with Kotlin DSL

## 2. Technology Stack

### Required Technologies

- **Ktor 3.x** — HTTP server and routing.
- **Exposed** — type-safe SQL DSL over JDBC.
- **HikariCP** — connection pooling.
- **Koin 4.x** — dependency injection.
- **kotlinx.serialization** — JSON (de)serialization on request/response boundary.
- **kotlinx.coroutines** — concurrency primitives; every I/O call is a `suspend fun`.
- **kotlin-logging + Logback** — structured logging.
- **JUnit 5 + MockK + Testcontainers** — unit and integration testing.

### Anti-patterns — PROHIBITED

- **NEVER** use blocking JDBC calls outside `Dispatchers.IO`. Every Exposed transaction must be wrapped in `newSuspendedTransaction`.
- **NEVER** introduce Spring or Spring Boot. The whole point of this project is to stay slim.
- **NEVER** use Jackson, Gson, or Moshi. Serialization is `kotlinx.serialization` only.
- **NEVER** throw exceptions across the public API boundary — return `Result<T, AppError>` from services.
- **DO NOT** add reflection-heavy libraries (e.g. ModelMapper). Mapping is hand-written in `mappers/`.

## 3. Architecture & Responsibilities

### Pattern

Hexagonal (ports & adapters) with a thin service layer. Domain types are pure Kotlin data classes; adapters (HTTP, DB) live at the edges and convert to/from domain types.

### Module/Directory Structure

```
src/main/kotlin/com/taskforge/
├── domain/          # Pure business types: Task, User, Project, value objects
├── ports/           # Interfaces: TaskRepository, UserRepository, EventPublisher
├── application/     # Use cases: CreateTaskUseCase, AssignTaskUseCase, ...
├── adapters/
│   ├── http/        # Ktor routes, request/response DTOs, mappers
│   └── persistence/ # Exposed table definitions, repository impls
├── config/          # Koin modules, env config loader
└── Main.kt          # Wiring: install plugins, start engine
```

### Data Flow

HTTP request → Ktor route → maps DTO to domain → calls Application use case → use case calls Port (interface) → adapter implements port → returns domain type → route maps domain to response DTO → JSON response.

### State Management

The server is stateless. All persistent state lives in PostgreSQL via Exposed. Per-request state is in coroutine context. No singletons holding mutable state — Koin scopes are `single` only for adapters and connection pools.

### Validation

Validation lives in **value objects** in `domain/`. `TaskTitle`, `Email`, `ProjectId` are inline value classes with private constructors and `operator fun invoke(raw: String): Result<...>` factories. Routes never accept raw strings — they construct value objects first and short-circuit on failure.

### Dependency Injection

Koin 4.x with explicit modules per layer:
- `persistenceModule` — Exposed tables, HikariCP DataSource, repository impls
- `applicationModule` — use cases (factory-scoped)
- `httpModule` — route configuration helpers

`Main.kt` calls `startKoin { modules(...) }` and Ktor's `install(Koin)`.

### Error Handling

Domain-level: every fallible operation returns `kotlin.Result<T>` or a custom `Either<AppError, T>` (using `arrow-core` is allowed but optional).

`AppError` is a sealed class hierarchy:
```kotlin
sealed class AppError {
    data class NotFound(val resource: String, val id: String) : AppError()
    data class Validation(val field: String, val reason: String) : AppError()
    data class Conflict(val message: String) : AppError()
    data class Internal(val cause: Throwable) : AppError()
}
```

A single `StatusPages` block in the HTTP adapter maps `AppError` to HTTP status codes. **Exceptions never leak past use cases.**

### Layer Communication Rules

- `domain/` MUST NOT import anything outside `kotlin.*` and `kotlinx.serialization`. No Ktor, no Exposed, no Koin.
- `application/` MAY import `domain/` and `ports/`. MUST NOT import any adapter.
- `adapters/persistence/` MAY import `domain/` and `ports/`. MUST implement port interfaces.
- `adapters/http/` MAY import `application/` and `domain/` (for value object construction). MUST NOT import `adapters/persistence/`.
- Cross-adapter imports are forbidden — adapters communicate only through `application/`.

## 4. System Boundaries & Integrations

### External APIs

None directly. The service is a producer of a REST API documented via an OpenAPI 3.1 spec generated by `ktor-openapi-tools` at `/openapi.yaml`. Spec is committed to the repo and regenerated by a Gradle task before each release.

### Local Storage

PostgreSQL 16 via Exposed. Schema migrations are managed by Flyway with SQL files under `src/main/resources/db/migration/`. **No ORM-level schema generation in production.**

### Third-party SDKs

None. The project deliberately avoids SDKs to keep the binary slim and the supply chain auditable.

### Background Processing

Coroutine-based scheduled jobs via a thin `JobScheduler` wrapper around `Dispatchers.Default` and `delay()`. For anything heavier (retries, persistence, distribution), the project will adopt **Quartz with JDBC store** — but only when justified by a concrete need.

### Other Integrations

- **stdout/stderr structured logs** consumed by an external log aggregator (Loki/Promtail in deploy).
- **Prometheus metrics** at `/metrics` via `ktor-server-metrics-micrometer`.

## 5. Known Constraints & Tech Debt

- **Auth is JWT-only and does not support refresh tokens.** Tokens are 24h. A refresh-token redesign is on the roadmap; do not extend the current `AuthService` without flagging this.
- **The `LegacyTaskImporter` in `adapters/persistence/legacy/` reads from the previous SQLite-based version.** It is scheduled for removal once all installations are migrated. Do not add features to it.
- **No multi-tenancy.** Every deployment is single-team. Adding multi-tenancy would require schema changes (`tenant_id` columns) and is explicitly out of scope.
- **Testcontainers is used for integration tests, which means CI requires Docker.** Locally, devs may run tests against a `docker compose up postgres` instance; this is the only supported alternative.

---
*Generated by Agentic SDLC Forge — 2026-05-08*
*Review and edit this document as your project evolves.*
