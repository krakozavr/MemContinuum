#!/usr/bin/env bash
# PreCompact hook: recomputes coverage evidence from this session's ledger
# and persists it to state.pending for sessionstart-remind.sh (source=compact)
# to inject right after compaction completes.
#
# CRITICAL (docs/DESIGN.md, "Facts that changed the design"):
# PreCompact's stdout does NOT reach the working model -- it becomes compact
# *instructions* for the summarizer instead, and there is no PreCompact member
# of hookSpecificOutput. This script therefore prints ABSOLUTELY NOTHING to
# stdout on any path, never emits a `decision` field, and always exits 0
# (exit 2 would BLOCK compaction, which is forbidden here).
#
# What it does:
#   - loads this session's ledger from state
#   - for ledger entries under the code root: runs `memidx.py unmapped`
#     (self-healing: check -> reindex --no-embed on drift) to classify them
#   - for ledger entries under the store root with no code entries present:
#     runs a plain `memidx.py reindex --no-embed` directly, so store-only
#     edits still get reconciled into the index even with nothing to check
#     coverage for (ruling E: "store edits trigger RECONCILIATION, not
#     reminders")
#   - compares the code/store roots' current git HEAD against the session's
#     start_code_sha/start_store_sha (captured by sessionstart-remind.sh)
#   - writes the result to state.pending, replacing whatever was there
#
# Timeout: this whole script runs under a 2s watchdog (see the guard just
# below) instead of per-call `timeout 2`; a slow/hung python still leaves
# this hook exiting 0 well under Claude Code's own PreCompact timeout.
#
# Env: see hooks/memlib.sh.

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"

# Watchdog guard (macOS port, docs/DESIGN.md SS8 port note, 2026-08-30): must
# be the literal first thing after resolving SCRIPT_DIR, strictly BEFORE
# sourcing memlib.sh (which does its own mkdir -p work) -- see
# hooks/userprompt-remind.sh's test_outer_deadline_covers_memlib_sourcing for
# why this ordering matters. Replaces the old per-call `timeout 2 ...` wraps
# (macOS bash 3.2 ships neither `timeout` nor `flock`): a tiny python
# launcher starts this same script as a child in its own process group and
# kills the WHOLE group on a 2s wall-clock budget, so an orphaned grandchild
# (e.g. a hung python call several layers deep) cannot outlive the deadline.
# Every python call this script and memlib.sh's helpers make therefore needs
# no timeout of its own -- this one watchdog bounds the entire run. Always
# exits 0; stdin/stdout/stderr are the real, inherited file descriptors
# (never piped through python), so passthrough is unbuffered. If
# MEMCONTINUUM_PYTHON (or the venv fallback) does not resolve to an
# executable, this falls through UNGUARDED instead of exec-ing a dead path --
# memlib.sh's own "no python resolved" detection then fires exactly as it
# would with no guard at all.
if [ -z "${MC_UNDER_TIMEOUT:-}" ]; then
    export MC_UNDER_TIMEOUT=1
    MC_GUARD_PY="${MEMCONTINUUM_PYTHON:-$SCRIPT_DIR/../.venv/bin/python}"
    if [ -x "$MC_GUARD_PY" ]; then
        "$MC_GUARD_PY" -c '
import os, signal, subprocess, sys
# MC_WATCHDOG_LAUNCHER: runs the real hook script as a child in its own
# process group and enforces a 2s wall-clock budget for the whole run. On
# expiry, kills the entire group so an orphaned grandchild cannot outlive
# the deadline, then always exits 0.
try:
    proc = subprocess.Popen(sys.argv[1:], start_new_session=True)
except Exception:
    sys.exit(0)
try:
    proc.wait(timeout=2)
except subprocess.TimeoutExpired:
    pass
# Unconditional group sweep (not just on a timeout): a hung call several
# layers deep can background a detached descendant that inherits the
# real stdout/stderr fds, which would otherwise keep those pipes open
# past the point the main script logically finished, even though it
# exited on time. Reaping the whole group here, always, is the actual
# orphaned-grandchild fix -- killing the group only on the timeout branch
# still leaves this exact gap on the success path.
try:
    os.killpg(proc.pid, signal.SIGKILL)
except Exception:
    pass
try:
    proc.wait(timeout=1)
except Exception:
    pass
sys.exit(0)
' "${BASH:-bash}" "${BASH_SOURCE[0]}" "$@"
        exit 0
    fi
fi

# shellcheck source=memlib.sh
source "$SCRIPT_DIR/memlib.sh"

finish() {
    mc_log "precompact outcome=$1 session=${SESSION_ID:-}"
    exit 0
}

PAYLOAD="$(cat)"
[ -z "$PAYLOAD" ] && finish "empty-payload"

eval "$(mc_extract_fields "$PAYLOAD" session_id trigger)" 2>/dev/null
[ -z "${SESSION_ID:-}" ] && finish "no-session-id"

STATE_FILE="$(mc_state_file_for "$MC_PROJECT" "$SESSION_ID")"
[ -f "$STATE_FILE" ] || finish "no-state"

# --- pull what we need out of state (ledger, start SHAs) without mutating it --
READ_TMP="$(mktemp 2>/dev/null)" || finish "mktemp-failed"
trap 'rm -f "$READ_TMP" "${PENDING_TMP:-}" 2>/dev/null' EXIT

env PYTHONPATH= "$MC_PY" -c '
import json, sys
try:
    with open(sys.argv[1]) as f:
        state = json.load(f)
    if not isinstance(state, dict):
        state = {}
except Exception:
    state = {}
