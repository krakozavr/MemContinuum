#!/usr/bin/env bash
# memlib.sh -- shared helpers for MemContinuum's write-side reminder hooks
# (ledger-post-edit.sh, precompact-persist.sh, sessionstart-remind.sh,
# userprompt-remind.sh, sessionend-stamp.sh). Source this from each hook
# script; it is never executed standalone.
#
# Contract mirrors pre-edit-chain.sh (docs/DESIGN.md SS8 / docs/DESIGN.md
# ruling F): hard-clear PYTHONPATH, absolute venv python, MEMCONTINUUM_* env,
# a single hook.log, fail-open on every path. Every hook that sources this is
# still individually responsible for its own final `exit 0` -- memlib.sh never
# exits or traps on the caller's behalf.
#
# Env (project-agnostic; concrete values belong only in project wiring, e.g.
# .claude/settings.json or a *.json.example next to it -- never in this repo):
#   MEMCONTINUUM_HOME        base dir for the index db, hook.log, and session
#                        state ($MEMCONTINUUM_HOME/sessions/<project>/<id>.json).
#                        Defaults to ~/.memcontinuum (memidx.py's own default).
#   MEMCONTINUUM_PROJECT     project namespace passed to memidx.py --project.
#                        Defaults to $(basename "$MEMCONTINUUM_ROOT"), else
#                        "default" (memidx.py's own DEFAULT_PROJECT).
#   MEMCONTINUUM_ROOT        store markdown root (the decision-chain repo).
#   MEMCONTINUUM_CODE_ROOT   code root these hooks watch edits under.
#   MEMCONTINUUM_PYTHON      absolute path to the venv python. Falls back to
#                        <engine>/.venv/bin/python (see install.sh
#                        --bootstrap-venv) when unset.
#
# WRITE-LOCK (ruling E): these scripts' only writable surface is
# $MEMCONTINUUM_HOME/sessions/**/*.json[.lock] and $MEMCONTINUUM_HOME/hook.log.
# Never write anything under MEMCONTINUUM_ROOT (the store) or
# MEMCONTINUUM_CODE_ROOT (the code tree) from any function in this file or any
# script that sources it.

export PYTHONPATH=

MC_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
MC_MEMIDX="$MC_LIB_DIR/../memidx.py"
# Python resolution order (README.md "Requirements" / install.sh --bootstrap-venv):
#   $MEMCONTINUUM_PYTHON -> <engine>/.venv/bin/python -> (left unresolved; every
#   caller here fails open, so a missing python surfaces as a logged outcome,
#   never a blocked hook -- see mc_log below and each script's own finish()).
if [ -n "${MEMCONTINUUM_PYTHON:-}" ]; then
    MC_PY="$MEMCONTINUUM_PYTHON"
else
    MC_PY="$MC_LIB_DIR/../.venv/bin/python"
fi
MEMCONTINUUM_HOME="${MEMCONTINUUM_HOME:-$HOME/.memcontinuum}"
MC_LOG="$MEMCONTINUUM_HOME/hook.log"
mkdir -p "$MEMCONTINUUM_HOME" 2>/dev/null || true

if [ ! -x "$MC_PY" ]; then
    printf '%s memlib: no python resolved (checked MEMCONTINUUM_PYTHON, %s) -- run install.sh --bootstrap-venv\n' \
        "$(date -Iseconds 2>/dev/null || date)" "$MC_LIB_DIR/../.venv/bin/python" >>"$MC_LOG" 2>/dev/null || true
fi

MC_PROJECT="${MEMCONTINUUM_PROJECT:-}"
if [ -z "$MC_PROJECT" ]; then
    if [ -n "${MEMCONTINUUM_ROOT:-}" ]; then
        MC_PROJECT="$(basename "$MEMCONTINUUM_ROOT")"
    else
        MC_PROJECT="default"
    fi
fi

MC_DB_PATH="$MEMCONTINUUM_HOME/$MC_PROJECT.sqlite"

