#!/usr/bin/env bash
# PreToolUse hook: before an Edit/Write touches a file, look up whether any
# decision-chain topic references it (memidx.py for-path) and, if so, inject
# the compressed chain view(s) as additionalContext.
#
# Contract (docs/DESIGN.md SS3.1, SS8):
#   - reads the PreToolUse JSON payload on stdin, extracts tool_input.file_path
#   - no match / any failure  -> exit 0, no stdout (never blocks the edit)
#   - match                   -> one JSON object on stdout:
#         {"hookSpecificOutput":{"hookEventName":"PreToolUse",
#                                  "additionalContext":"..."}}
#   - must complete in well under 1s; every run appends one timing line to
#     $MEMCONTINUUM_HOME/hook.log
#   - hard-clears PYTHONPATH itself (the hook environment trap, DECISION SS8):
#     PreToolUse hooks spawn shells that re-source .bashrc, which re-exports a
#     Windows-site-packages PYTHONPATH that breaks the venv's own packages.
#
# Env:
#   MEMCONTINUUM_ROOT     store markdown root; used ONLY to derive a default
#                    project name ($(basename "$MEMCONTINUUM_ROOT")) when
#                    MEMCONTINUUM_PROJECT is unset. NOT passed to memidx.py --
#                    `for-path` has no --root flag (verified: exit 2). See
#                    "engine request" below.
#   MEMCONTINUUM_PROJECT  project namespace passed to memidx.py --project.
#                    Defaults to $(basename "$MEMCONTINUUM_ROOT"), else "default"
#                    (memidx.py's own DEFAULT_PROJECT). The literal default
#                    the concrete project name belongs in project wiring, never here.
#   MEMCONTINUUM_HOME     passed through to memidx.py unchanged (it resolves its
#                    own index db path from this; see memidx.py --help).
#                    Also where this script's own hook.log lives. Defaults to
#                    ~/.memcontinuum, matching memidx.py's own default.
#   MEMCONTINUUM_PYTHON   absolute path to the venv python. Falls back to
#                    <engine>/.venv/bin/python (install.sh --bootstrap-venv)
#                    when unset.
#   MEMCONTINUUM_STRIP_PREFIX
#                    optional colon-separated list of absolute path prefixes
#                    to strip from tool_input.file_path when trying to match
#                    it against a topic's (repo-relative) code_refs. A real
#                    PreToolUse payload's file_path is always absolute while
#                    code_refs are written relative to a project checkout, so
#                    without this (or a matching cwd) nothing ever matches.
#                    Project-specific values belong in project wiring.
#
# Engine request (not applied here -- memidx.py is not modified by this
# change; recording it per DECISION's rule instead):
#   `for-path` matches a queried path against code_refs by exact/prefix/glob
#   only (memidx.py:code_ref_matches) -- there is no suffix match, so an
#   absolute file_path never matches a repo-relative code_ref on its own.
#   Consider either a `--root`/`--strip-prefix` option on `for-path` itself,
#   or a documented suffix-match mode, so callers don't need this multi-candidate
#   workaround.

set -u
export PYTHONPATH=

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
MEMIDX="$SCRIPT_DIR/../memidx.py"
# Python resolution order: $MEMCONTINUUM_PYTHON -> <engine>/.venv/bin/python
# (install.sh --bootstrap-venv). Never a hard error here -- this hook fails
# open on every path (see the header comment above); a python that doesn't
# resolve just surfaces as a logged outcome below.
PY="${MEMCONTINUUM_PYTHON:-$SCRIPT_DIR/../.venv/bin/python}"
MEMCONTINUUM_HOME="${MEMCONTINUUM_HOME:-$HOME/.memcontinuum}"
LOG="$MEMCONTINUUM_HOME/hook.log"

mkdir -p "$MEMCONTINUUM_HOME" 2>/dev/null
if [ ! -x "$PY" ]; then
    printf '%s pre-edit-chain: no python resolved (checked MEMCONTINUUM_PYTHON, %s) -- run install.sh --bootstrap-venv\n' \
        "$(date -Iseconds 2>/dev/null || date)" "$SCRIPT_DIR/../.venv/bin/python" >>"$LOG" 2>/dev/null || true
fi

# $EPOCHREALTIME is a bash 5-ism (unbound under `set -u` on macOS's stock
# bash 3.2); `date +%s` (whole seconds -- nothing downstream parses the
# elapsed value, so the lost sub-second precision costs nothing) is the
# portable substitute, matching BSD date (no `%N`) same as GNU date.
START_TS=$(date +%s 2>/dev/null || echo 0)

log() {
    # never let logging itself fail the hook
    printf '%s\n' "$1" >>"$LOG" 2>/dev/null || true
}

finish() {
    # $1 = one-word outcome for the log line; everything after stays 0.
    local outcome="$1"
    local now elapsed
    now=$(date +%s 2>/dev/null || echo "$START_TS")
    elapsed=$(( now - START_TS ))
    log "$(date -Iseconds 2>/dev/null || date) outcome=$outcome elapsed=${elapsed}s project=${PROJECT:-} file=${FILE_PATH:-}"
    exit 0
}

# --- read + parse the payload -------------------------------------------
PAYLOAD="$(cat)"

