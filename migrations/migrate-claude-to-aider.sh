#!/usr/bin/env bash
# Wrapper for migrate_claude_to_aider.py
# Defaults to current directory as project and $HOME for user-global.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="${SCRIPT_DIR}/migrate_claude_to_aider.py"

if [[ ! -f "$PY" ]]; then
    echo "error: cannot find $PY" >&2
    exit 1
fi

# Python 3.9+ required (uses PEP 604 types)
if ! command -v python3 >/dev/null; then
    echo "error: python3 not found" >&2
    exit 1
fi

PROJECT="${PROJECT:-$(pwd)}"
DRY_RUN=""
SKIP_HOME=""
EXTRA_ARGS=()

usage() {
    cat <<EOF
Usage: $(basename "$0") [options]

Migrates Claude Code configuration to Aider.

Options:
  -p, --project PATH   Project root (default: \$PWD)
  -n, --dry-run        Show what would happen, don't write anything
      --skip-home      Don't migrate ~/.claude/
      --home PATH      Use PATH instead of \$HOME (mostly for testing)
  -h, --help           Show this help

Environment:
  PROJECT              Same as --project

Output:
  <project>/CONVENTIONS.md
  <project>/.aider.conf.yml
  <project>/.aiderignore
  <project>/docs/claude-migration/              (archived unmappable items)
  <project>/docs/claude-migration/MIGRATION_REPORT.md
  ~/.aider.conf.yml                             (if not --skip-home)
  ~/.aider.conventions.md                       (if not --skip-home)
  ~/.config/aider/claude-migration/             (archived globals)
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        -p|--project) PROJECT="$2"; shift 2 ;;
        -n|--dry-run) DRY_RUN="--dry-run"; shift ;;
        --skip-home)  SKIP_HOME="--skip-home"; shift ;;
        --home)       EXTRA_ARGS+=(--home "$2"); shift 2 ;;
        -h|--help)    usage; exit 0 ;;
        *)            echo "unknown option: $1" >&2; usage; exit 2 ;;
    esac
done

echo "Migrating Claude Code -> Aider"
echo "  project: $PROJECT"
[[ -n "$DRY_RUN" ]] && echo "  mode:    dry-run"
[[ -n "$SKIP_HOME" ]] && echo "  skipping user-global (~/.claude/)"
echo

python3 "$PY" --project "$PROJECT" ${DRY_RUN:+$DRY_RUN} ${SKIP_HOME:+$SKIP_HOME} "${EXTRA_ARGS[@]}"