# MC_TIMEOUT_FG -- when a caller has already wrapped its own whole
# invocation in an outer `timeout` (MC_UNDER_TIMEOUT=1 -- currently only
# userprompt-remind.sh does this, dual-gate review finding 3), every
# *nested* `timeout` call below must run with --foreground so it does not
# escape into its own process group. Without this, the outer timeout's
# kill-the-group cannot reach (and reap) a nested timeout's own
# descendants: an orphaned grandchild can keep running -- and keep the
# caller's captured stdout/stderr pipes open -- long past the outer
# deadline (verified empirically: a nested `timeout 2` without
# --foreground let a killed call's own child run to its full natural
# duration instead of ~2s). Empty (default, group-creating) `timeout`
# behavior is unchanged for every other caller, which is exactly what
# lets a LONE `timeout` call still correctly kill its own descendants.
MC_TIMEOUT_FG=""
[ -n "${MC_UNDER_TIMEOUT:-}" ] && MC_TIMEOUT_FG="--foreground"

# mc_log MESSAGE -- append one timestamped line to hook.log. Never fails the
# calling hook (logging failure is swallowed, not propagated).
mc_log() {
    printf '%s %s\n' "$(date -Iseconds 2>/dev/null || date)" "$1" >>"$MC_LOG" 2>/dev/null || true
}

# mc_state_dir_for PROJECT
mc_state_dir_for() {
    printf '%s/sessions/%s' "$MEMCONTINUUM_HOME" "$1"
}

# mc_state_file_for PROJECT SESSION_ID
mc_state_file_for() {
    printf '%s/%s.json' "$(mc_state_dir_for "$1")" "$2"
}

# mc_extract_fields PAYLOAD_JSON FIELD...
# Reads PAYLOAD_JSON on stdin (never argv, never an env var -- see dual-gate
# review finding 1 below) and prints one shlex-quoted `NAME=value` line per
# requested field,
# suitable for `eval "$(mc_extract_fields ...)"`. Supported field tokens:
# any top-level payload key (uppercased for the shell var name), plus the
# special "tool_input.file_path" -> FILE_PATH, "_prompt_hash" -> PROMPT_HASH
# (sha256(payload["prompt_id"])[:16] -- the raw prompt_id itself is never
# extracted or emitted, dual-gate review finding 2), and "_top_keys_csv" ->
# TOP_KEYS_CSV (sorted top-level KEY NAMES only, comma-joined, never
# values -- the payload-shape capture addendum). Never reads
# transcript_path, user_input, last_assistant_message, or any payload VALUE
# beyond an explicitly requested scalar field on purpose -- callers must
# not ask for them (docs/DESIGN.md ruling B). The payload is
# piped to this one python's stdin only -- never placed in an env var or
# another process's argv (dual-gate review finding 1).
mc_extract_fields() {
    local payload="$1"
    shift
    printf '%s' "$payload" | timeout $MC_TIMEOUT_FG 2 env PYTHONPATH= "$MC_PY" -c '
import hashlib, json, sys, shlex
fields = sys.argv[1:]
try:
    d = json.load(sys.stdin)
except Exception:
    d = {}
if not isinstance(d, dict):
    d = {}
for f in fields:
    if f == "tool_input.file_path":
        v = (d.get("tool_input") or {}).get("file_path") or ""
        name = "FILE_PATH"
    elif f == "_prompt_hash":
        pid = d.get("prompt_id") or ""
        v = hashlib.sha256(pid.encode()).hexdigest()[:16] if pid else ""
        name = "PROMPT_HASH"
    elif f == "_top_keys_csv":
        v = ",".join(sorted(d.keys()))
        name = "TOP_KEYS_CSV"
    else:
        v = d.get(f)
        if v is None:
            v = ""
        name = f.upper()
    print(f"{name}={shlex.quote(str(v))}")
' "$@" 2>>"$MC_LOG"
}

