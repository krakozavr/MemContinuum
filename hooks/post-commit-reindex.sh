#!/usr/bin/env bash
# git post-commit hook for the STORE repo (memory/, not this tool repo):
# reindexes the store's markdown into the derived SQLite index after every
# commit, so `search`/`chain`/`for-path` see the new content immediately
# instead of waiting for the next manual `reindex`.
#
# Symlink or copy this as .git/hooks/post-commit inside the store repo --
# see install-hooks.md.
#
# Env (same names/defaults as pre-edit-chain.sh):
#   MEMCONTINUUM_ROOT     the store's markdown root to reindex. Required --
#                     unlike the pre-edit hook, this one DOES pass it to
#                     memidx.py, as `reindex --root`.
#   MEMCONTINUUM_PROJECT  project namespace. Defaults to $(basename "$MEMCONTINUUM_ROOT"),
#                     else "default". The literal project-name default lives
#                     only in project wiring, never in this script.
#   MEMCONTINUUM_HOME     passed through to memidx.py unchanged; also where this
#                     script's own log line goes ($MEMCONTINUUM_HOME/hook.log).
#                     Defaults to ~/.memcontinuum, matching memidx.py's default.
#   MEMCONTINUUM_PYTHON   absolute path to the venv python. Falls back to
#                     <engine>/.venv/bin/python (scripts/repo-init.sh --bootstrap-venv)
#                     when unset.

set -u
export PYTHONPATH=

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
MEMIDX="$SCRIPT_DIR/../memidx.py"
# Python resolution order: $MEMCONTINUUM_PYTHON -> <engine>/.venv/bin/python
# (scripts/repo-init.sh --bootstrap-venv). A commit must never be blocked by this, so
# an unresolved python just surfaces as a logged rc below, same as any other
# reindex failure.
PY="${MEMCONTINUUM_PYTHON:-$SCRIPT_DIR/../.venv/bin/python}"
MEMCONTINUUM_HOME="${MEMCONTINUUM_HOME:-$HOME/.memcontinuum}"
LOG="$MEMCONTINUUM_HOME/hook.log"

mkdir -p "$MEMCONTINUUM_HOME" 2>/dev/null

if [ -z "${MEMCONTINUUM_ROOT:-}" ]; then
    printf '%s post-commit-reindex: MEMCONTINUUM_ROOT not set, skipping\n' "$(date -Iseconds 2>/dev/null || date)" >>"$LOG" 2>/dev/null || true
    exit 0
fi

PROJECT="${MEMCONTINUUM_PROJECT:-$(basename "$MEMCONTINUUM_ROOT")}"

# $EPOCHREALTIME is a bash 5-ism (unbound under `set -u` on macOS's stock
# bash 3.2); `date +%s` (whole seconds -- nothing downstream parses the
# elapsed value, so the lost sub-second precision costs nothing) is the
# portable substitute, matching BSD date (no `%N`) same as GNU date.
START_TS=$(date +%s 2>/dev/null || echo 0)
OUT="$(PYTHONPATH= "$PY" "$MEMIDX" reindex --root "$MEMCONTINUUM_ROOT" --project "$PROJECT" ${MEMCONTINUUM_HOME:+--db "$MEMCONTINUUM_HOME/$PROJECT.sqlite"} 2>&1)"
RC=$?
NOW_TS=$(date +%s 2>/dev/null || echo "$START_TS")
ELAPSED=$(( NOW_TS - START_TS ))

printf '%s post-commit-reindex: rc=%s elapsed=%ss project=%s root=%s :: %s\n' \
    "$(date -Iseconds 2>/dev/null || date)" "$RC" "$ELAPSED" "$PROJECT" "$MEMCONTINUUM_ROOT" "$OUT" >>"$LOG" 2>/dev/null || true

# A commit should never be blocked by a reindex failure.
exit 0