FILE_PATH=""
CWD=""
if [ -n "$PAYLOAD" ]; then
    if command -v jq >/dev/null 2>&1; then
        FILE_PATH="$(printf '%s' "$PAYLOAD" | jq -r '.tool_input.file_path // empty' 2>/dev/null)"
        CWD="$(printf '%s' "$PAYLOAD" | jq -r '.cwd // empty' 2>/dev/null)"
    else
        FILE_PATH="$(printf '%s' "$PAYLOAD" | "$PY" -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(1)
print(d.get("tool_input", {}).get("file_path", "") or "")
' 2>/dev/null)"
        CWD="$(printf '%s' "$PAYLOAD" | "$PY" -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(1)
print(d.get("cwd", "") or "")
' 2>/dev/null)"
    fi
fi

if [ -z "$FILE_PATH" ]; then
    finish "no-file-path"
fi

# --- project resolution ---------------------------------------------------
PROJECT="${MEMCONTINUUM_PROJECT:-}"
if [ -z "$PROJECT" ]; then
    if [ -n "${MEMCONTINUUM_ROOT:-}" ]; then
        PROJECT="$(basename "$MEMCONTINUUM_ROOT")"
    else
        PROJECT="default"
    fi
fi

# --- build candidate paths to try against for-path -------------------------
declare -a CANDIDATES=()
add_candidate() {
    local c="$1"
    [ -z "$c" ] && return
    for existing in "${CANDIDATES[@]:-}"; do
        [ "$existing" = "$c" ] && return
    done
    CANDIDATES+=("$c")
}

add_candidate "$FILE_PATH"

if [ -n "$CWD" ] && [ "${FILE_PATH#"$CWD"/}" != "$FILE_PATH" ]; then
    add_candidate "${FILE_PATH#"$CWD"/}"
fi

if [ -n "${MEMCONTINUUM_STRIP_PREFIX:-}" ]; then
    IFS=':' read -r -a PREFIXES <<<"$MEMCONTINUUM_STRIP_PREFIX"
    for prefix in "${PREFIXES[@]}"; do
        [ -z "$prefix" ] && continue
        if [ "${FILE_PATH#"$prefix"}" != "$FILE_PATH" ]; then
            add_candidate "${FILE_PATH#"$prefix"}"
        fi
    done
fi

# --- fail loudly (to the log only) when the index simply isn't there yet --
# memidx.py treats a missing db exactly like "no topics reference this path"
# (exit 0, empty result) -- indistinguishable from a healthy empty answer.
# That is the exact "silently dead forcing function" risk docs/DESIGN.md SS8
# names, so it gets its own outcome in the log instead of collapsing into
# outcome=no-match.
DB_PATH="$MEMCONTINUUM_HOME/$PROJECT.sqlite"
if [ ! -f "$DB_PATH" ]; then
    finish "index-missing db=$DB_PATH"
fi

# --- query memidx.py for-path for each candidate until one matches ---------
MATCHED_CANDIDATE=""
RESULT_JSON=""
for candidate in "${CANDIDATES[@]}"; do
    RESULT_JSON="$(PYTHONPATH= "$PY" "$MEMIDX" for-path "$candidate" --project "$PROJECT" --db "$DB_PATH" --json 2>>"$LOG")"
    RC=$?
    if [ $RC -ne 0 ]; then
        continue
    fi
    TRIMMED="$(printf '%s' "$RESULT_JSON" | tr -d '[:space:]')"
    if [ -n "$TRIMMED" ] && [ "$TRIMMED" != "[]" ]; then
        MATCHED_CANDIDATE="$candidate"
        break
    fi
done

if [ -z "$MATCHED_CANDIDATE" ]; then
    finish "no-match"
fi

TOPIC_COUNT="$(printf '%s' "$RESULT_JSON" | grep -c '"id":')"

# --- get the pretty chain-view text for the matched candidate --------------
CHAIN_TEXT="$(PYTHONPATH= "$PY" "$MEMIDX" for-path "$MATCHED_CANDIDATE" --project "$PROJECT" --db "$DB_PATH" 2>>"$LOG")"

if [ -z "$CHAIN_TEXT" ]; then
    finish "empty-chain-text"
fi

CITATION_REMINDER='CONSTRAINT only if authority is owner-verbatim/owner-ratified and status active; HOLD for evidence-bearing incidents; everything else is context.'
HEADER="Decision-chain memory: ${TOPIC_COUNT} topic(s) reference this file."

# --- assemble final JSON safely (newlines/quotes included) -----------------
export HOOK_HEADER="$HEADER"
export HOOK_CHAIN_TEXT="$CHAIN_TEXT"
export HOOK_CITATION="$CITATION_REMINDER"

OUTPUT_JSON="$(PYTHONPATH= "$PY" -c '
import json, os

ctx = "\n\n".join([
    os.environ["HOOK_HEADER"],
    os.environ["HOOK_CHAIN_TEXT"],
    os.environ["HOOK_CITATION"],
])
print(json.dumps({
    "hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "additionalContext": ctx,
    }
}))
' 2>>"$LOG")"

if [ -z "$OUTPUT_JSON" ]; then
    finish "output-build-failed"
fi

printf '%s\n' "$OUTPUT_JSON"
finish "matched"