# mc_git_head DIR -- read-only; empty string if DIR is missing or not a repo.
# Never mutates DIR.
mc_git_head() {
    local dir="$1"
    [ -n "$dir" ] && [ -d "$dir" ] || { printf ''; return; }
    timeout $MC_TIMEOUT_FG 2 git -C "$dir" rev-parse HEAD 2>>"$MC_LOG" || printf ''
}

# mc_update_state_json STATE_FILE PY_TRANSFORM
#
# The one shared "lock + atomic-rename JSON update" primitive. Acquires an
# flock on STATE_FILE.lock (2s timeout), loads the existing STATE_FILE (or
# {} if missing/corrupt/unreadable) and pipes it to the transform python's
# STDIN -- never an exported env var, and never argv -- so a legacy or
# otherwise-sensitive key already sitting in a caller's persisted state
# (e.g. a pre-fingerprint-scheme raw prompt_id) is never inherited by any
# subprocess that python spawns (re-gate finding, HIGH; the same class of
# bug as dual-gate review finding 1 above, but for the EXISTING state
# rather than the incoming payload). Runs
# PY_TRANSFORM against it (which may read any MC_*-prefixed env var the
# caller exported beforehand, and must end by printing the new, complete
# state object via `print(json.dumps(state))` -- or print nothing / exit
# non-zero to make this a no-op write), and atomically renames a temp file
# onto STATE_FILE. Returns the transform's exit code; a caller that also
# needs a *value* out of the transform (not just the persisted state) should
# have the transform write that value to a side-channel file of its own
# choosing (e.g. one named by an MC_*_OUT env var) and read it back itself
# afterward -- this function's own stdout/return value carries no such value.
mc_update_state_json() {
    local state_file="$1"
    local py_transform="$2"
    local state_dir
    state_dir="$(dirname "$state_file")"
    mkdir -p "$state_dir" 2>/dev/null || { mc_log "outcome=state-dir-failed dir=$state_dir"; return 1; }

    local lockfile="$state_file.lock"
    exec 9>"$lockfile" 2>/dev/null || { mc_log "outcome=lock-open-failed file=$lockfile"; return 1; }
    if ! flock -w 2 9; then
        mc_log "outcome=lock-timeout file=$lockfile"
        exec 9>&- 2>/dev/null || true
        return 1
    fi

    local existing="{}"
    if [ -f "$state_file" ]; then
        existing="$(cat "$state_file" 2>/dev/null)"
        [ -z "$existing" ] && existing="{}"
    fi

    local new_json rc
    new_json="$(printf '%s' "$existing" | timeout $MC_TIMEOUT_FG 2 env PYTHONPATH= "$MC_PY" -c "
import json, os, sys
try:
    state = json.loads(sys.stdin.read() or '{}')
    if not isinstance(state, dict):
        state = {}
except Exception:
    state = {}
$py_transform
" 2>>"$MC_LOG")"
    rc=$?

    if [ $rc -eq 0 ] && [ -n "$new_json" ]; then
        local tmp="$state_file.tmp.$$"
        if printf '%s' "$new_json" >"$tmp" 2>/dev/null; then
            mv -f "$tmp" "$state_file" 2>/dev/null || rm -f "$tmp" 2>/dev/null
        fi
    fi

    exec 9>&- 2>/dev/null || true
    return $rc
}

# mc_prune_old_state PROJECT MINUTES -- deletes *.json state files older than
# MINUTES under $MEMCONTINUUM_HOME/sessions/PROJECT (mtime-based; a state
# file's mtime is its last write, i.e. its last activity). Never touches any
# other project's directory, and never touches MEMCONTINUUM_ROOT/CODE_ROOT.
mc_prune_old_state() {
    local project="$1"
    local minutes="$2"
    local dir
    dir="$(mc_state_dir_for "$project")"
    [ -d "$dir" ] || return 0
    find "$dir" -maxdepth 1 -type f -name '*.json' -mmin "+$minutes" -exec rm -f {} + 2>/dev/null || true
}
