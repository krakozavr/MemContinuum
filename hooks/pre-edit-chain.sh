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
#                    $MEMCONTINUUM_HOME/config.sh (if it sets MEMCONTINUUM_PYTHON),
#                    then <engine>/.venv/bin/python (scripts/repo-init.sh
#                    --bootstrap-venv) when unset.
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
MEMCONTINUUM_HOME="${MEMCONTINUUM_HOME:-$HOME/.memcontinuum}"
# Python resolution order (matches hooks/memlib.sh; F6 fix, round 4):
#   $MEMCONTINUUM_PYTHON -> $MEMCONTINUUM_HOME/config.sh -> <engine>/.venv/bin/python
# (scripts/repo-init.sh --bootstrap-venv). Never a hard error here -- this hook fails
# open on every path (see the header comment above); a python that doesn't
# resolve just surfaces as a logged outcome below. The config.sh step is
# sourced (never sed/grep'd -- it is sourceable shell); a missing/corrupt
# config.sh is swallowed by `|| true` so it can only cost sourcing time, never
# block the hook.
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

# Project resolution moved ABOVE the no-python check (round-2 review
# finding, same move memlib.sh already made for its own twin diagnostic):
# depends only on env (MEMCONTINUUM_PROJECT / basename(MEMCONTINUUM_ROOT) /
# "default"), never on the payload, so it costs nothing to compute this
# early -- and memidx.py stats groups hook.log by project=, so this line
# needs one exactly like every other line does.
PROJECT="${MEMCONTINUUM_PROJECT:-}"
if [ -z "$PROJECT" ]; then
    if [ -n "${MEMCONTINUUM_ROOT:-}" ]; then
        PROJECT="$(basename "$MEMCONTINUUM_ROOT")"
    else
        PROJECT="default"
    fi
fi

if [ ! -x "$PY" ]; then
    printf '%s pre-edit-chain: no python resolved (checked MEMCONTINUUM_PYTHON, %s) -- run scripts/repo-init.sh --bootstrap-venv project=%s\n' \
        "$(date -Iseconds 2>/dev/null || date)" "$SCRIPT_DIR/../.venv/bin/python" "$PROJECT" >>"$LOG" 2>/dev/null || true
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

# PROJECT is already resolved above (moved ahead of the no-python check).

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
# Round-3 addendum (review finding): a candidate whose `for-path` call
# itself FAILED (RC != 0 -- a broken python, a corrupt db mid-write, any
# exec failure) was silently `continue`d past and, if every candidate
# failed the same way, fell straight through to the same `finish
# "no-match"` a genuine "queried fine, found nothing" result uses --
# indistinguishable in the log from real negative evidence. Track whether
# ANY candidate's query actually ran to completion; if none did, this
# was never really evaluated at all, so it gets its own distinct outcome.
MATCHED_CANDIDATE=""
RESULT_JSON=""
ANY_QUERY_SUCCEEDED=0
for candidate in "${CANDIDATES[@]}"; do
    RESULT_JSON="$(PYTHONPATH= "$PY" "$MEMIDX" for-path "$candidate" --project "$PROJECT" --db "$DB_PATH" --json 2>>"$LOG")"
    RC=$?
    if [ $RC -ne 0 ]; then
        continue
    fi
    ANY_QUERY_SUCCEEDED=1
    TRIMMED="$(printf '%s' "$RESULT_JSON" | tr -d '[:space:]')"
    if [ -n "$TRIMMED" ] && [ "$TRIMMED" != "[]" ]; then
        MATCHED_CANDIDATE="$candidate"
        break
    fi
done

if [ -z "$MATCHED_CANDIDATE" ]; then
    if [ "$ANY_QUERY_SUCCEEDED" -eq 0 ]; then
        finish "query-failed"
    fi
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
