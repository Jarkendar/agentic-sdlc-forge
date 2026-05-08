# Architecture Map — TripJournal

> Offline-first Android app for logging trips with photos and notes; designed for hikers and motorcyclists with intermittent connectivity.

## 1. Project Identity

- **Problem:** Existing trip-logging apps assume always-on connectivity, lose data when offline, and bury photo attachments behind paywalls. Hikers in remote areas need a journal that works on a phone with no signal for days at a time.
- **Target Users:** End-users (hikers, motorcycle tourers, overlanders). Single-user app — no social features, no cloud account by default.
- **Platform:** Android (minSdk 26, targetSdk 35)
- **Language:** Kotlin 2.0
- **Build System:** Gradle 8.10 with Kotlin DSL and version catalogs

## 2. Technology Stack

### Required Technologies

- **Jetpack Compose (BoM 2025.01.00)** — entire UI; no XML layouts anywhere.
- **Material 3** — design system; custom theme tokens in `ui/theme/`.
- **Hilt** — dependency injection across all layers.
- **Room 2.7** — local SQLite persistence; the source of truth.
- **DataStore Preferences** — user settings only (theme, default map provider).
- **kotlinx.coroutines + Flow** — async and reactive streams; UI consumes `StateFlow`.
- **Coil 3.x** — image loading from disk and Content URIs.
- **CameraX** — photo capture (separate from system camera intent for full control over EXIF and storage).
- **WorkManager** — background sync when connectivity returns.
- **Maps SDK for Android** — offline tile cache for trip routes.
- **JUnit 5 + MockK + Turbine + Compose UI Test** — testing.

### Anti-patterns — PROHIBITED

- **NEVER** use XML layouts. The entire UI is Compose.
- **NEVER** use LiveData. State exposure is `StateFlow` and `SharedFlow` only.
- **NEVER** use RxJava. Async is coroutines-only.
- **NEVER** use AsyncTask, Loaders, or any pre-Jetpack lifecycle helper.
- **NEVER** load images with Glide or Picasso. Coil only.
- **NEVER** put business logic in `@Composable` functions. Composables read state and emit events; logic lives in ViewModels and use cases.
- **NEVER** access Room from a Composable. Repository → use case → ViewModel → UI.
- **DO NOT** use `runBlocking` outside tests. Ever.
- **DO NOT** introduce inheritance for sharing UI logic. Use composition (small composables, slots, modifiers).

## 3. Architecture & Responsibilities

### Pattern

MVI (Model-View-Intent) on top of Clean Architecture layers. Every screen has a single `*ViewModel` exposing one `StateFlow<UiState>` and accepting `Intent` events via a single `onIntent(intent: Intent)` function.

### Module/Directory Structure

Multi-module project for build performance and enforceable dependencies.

```
:app                          # Single Activity, navigation host, theme
:core
  :core:domain                # Pure Kotlin: entities, use cases, repository interfaces
  :core:data                  # Room, repositories, mappers, network (none yet)
  :core:ui                    # Reusable composables, design tokens, preview helpers
  :core:testing               # Test fixtures, fake repositories
:feature
  :feature:tripList           # Browse trips
  :feature:tripDetail         # View one trip + entries
  :feature:entryEditor        # Create/edit a journal entry (text + photos)
  :feature:settings           # Theme, units, export
```

Modules in `:core:domain` have **zero Android dependencies** — pure Kotlin. They can be unit-tested without Robolectric or instrumentation.

### Data Flow

UI (Composable) → emits `Intent` → ViewModel → calls UseCase → UseCase calls RepositoryInterface (in `:core:domain`) → RepositoryImpl (in `:core:data`) reads from Room → Room emits `Flow<List<Entity>>` → mapper converts Entity → Domain model → ViewModel maps Domain → `UiState` → Composable recomposes.

### State Management

Per-screen `ViewModel` exposes `val uiState: StateFlow<TripDetailUiState>`. `UiState` is a sealed class:

```kotlin
sealed class TripDetailUiState {
    data object Loading : TripDetailUiState()
    data class Content(val trip: Trip, val entries: List<Entry>) : TripDetailUiState()
    data class Error(val message: String) : TripDetailUiState()
}
```

