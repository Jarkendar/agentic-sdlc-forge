# Architecture Map — feedforge

> Self-hosted RSS/Atom feed aggregator and reader API; replaces a commercial Feedbin/Feedly subscription with a single-binary Python service.

## 1. Project Identity

- **Problem:** Commercial RSS readers either die (Google Reader) or charge monthly for what is fundamentally a polling service. Self-hosted alternatives (FreshRSS, Tiny Tiny RSS) are PHP-stack heavy. We want a small Python service that polls feeds, stores entries, and exposes a clean REST API for any client.
- **Target Users:** Self-hosters and developers; the API is consumed by terminal clients, mobile apps (third-party), and a future companion web frontend (out of scope here).
- **Platform:** Backend Python service (Linux; deploy as systemd unit or Docker container).
- **Language:** Python 3.12 (uses `match` statements, exception groups, generic syntax).
- **Build System:** `uv` for dependency management and virtual environments; `pyproject.toml` for metadata; no `setup.py`.

## 2. Technology Stack

### Required Technologies

- **FastAPI** — HTTP layer; type-hinted handlers, automatic OpenAPI generation.
- **SQLAlchemy 2.x (async)** — ORM and query builder; async sessions only.
- **Alembic** — schema migrations; one migration per PR, no auto-generation in production.
- **Pydantic v2** — request/response models AND domain DTOs at module boundaries.
- **httpx** — outbound HTTP for fetching feeds; async client only.
- **feedparser** — RSS/Atom parsing.
- **APScheduler** — scheduled feed-polling jobs.
- **structlog** — structured JSON logging.
- **pytest + pytest-asyncio + httpx-respx** — testing.
- **ruff** — linting and formatting; the only style tool. No black, no flake8, no isort.
- **mypy --strict** — type checking; CI fails on `Any`-leakage.

### Anti-patterns — PROHIBITED

- **NEVER** use `requests`. The codebase is fully async; mixing sync HTTP would block the event loop.
- **NEVER** use synchronous SQLAlchemy sessions. All DB access goes through `AsyncSession`.
- **NEVER** use `time.sleep` in handler code; use `asyncio.sleep`.
- **NEVER** raise bare `Exception` or use bare `except:`. Catch specific types.
- **NEVER** put SQL strings in handler modules. SQL lives in `repositories/` and uses SQLAlchemy core or ORM.
- **NEVER** introduce Django, Flask, or Tornado. FastAPI is the only HTTP framework.
- **DO NOT** use Celery. APScheduler is sufficient for our polling load.
- **DO NOT** add untyped functions. Every public function has full type annotations.

## 3. Architecture & Responsibilities

### Pattern

Layered architecture with explicit boundaries: **handlers → services → repositories → database**. Domain models are Pydantic BaseModels separate from SQLAlchemy ORM models.

### Module/Directory Structure

```
src/feedforge/
├── api/
│   ├── routes/            # FastAPI APIRouter modules, one per resource
│   ├── dependencies.py    # FastAPI Depends() factories (DB session, auth)
│   └── schemas.py         # Request/response Pydantic models
├── domain/
│   ├── models.py          # Pydantic domain models (Feed, Entry, Subscription)
│   ├── errors.py          # Domain exception hierarchy
│   └── services/          # Business logic; one module per use case
├── infrastructure/
│   ├── db/
│   │   ├── orm.py         # SQLAlchemy ORM models
│   │   ├── session.py     # Async session factory
│   │   └── repositories/  # Repository implementations
│   ├── http/              # httpx-based feed fetcher
│   └── scheduler/         # APScheduler jobs and configuration
├── config.py              # Pydantic Settings; env-driven
└── main.py                # FastAPI app factory; wires routers, middleware, scheduler
```

### Data Flow

HTTP request → FastAPI route → calls a service via `Depends(get_service)` → service calls a repository (port-interface pattern, repos live in `infrastructure/`) → repository runs SQLAlchemy → returns ORM rows → service maps ORM to Pydantic domain model → returns to route → route maps domain model to response schema.

For polling: `APScheduler` triggers `poll_feeds_job()` → service iterates subscriptions → for each, `FeedFetcher.fetch(url)` returns parsed entries → service deduplicates against existing entries (by GUID + content hash) → persists new entries via repository.

### State Management

- The HTTP service is stateless. No in-memory caches that survive request boundaries.
- The scheduler runs in the same process as the HTTP server (not a separate worker), but on a dedicated thread pool.
- Connection pooling is handled by SQLAlchemy's async pool. Pool size is `os.cpu_count() * 2` by default.

### Validation

