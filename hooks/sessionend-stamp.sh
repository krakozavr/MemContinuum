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
