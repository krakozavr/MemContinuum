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
#                     $MEMCONTINUUM_HOME/config.sh (if it sets MEMCONTINUUM_PYTHON),
#                     then <engine>/.venv/bin/python (scripts/repo-init.sh
#                     --bootstrap-venv) when unset.

set -u
export PYTHONPATH=

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
MEMIDX="$SCRIPT_DIR/../memidx.py"
MEMCONTINUUM_HOME="${MEMCONTINUUM_HOME:-$HOME/.memcontinuum}"
# Python resolution order (matches hooks/memlib.sh; F6 fix, round 4):
#   $MEMCONTINUUM_PYTHON -> $MEMCONTINUUM_HOME/config.sh -> <engine>/.venv/bin/python
# (scripts/repo-init.sh --bootstrap-venv). A commit must never be blocked by this, so
# an unresolved python just surfaces as a logged rc below, same as any other
# reindex failure. The config.sh step is sourced (never sed/grep'd -- it is
# sourceable shell); a missing/corrupt config.sh is swallowed by `|| true` so
# it can only cost sourcing time, never block the commit.
# R2/R3 fix, round 4: the file just sourced above may be a POINTER (a
# custom-HOME install also writes a minimal config.sh at the fixed default
# path recording only the real MEMCONTINUUM_HOME -- memcontinuum-setup.sh
# "3. config"). If sourcing it just redefined MEMCONTINUUM_HOME to a
# DIFFERENT directory than the file we sourced, follow through and source
# the REAL config.sh too, so MEMCONTINUUM_PYTHON actually resolves there.
# Unconditional on MEMCONTINUUM_PYTHON already being set (R3): config.sh's
# own `if [ -z "${MEMCONTINUUM_PYTHON:-}" ]` guard keeps env/baked
# precedence for PYTHON either way; LOG below must still land under the
# real HOME even when PYTHON was already baked into the hook line.
MC_HOME_CONFIG_1="$MEMCONTINUUM_HOME/config.sh"
if [ -f "$MC_HOME_CONFIG_1" ]; then
    # shellcheck source=/dev/null
    . "$MC_HOME_CONFIG_1" 2>/dev/null || true
fi
# Re-default after every source: a damaged-but-sourceable config may have
# `unset MEMCONTINUUM_HOME`, and under `set -u` a bare expansion would
# kill the hook (regate round 2).
MEMCONTINUUM_HOME="${MEMCONTINUUM_HOME:-$HOME/.memcontinuum}"
if [ "$MEMCONTINUUM_HOME/config.sh" != "$MC_HOME_CONFIG_1" ] && [ -f "$MEMCONTINUUM_HOME/config.sh" ]; then
    # shellcheck source=/dev/null
    . "$MEMCONTINUUM_HOME/config.sh" 2>/dev/null || true
    MEMCONTINUUM_HOME="${MEMCONTINUUM_HOME:-$HOME/.memcontinuum}"
fi
unset MC_HOME_CONFIG_1
PY="${MEMCONTINUUM_PYTHON:-$SCRIPT_DIR/../.venv/bin/python}"
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
