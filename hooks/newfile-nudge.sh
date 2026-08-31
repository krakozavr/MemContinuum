#!/usr/bin/env bash
# PreToolUse hook (matcher: Write ONLY -- never Edit): the deterministic
# new-file nudge. pre-edit-chain.sh only ever fires on a path that
# ALREADY EXISTS (`for-path` looks up governance for an existing file) --
# a brand-new file has no existing-file governance to trip, so nothing
# reminds the agent to check the code index before writing it. This hook
# is the mirror-image trigger: fires ONLY when tool_input.file_path does
# NOT exist yet (and isn't a symlink), sits under a configured code root,
# and has an indexed source extension -- and injects one line pointing at
# the same check memory-search's SKILL.md already asks for, at the one
# moment it's most likely to be skipped.
#
# Deliberately separate from pre-edit-chain.sh, and deliberately minimal:
# no vector, no index read, no memidx.py call of any kind (unlike every
# other hook in this repo). Whether the code index is actually
# initialized/current is left to the agent to confirm via `code-search`
# itself -- this hook cannot know that without paying for a python+sqlite
# read on every single Write, for a question code-search's own `state`
# field (finding 1) already answers on demand.
#
# Contract:
#   - reads the PreToolUse JSON payload on stdin, extracts tool_input.file_path
#   - fires ONLY when: the path does not exist AND is not a symlink (a
#     broken symlink is `! -e` but IS `-L` -- must stay silent, not be
#     treated as new), is under MEMCONTINUUM_CODE_ROOT, and ends in an
#     indexed source extension
#   - any other case, or any failure -> exit 0, no stdout (never blocks
#     the write)
#   - always runs under hooks/mc-watchdog.sh's shared wall-clock watchdog
#     (the same guard block every write-side hook uses) even though this
#     hook's own logic never calls python for real work -- one shared
#     mechanism, not a second bespoke timeout story for the one hook that
#     happens to be fast
#   - logs exactly one outcome line per invocation to
#     $MEMCONTINUUM_HOME/hook.log; never writes anything else, anywhere
#
# Env:
#   MEMCONTINUUM_CODE_ROOT   the code root this hook watches for new files.
#                            Required for this hook to ever fire (no code
#                            root configured -> always silent).
#   MEMCONTINUUM_HOME        base dir for hook.log. Defaults to
#                            ~/.memcontinuum (memidx.py's own default).
#   MEMCONTINUUM_PYTHON      absolute path to the venv python, used ONLY
#                            for the shared watchdog launcher and (when jq
#                            isn't on PATH) JSON handling -- never to run
#                            memidx.py.

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"

# Watchdog guard (same pattern as every other hook here -- see
# userprompt-remind.sh's own guard-block comment for the full rationale):
# must be the literal first thing this script does after resolving
# SCRIPT_DIR and sourcing mc-watchdog.sh (a fast, filesystem/subprocess-
# free variable assignment only -- see its own header).
# shellcheck source=mc-watchdog.sh
source "${MC_WATCHDOG_LIB_PATH:-$SCRIPT_DIR/mc-watchdog.sh}" 2>/dev/null
if [ -z "${MC_UNDER_TIMEOUT:-}" ]; then
    export MC_UNDER_TIMEOUT=1
    MC_GUARD_PY="${MEMCONTINUUM_PYTHON:-$SCRIPT_DIR/../.venv/bin/python}"
    if [ -x "$MC_GUARD_PY" ] && [ -n "${MC_WATCHDOG_LAUNCHER_PY:-}" ]; then
        "$MC_GUARD_PY" -c "$MC_WATCHDOG_LAUNCHER_PY" "${BASH:-bash}" "${BASH_SOURCE[0]}" "$@"
        exit 0
    fi
fi

export PYTHONPATH=
PY="${MEMCONTINUUM_PYTHON:-$SCRIPT_DIR/../.venv/bin/python}"
MEMCONTINUUM_HOME="${MEMCONTINUUM_HOME:-$HOME/.memcontinuum}"
LOG="$MEMCONTINUUM_HOME/hook.log"
mkdir -p "$MEMCONTINUUM_HOME" 2>/dev/null || true

FILE_PATH=""

log() {
    # never let logging itself fail the hook
    printf '%s\n' "$1" >>"$LOG" 2>/dev/null || true
}

