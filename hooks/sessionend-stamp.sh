#!/usr/bin/env bash
# SessionEnd hook: stamps state.ended_at only. SessionEnd is never seen by
# the model and shares a 1.5s budget across every SessionEnd hook
# (docs/DESIGN.md, "facts that changed the design"), so this script
# does the absolute minimum: one locked, atomic state update, no memidx.py
# calls, no git calls, no stdout, and it must never block on anything.
#
# If no state file exists yet for this session_id, one is created holding
# just session_id/project/ended_at -- harmless either way, and keeps
# "state keyed by session_id" true even for a session that ends before its
# first SessionStart(startup) ever ran (should not normally happen, but
# costs nothing to handle).
#
# Env: see hooks/memlib.sh.

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"

# Watchdog guard (macOS port, docs/DESIGN.md SS8 port note, 2026-08-30;
# deduped into hooks/mc-watchdog.sh, finding 1, 2026-08-31): must be the
# literal first thing after resolving SCRIPT_DIR and sourcing
# mc-watchdog.sh (a fast, filesystem/subprocess-free variable assignment
# only -- see its own header), strictly BEFORE sourcing memlib.sh (which
# does its own mkdir -p work) -- see
# hooks/userprompt-remind.sh's test_outer_deadline_covers_memlib_sourcing
# for why this ordering matters. A tiny python launcher
# (mc-watchdog.sh's MC_WATCHDOG_LAUNCHER_PY) starts this same script as a
# child in its own process group and kills the WHOLE group on a
# wall-clock budget (2s here; see hooks/mc-watchdog.sh), so an orphaned
# grandchild (e.g. a hung python call several layers deep, or the
# launcher itself being killed -- finding 1's SIGTERM/SIGINT/atexit fix)
# cannot outlive the deadline. Every python call this script and
# memlib.sh's helpers make therefore needs no timeout of its own -- this
# one watchdog bounds the entire run. Always exits 0; stdin/stdout/stderr
# are the real, inherited file descriptors (never piped through python),
# so passthrough is unbuffered. If MEMCONTINUUM_PYTHON (or the venv
# fallback) does not resolve to an executable, or mc-watchdog.sh failed
# to source (MC_WATCHDOG_LAUNCHER_PY unset), this falls through
# UNGUARDED instead of exec-ing a dead path -- memlib.sh's own "no python
# resolved" detection then fires exactly as it would with no guard at
# all.
# shellcheck source=mc-watchdog.sh
source "${MC_WATCHDOG_LIB_PATH:-$SCRIPT_DIR/mc-watchdog.sh}" 2>/dev/null
# SessionEnd's own harness budget is 1.5s (see this file's own
# header) -- tighter than the shared 2s default, so it must not eat
# the whole harness window itself.
export MC_WATCHDOG_BUDGET=1.2
if [ -z "${MC_UNDER_TIMEOUT:-}" ]; then
    export MC_UNDER_TIMEOUT=1
    MC_GUARD_PY="${MEMCONTINUUM_PYTHON:-$SCRIPT_DIR/../.venv/bin/python}"
    if [ -x "$MC_GUARD_PY" ] && [ -n "${MC_WATCHDOG_LAUNCHER_PY:-}" ]; then
        "$MC_GUARD_PY" -c "$MC_WATCHDOG_LAUNCHER_PY" "${BASH:-bash}" "${BASH_SOURCE[0]}" "$@"
        exit 0
    fi
fi

# shellcheck source=memlib.sh
source "$SCRIPT_DIR/memlib.sh"

finish() {
    mc_log "sessionend outcome=$1 session=${SESSION_ID:-}"
    exit 0
}

PAYLOAD="$(cat)"
[ -z "$PAYLOAD" ] && finish "empty-payload"

eval "$(mc_extract_fields "$PAYLOAD" session_id)" 2>/dev/null
[ -z "${SESSION_ID:-}" ] && finish "no-session-id"

STATE_FILE="$(mc_state_file_for "$MC_PROJECT" "$SESSION_ID")"

export MC_SESSION_ID="$SESSION_ID"
export MC_PROJECT_ENV="$MC_PROJECT"
export MC_NOW="$(date +%s 2>/dev/null || echo 0)"

mc_update_state_json "$STATE_FILE" '
import os, time

state.setdefault("session_id", os.environ.get("MC_SESSION_ID", ""))
state.setdefault("project", os.environ.get("MC_PROJECT_ENV", ""))
try:
    state["ended_at"] = float(os.environ.get("MC_NOW") or time.time())
except Exception:
    state["ended_at"] = time.time()

print(json.dumps(state))
' >>"$MC_LOG" 2>&1

finish "stamped"