State is computed by combining flows from repositories with `combine { ... }.stateIn(viewModelScope, SharingStarted.WhileSubscribed(5000), Loading)`. ViewModels NEVER hold mutable state outside the `MutableStateFlow` they expose.

### Validation

Validation lives in **`:core:domain` use cases**. Input from the UI is mapped to use case params; the use case returns `Result<T, ValidationError>`. Composables receive validation errors via `UiState` and render them inline.

`ValidationError` is a sealed class per use case (e.g. `EntryEditorError.TitleTooShort`, `EntryEditorError.NoPhotosOrText`).

### Dependency Injection

Hilt with one `@Module` per layer:
- `DatabaseModule` (`@InstallIn(SingletonComponent)`) — Room database, DAOs.
- `RepositoryModule` — binds repository interfaces to implementations.
- `UseCaseModule` — provides use cases (factory-style, no scoping needed).

ViewModels use `@HiltViewModel` and `@Inject constructor(...)`.

### Error Handling

- Domain layer: `Result<T, DomainError>` (Arrow's `Either` is permitted but optional; vanilla sealed classes are the default).
- Data layer: catches Room exceptions, IOExceptions, and SQLiteConstraintExceptions; maps to `DomainError`. Never re-throws.
- UI layer: never catches anything. Errors arrive via `UiState.Error` and are rendered as snackbars or full-screen states.

### Layer Communication Rules

- `:core:domain` MUST NOT import anything Android-specific. Compile-time enforced — its `build.gradle.kts` applies `kotlin("jvm")` only.
- `:feature:*` modules MAY depend on `:core:domain` and `:core:ui`. They MUST NOT depend on `:core:data` directly — use cases are the boundary.
- `:feature:*` modules MUST NOT depend on each other. Cross-feature navigation goes through `:app`'s NavHost.
- `:app` is the only module that wires Hilt and starts Compose.

## 4. System Boundaries & Integrations

### External APIs

None in v1. Cloud sync is a v2 feature (placeholder `SyncRepository` interface exists in `:core:domain` but has no implementation).

### Local Storage

- **Room** — single source of truth for trips, entries, photos metadata, GPS tracks.
- **Internal storage** — photo files under `filesDir/photos/<entry_uuid>/<photo_uuid>.jpg`. Database stores file paths only.
- **DataStore Preferences** — user settings (theme, distance units, default map provider).

### Third-party SDKs

- **Google Maps SDK for Android** — map rendering and offline tile caching.
- **Firebase Crashlytics** — crash reporting (release builds only; debug builds disabled via build variant).
- **No analytics, no ads, no tracking SDKs.**

### Background Processing

WorkManager:
- `PhotoCompressionWorker` — compresses freshly captured photos in the background.
- `BackupWorker` — exports the database + photos to a user-selected directory once a week (user-configurable).

Workers are constrained on `RequiresCharging` and `RequiresDeviceIdle` to avoid eating battery in the field.

### Other Integrations

- **Storage Access Framework** for backup export and import — no MANAGE_EXTERNAL_STORAGE permission ever.
- **Location Services (FusedLocationProviderClient)** — GPS track recording. Permissions: ACCESS_FINE_LOCATION + ACCESS_BACKGROUND_LOCATION (the latter requested only when the user enables track recording).
- **CameraX** — for in-app photo capture.

## 5. Known Constraints & Tech Debt

- **No tablet-specific layouts yet.** Composables use `WindowSizeClass` heuristics but the app is portrait-phone first. Tablet polish is on the roadmap; do not add tablet-specific code paths without flagging.
- **`OldTripImporter` in `:core:data`** reads from the v0.x SQLite-based prototype. It is scheduled for removal in v1.5. Do not extend it.
- **Photo EXIF stripping is incomplete.** GPS tags in EXIF are stripped on capture, but other identifying tags (device model, software version) are not. A `ExifSanitizer` class exists but only covers GPS; finishing it is a known follow-up.
- **The map module currently leaks an Activity reference under configuration changes.** Tracked as issue #142. Do not duplicate the workaround pattern from `MapScreen` to other screens — it is wrong and being refactored.

---
*Generated by Agentic SDLC Forge — 2026-05-08*
*Review and edit this document as your project evolves.*
