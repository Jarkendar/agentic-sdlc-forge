# Git Flow Guidelines for AI Agents

This document defines **MANDATORY** Git workflow rules for all AI agents (Executor, Documentalist, etc.) working on this project. These rules are **NON-NEGOTIABLE** and **MUST** be followed without exception.

---

## 1. Branch Naming Convention

AI agents **MUST** use the following branch naming structure:

```
type/kebab-case-description
```

### Valid Types:
- `feature/` - for new features
- `bugfix/` - for bug fixes
- `refactor/` - for code refactoring
- `test/` - for adding or modifying tests
- `docs/` - for documentation changes
- `chore/` - for maintenance tasks

### Examples of CORRECT branch names:
- `feature/user-authentication`
- `bugfix/api-timeout-error`
- `refactor/database-connection-pool`
- `test/add-unit-tests-for-parser`

### **STRICTLY PROHIBITED** branch names:
- ❌ `fix`
- ❌ `update`
- ❌ `test`
- ❌ `changes`
- ❌ `new-feature`
- ❌ `bugfix` (without description)

**FORBIDDEN**: Creating branches with generic, non-descriptive names. Every branch **MUST** clearly indicate what it contains.

---

## 2. Conventional Commits Standard

All commit messages **MUST** follow the Conventional Commits specification. This is **NON-NEGOTIABLE**.

### Required Format:

```
<type>: <subject line - max 70 characters>


[mandatory detailed body - explain WHY and HOW]

[optional footer]
```

**CRITICAL REQUIREMENTS**:
- First line (subject): **MUST** be maximum 70 characters
- **MUST** have exactly TWO blank lines after subject line
- Body: **MUST** provide detailed explanation of changes
- All commit messages **MUST** be written in **ENGLISH**

### Valid Commit Types:
- `feat:` - new feature
- `fix:` - bug fix
- `refactor:` - code refactoring without changing functionality
- `test:` - adding or modifying tests
- `docs:` - documentation changes
- `chore:` - maintenance tasks (dependencies, build config, etc.)
- `style:` - code style changes (formatting, missing semicolons, etc.)
- `perf:` - performance improvements

### Examples of CORRECT commits:
```
feat: add user login endpoint


Implement POST /api/auth/login endpoint with JWT token generation.
Includes email/password validation and rate limiting.

Closes #123
```

```
fix: resolve null pointer exception in data parser


The parser was not handling empty input strings correctly, causing
NPE when processing malformed JSON. Added null checks and proper
error handling with descriptive error messages.
```

```
refactor: extract validation logic into separate module


Moved user input validation from controller to dedicated validator
class to improve code reusability and testability. No functional
changes to validation behavior.
```

### **STRICTLY PROHIBITED** commit messages:
- ❌ `fixed stuff`
- ❌ `updates`
- ❌ `changes`
- ❌ `wip`
- ❌ `test commit`
- ❌ Any commit without a conventional commit prefix
- ❌ Subject line longer than 70 characters
- ❌ Missing two blank lines after subject
- ❌ Missing detailed body explanation
- ❌ Commits in languages other than English

**FORBIDDEN**: Creating commits without proper type prefixes, vague descriptions, missing body, or incorrect formatting.

---

## 3. Atomic Commits

Every commit **MUST** be atomic - containing **ONLY ONE** logical change.

### Rules:
1. **MUST** commit one logical change at a time
2. **FORBIDDEN** to mix different types of changes in a single commit
3. **FORBIDDEN** to combine refactoring with new features
4. **FORBIDDEN** to include unrelated file changes

### Examples:

#### ✅ CORRECT (Atomic):
```
Commit 1: refactor: extract user validation into separate function
Commit 2: feat: add email notification on user registration
Commit 3: test: add unit tests for user validation
```

#### ❌ INCORRECT (Non-atomic):
```
Commit 1: feat: add email notification, refactor validation, fix typo in docs
```

**STRICTLY PROHIBITED**: Combining multiple unrelated changes in a single commit. Each commit **MUST** be independently reviewable and revertable.

---

## 4. Pre-Commit Verification

Before creating any commit, AI agents **MUST** perform the following checks:

### Mandatory Checks:
1. ✅ **Syntax Verification**: Code **MUST** be free of syntax errors
2. ✅ **Architecture Compliance**: Changes **MUST** comply with architecture rules defined in `.forge/` directory
3. ✅ **Formatting**: Code **MUST** follow project formatting standards
4. ✅ **No Debug Code**: **FORBIDDEN** to commit debug statements, console.log, print statements (unless intentional)
5. ✅ **No Commented Code**: **FORBIDDEN** to commit large blocks of commented-out code

### Verification Process:
1. Review all changed files
2. Verify syntax correctness
3. Check compliance with `.forge/architecture.md` (if exists)
4. Ensure commit message follows Conventional Commits
5. Verify branch name follows naming convention
6. Only then create the commit

**STRICTLY PROHIBITED**: Creating commits without performing these verification steps.

---

## 5. Commit Message Best Practices

### Subject Line Requirements:
- **MUST** be written in imperative mood ("add feature" not "added feature")
- **MUST** be maximum 70 characters
- **MUST** start with lowercase letter (after the type prefix)
- **MUST NOT** end with a period
- **MUST** be written in **ENGLISH**

### Body Requirements (MANDATORY):
- **MUST** be separated from subject by exactly TWO blank lines
- **MUST** explain **WHY** the change was made and **HOW** it works
- **MUST** provide context and reasoning
- **MUST** wrap at 72 characters per line
- **MUST** be written in **ENGLISH**
- The diff shows WHAT changed; the body explains WHY and HOW

### Footer (Optional):
- Reference issue numbers: `Closes #123`
- Note breaking changes: `BREAKING CHANGE: API endpoint renamed`

---

## 6. Merge Strategy

When merging branches:

1. **MUST** ensure all commits follow the rules above
2. **MUST** use meaningful merge commit messages
3. **FORBIDDEN** to squash commits that should remain separate for history clarity
4. **MUST** resolve conflicts carefully, preserving architectural integrity

---

## 7. Enforcement

These rules are **ABSOLUTE** and **NON-NEGOTIABLE**. Any violation **MUST** be corrected immediately.

AI agents **MUST**:
- Treat these rules as highest priority
- Never create commits or branches that violate these standards
- Self-verify compliance before any Git operation

**STRICTLY PROHIBITED**: Ignoring or bypassing any of these rules under any circumstances.

---

## Project-Specific Overrides

> **Note for Human Developers:**
> 
> You may add project-specific Git workflow requirements below this section. Any rules added here will take **ABSOLUTE PRIORITY** over the general rules defined above.
> 
> Examples of project-specific overrides:
> - Required linter execution before commit
> - Mandatory test suite execution
> - Specific commit message templates
> - Additional branch naming requirements
> - Pre-commit hooks that must pass
> 
> AI agents **MUST** treat any rules added in this section as **HIGHEST PRIORITY** and follow them without exception.

<!-- Add your project-specific Git workflow overrides below this line -->