- **Pydantic everywhere on the boundaries.** Request bodies, query params, response models, domain DTOs — all `BaseModel` subclasses with `model_config = ConfigDict(extra="forbid")`.
- **Domain invariants in `__post_init_post_parse__` / `model_validator(mode='after')`.** Example: a `Feed` can never have `last_polled_at < created_at`; this is enforced at construction.
- **No validation in route handlers.** Routes assume validated input and return validated output.

### Dependency Injection

FastAPI's `Depends` system. We do NOT use a third-party DI framework.

- `api/dependencies.py` defines `get_db_session()`, `get_feed_service()`, `get_current_user()`, etc.
- Services receive their repositories via `__init__`; repositories receive their `AsyncSession` via FastAPI's request-scoped dependency.
- For tests, `app.dependency_overrides[get_db_session] = lambda: test_session` swaps the real session for an in-memory SQLite one.

### Error Handling

Three-layer strategy:

1. **Domain layer raises typed exceptions** from `domain/errors.py`:
   ```python
   class DomainError(Exception): ...
   class FeedNotFoundError(DomainError): ...
   class DuplicateSubscriptionError(DomainError): ...
   class FeedFetchError(DomainError): ...
   ```
2. **Service layer never catches DomainError.** It bubbles up unchanged.
3. **A single `add_exception_handler()` block in `main.py`** maps each `DomainError` subclass to an HTTP status (404, 409, 502, ...). Unknown exceptions become 500 with a logged correlation ID.

`HTTPException` is used ONLY at the API boundary; never raised from `domain/` or `infrastructure/`.

### Layer Communication Rules

- `domain/` MUST NOT import from `infrastructure/` or `api/`. It depends only on standard library and Pydantic.
- `infrastructure/` MAY import `domain/` (to map ORM to domain models).
- `api/` MAY import `domain/` and `infrastructure/dependencies` (for Depends factories).
- Repositories are accessed via interfaces declared in `domain/`. Concrete impls live in `infrastructure/db/repositories/`.
- Cross-route imports (one router importing another's helpers) are **forbidden**. Shared helpers go in `api/dependencies.py` or a new `api/_helpers.py`.

## 4. System Boundaries & Integrations

### External APIs

The service consumes RSS/Atom feeds from arbitrary third-party URLs. There is no contract — feeds may be malformed, slow, or absent. The fetcher must:
- Time out after 30s per feed.
- Honor `Last-Modified` and `ETag` headers to avoid re-downloading unchanged feeds.
- Retry transient failures (5xx, network errors) with exponential backoff up to 3 attempts.
- Log and skip permanent failures (4xx, parse errors); never crash the polling job.

### Local Storage

PostgreSQL 16 (production) or SQLite (development). Schema migrations via Alembic. The codebase MUST work against both — no Postgres-specific features in queries unless gated behind a dialect check.

### Third-party SDKs

None. Outbound HTTP is httpx; feed parsing is feedparser; auth is JWT via `python-jose`. No vendor SDKs.

### Background Processing

APScheduler with `AsyncIOScheduler`:
- `poll_feeds_job` — runs every 15 minutes; polls all subscriptions whose `next_poll_at` has passed.
- `cleanup_old_entries_job` — runs daily at 3am; deletes entries older than user-configured retention.

Jobs are idempotent and safe to skip on missed schedules.

### Other Integrations

- **Prometheus metrics** at `/metrics` via `prometheus-client`.
- **OpenTelemetry traces** exported to OTLP endpoint when `OTEL_EXPORTER_OTLP_ENDPOINT` is set; no-op otherwise.

## 5. Known Constraints & Tech Debt

- **The `LegacyFeedImporter` in `infrastructure/import_v1.py`** reads from the v1.x JSON-file-based format. It runs once per database and then becomes dead code; do not extend it. It will be deleted in v3.0.
- **No multi-user isolation at the database level.** Currently every user sees their own subscriptions via `WHERE user_id = ?` filters in repositories. We know this is fragile and a future redesign will move toward Postgres row-level security. Do not add new repository methods that bypass the user_id filter.
- **The deduplication algorithm uses GUID + SHA-256(title + content).** It is over-aggressive and occasionally drops legitimate updates to the same article (e.g. corrections). A "soft duplicate" mode is on the roadmap.
- **SQLite is supported for development only.** Production deployments MUST use PostgreSQL. The Alembic migrations are tested against both, but the polling job's row-level locking semantics differ subtly and have not been audited on SQLite.
- **CI runs only against PostgreSQL.** Local-only SQLite tests are run by developers; do not assume green CI means SQLite works.

---
*Generated by Agentic SDLC Forge — 2026-05-08*
*Review and edit this document as your project evolves.*