finish() {
    # $1 = one-word outcome for the log line; everything after stays 0.
    log "$(date -Iseconds 2>/dev/null || date) newfile-nudge outcome=$1 file=${FILE_PATH:-}"
    exit 0
}

# --- read + parse the payload -------------------------------------------
PAYLOAD="$(cat)"

if [ -n "$PAYLOAD" ]; then
    if command -v jq >/dev/null 2>&1; then
        FILE_PATH="$(printf '%s' "$PAYLOAD" | jq -r '.tool_input.file_path // empty' 2>/dev/null)"
    else
        FILE_PATH="$(printf '%s' "$PAYLOAD" | "$PY" -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(1)
print(d.get("tool_input", {}).get("file_path", "") or "")
' 2>/dev/null)"
    fi
fi

if [ -z "$FILE_PATH" ]; then
    finish "no-file-path"
fi

# `-e` is false for a broken symlink too, so `-L` must be checked
# separately -- a symlink (broken or not) is never "a new file".
if [ -e "$FILE_PATH" ] || [ -L "$FILE_PATH" ]; then
    finish "existing-or-symlink"
fi

# Same "indexed source extension" notion as memidx.py's LANG_EXTENSIONS --
# hardcoded here (this hook never imports/runs memidx.py) since today
# there is exactly one language; extend this list in lockstep with
# LANG_EXTENSIONS if that ever grows.
case "$FILE_PATH" in
    *.swift) ;;
    *) finish "not-indexed-extension" ;;
esac

CODE_ROOT="${MEMCONTINUUM_CODE_ROOT:-}"
if [ -z "$CODE_ROOT" ]; then
    finish "no-code-root-configured"
fi

# A plain lexical `case "$FILE_PATH" in "$CODE_ROOT"/*` prefix match (the
# original check here) is not enough to prove containment: it is fooled
# both by a literal `/../` traversal segment (which is textually "under"
# the root string while resolving to a sibling of it) and by a symlinked
# ancestor directory (every path segment textually under the root, but
# the real directory it names lives elsewhere). MEDIUM (2026-08-31
# review), fixed bash-3.2-safe with no external binaries beyond what this
# hook already uses:
#   1. reject any literal `/../` traversal segment (or a leading `../`)
#      outright, purely as a string -- a syntactic red flag regardless of
#      what it would resolve to.
#   2. canonicalize CODE_ROOT and the nearest EXISTING ancestor directory
#      of FILE_PATH (walking up via dirname -- FILE_PATH itself was
#      already confirmed not to exist above) via `cd ... && pwd -P`,
#      which resolves symlinks, and require that ancestor to sit under
#      the canonicalized root. Both `cd`+`pwd -P` and `dirname` are
#      already-used builtins/coreutils elsewhere in this hook family, so
#      this adds nothing new to the p95 budget beyond a handful of cheap
#      filesystem stats.
case "$FILE_PATH" in
    */../*|*/..|../*|..) finish "path-traversal" ;;
esac

CODE_ROOT_REAL="$(cd "$CODE_ROOT" 2>/dev/null && pwd -P)"
if [ -z "$CODE_ROOT_REAL" ]; then
    finish "code-root-unresolvable"
fi

ANCESTOR="$FILE_PATH"
while [ ! -d "$ANCESTOR" ]; do
    NEXT="$(dirname "$ANCESTOR")"
    if [ "$NEXT" = "$ANCESTOR" ]; then
        finish "no-existing-ancestor"
    fi
    ANCESTOR="$NEXT"
done
ANCESTOR_REAL="$(cd "$ANCESTOR" 2>/dev/null && pwd -P)"
if [ -z "$ANCESTOR_REAL" ]; then
    finish "ancestor-unresolvable"
fi

case "$ANCESTOR_REAL" in
    "$CODE_ROOT_REAL"|"$CODE_ROOT_REAL"/*) ;;
    *) finish "outside-code-root" ;;
esac

MESSAGE="New source file under ${CODE_ROOT} — confirm the code index is initialized/current, then run code-search; name relevant hits or say none."

export HOOK_MESSAGE="$MESSAGE"
OUTPUT_JSON="$(PYTHONPATH= "$PY" -c '
import json, os

print(json.dumps({
    "hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "additionalContext": os.environ["HOOK_MESSAGE"],
    }
}))
' 2>>"$LOG")"

if [ -z "$OUTPUT_JSON" ]; then
    finish "output-build-failed"
fi

printf '%s\n' "$OUTPUT_JSON"
finish "nudged"
