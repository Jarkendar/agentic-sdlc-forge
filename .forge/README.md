# Verification presets

Drop-in `[[verification.commands]]` snippets for common stacks. Copy the
relevant entries into your `.forge/config.toml` under the `[verification]`
section, then tweak paths and timeouts for your project.

| File | Stack | Tools |
|---|---|---|
| `python.toml` | Python | `pytest`, `ruff` |
| `kotlin-gradle.toml` | Kotlin (JVM) | Gradle, `detekt` |
| `android.toml` | Android | Gradle, `detekt`, AGP `lint` |
| `kmp.toml` | Kotlin Multiplatform | Gradle, `detekt`, `allTests` |
| `node-pnpm.toml` | Node.js | `eslint`, `tsc --noEmit`, `pnpm test` |

## How verification works

The Verifier runs each command in declaration order. The first failing
command short-circuits the rest — there's no point running tests if lint
already caught a syntax error.

`stage` is one of:

- `verify_lint` — fast static checks (linters, formatters)
- `verify_compile` — compilation / type checking
- `verify_build` — full build artifacts
- `verify_test` — actual test execution

## In the future

Stage 8's `forge init` will pick a preset interactively and generate the
`[verification]` section automatically. Until then, copy by hand.