ledger = state.get("ledger") or []
code_paths = sorted({e["path"] for e in ledger if e.get("kind") == "code" and e.get("path")})
store_touched = any(e.get("kind") == "store" for e in ledger)
out = {
    "code_paths": code_paths,
    "store_touched": store_touched,
    "start_code_sha": state.get("start_code_sha", ""),
    "start_store_sha": state.get("start_store_sha", ""),
}
with open(sys.argv[2], "w") as f:
    json.dump(out, f)
' "$STATE_FILE" "$READ_TMP" >>"$MC_LOG" 2>&1
if [ ! -s "$READ_TMP" ]; then
    finish "read-failed"
fi

read_field() {
    env PYTHONPATH= "$MC_PY" -c '
import json, sys
try:
    with open(sys.argv[1]) as f:
        d = json.load(f)
except Exception:
    d = {}
v = d.get(sys.argv[2])
print(json.dumps(v) if not isinstance(v, str) else v)
' "$READ_TMP" "$1" 2>>"$MC_LOG"
}

STORE_TOUCHED="$(read_field store_touched)"
START_CODE_SHA="$(read_field start_code_sha)"
START_STORE_SHA="$(read_field start_store_sha)"

# bash 3.2 has no `mapfile`/`readarray` -- process substitution (never a
# pipe, which would run the loop in a subshell and drop the assignments)
# feeding a plain while-read loop is the portable equivalent.
CODE_PATHS=()
while IFS= read -r p; do
    CODE_PATHS+=("$p")
done < <(env PYTHONPATH= "$MC_PY" -c '
import json, sys
with open(sys.argv[1]) as f:
    d = json.load(f)
for p in d.get("code_paths", []):
    print(p)
' "$READ_TMP" 2>>"$MC_LOG")

# --- store-only reconciliation (no code paths to classify) ------------------
if [ "${#CODE_PATHS[@]}" -eq 0 ]; then
    if [ "$STORE_TOUCHED" = "true" ] && [ -n "${MEMCONTINUUM_ROOT:-}" ]; then
        env PYTHONPATH= "$MC_PY" "$MC_MEMIDX" reindex \
            --root "$MEMCONTINUUM_ROOT" --project "$MC_PROJECT" --db "$MC_DB_PATH" \
            --no-embed >>"$MC_LOG" 2>&1
    fi
fi

# --- classify code-root edits via the self-healing `unmapped` engine call ---
UNMAPPED_JSON="{}"
if [ "${#CODE_PATHS[@]}" -gt 0 ] && [ -n "${MEMCONTINUUM_ROOT:-}" ]; then
    ARGS=(unmapped)
    ARGS+=("${CODE_PATHS[@]}")
    ARGS+=(--root "$MEMCONTINUUM_ROOT" --project "$MC_PROJECT" --db "$MC_DB_PATH" --json)
    [ -n "${MEMCONTINUUM_CODE_ROOT:-}" ] && ARGS+=(--code-root "$MEMCONTINUUM_CODE_ROOT")
    RAW="$(env PYTHONPATH= "$MC_PY" "$MC_MEMIDX" "${ARGS[@]}" 2>>"$MC_LOG")"
    RC=$?
    if [ $RC -eq 0 ] && [ -n "$RAW" ]; then
        UNMAPPED_JSON="$RAW"
    fi
fi

# --- git HEAD comparison -----------------------------------------------------
CUR_CODE_SHA="$(mc_git_head "${MEMCONTINUUM_CODE_ROOT:-}")"
CUR_STORE_SHA="$(mc_git_head "${MEMCONTINUUM_ROOT:-}")"

if [ -n "$CUR_CODE_SHA" ] && [ "$CUR_CODE_SHA" != "$START_CODE_SHA" ]; then
    CODE_CHANGED="true"
else
    CODE_CHANGED="false"
fi
if [ -n "$CUR_STORE_SHA" ] && [ "$CUR_STORE_SHA" != "$START_STORE_SHA" ]; then
    STORE_CHANGED="true"
else
    STORE_CHANGED="false"
fi

export MC_UNMAPPED_JSON="$UNMAPPED_JSON"
export MC_CODE_CHANGED="$CODE_CHANGED"
export MC_STORE_CHANGED="$STORE_CHANGED"

mc_update_state_json "$STATE_FILE" '
import json, os, time

try:
    unmapped_out = json.loads(os.environ.get("MC_UNMAPPED_JSON") or "{}")
    if not isinstance(unmapped_out, dict):
        unmapped_out = {}
except Exception:
    unmapped_out = {}

pending = {
    "unmapped": unmapped_out.get("unmapped", []),
    "mapped_topic_count": len(unmapped_out.get("mapped_topic", []) or []),
    "mapped_concept_only_count": len(unmapped_out.get("mapped_concept_only", []) or []),
    "coverage_status": unmapped_out.get("coverage_status", "unknown"),
    "code_head_changed": os.environ.get("MC_CODE_CHANGED") == "true",
    "store_head_changed": os.environ.get("MC_STORE_CHANGED") == "true",
    "computed_at": time.time(),
    # Look-back addendum (docs/DESIGN.md 2026-08-30):
    # sessionstart-remind.sh(compact) needs these frozen at PreCompact time
    # (not re-read live post-compaction) to decide its look-back twin.
    "user_turn_count": state.get("user_turn_count", 0),
    "last_growth_turn": state.get("last_growth_turn", 0),
}
state["pending"] = pending
print(json.dumps(state))
' >>"$MC_LOG" 2>&1

finish "computed code_paths=${#CODE_PATHS[@]} store_touched=$STORE_TOUCHED"
