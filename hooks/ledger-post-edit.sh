#!/usr/bin/env bash
# PostToolUse hook (matcher: Edit|Write|NotebookEdit): silently appends
# every edited file_path under the code root or the store root to this
# session's ledger ($MEMCONTINUUM_HOME/sessions/<project>/<session_id>.json).
# Never prints anything (PostToolUse additionalContext exists but this hook
# never uses it -- it is pure evidence-gathering, not a reminder point --
# docs/DESIGN.md ruling A/E). Runs identically inside a subagent
# (agent_id set): a subagent's edits are still real edits worth tracking,
# even though subagents never get injected reminders (that gate lives in
# userprompt-remind.sh / sessionstart-remind.sh, not here).
#
# Contract: read the PostToolUse JSON payload on stdin; never write anything
# under MEMCONTINUUM_ROOT or MEMCONTINUUM_CODE_ROOT; always exit 0 (fail-open);
# never emit any stdout; append one outcome line to hook.log.
#
# Look-back addendum (docs/DESIGN.md 2026-08-30): this is
# also the single place that advances state.last_growth_turn/last_growth_ts
# -- the "edit ledger grew" half of userprompt-remind.sh's T-thin formula.
# Growth means a genuinely NEW (path, content_sha256) pair enters the
# ledger: a brand-new path, or an existing path whose content changed.
# Re-touching a path with byte-identical content is NOT growth (matches the
# fingerprint semantics userprompt-remind.sh already uses for its own
# coverage-injection cooldown). Stamped at the ledger's own idea of "now"
# (turn = the session's current user_turn_count, which only
# userprompt-remind.sh ever advances -- this hook never bumps it).
#
# Env: see hooks/memlib.sh's own header comment (MEMCONTINUUM_HOME,
# MEMCONTINUUM_PROJECT, MEMCONTINUUM_ROOT, MEMCONTINUUM_CODE_ROOT, MEMCONTINUUM_PYTHON).

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

START_TS=$(date +%s 2>/dev/null || echo 0)

finish() {
    local outcome="$1"
    local now elapsed
    now=$(date +%s 2>/dev/null || echo "$START_TS")
    elapsed=$(( now - START_TS ))
    mc_log "ledger outcome=$outcome elapsed=${elapsed}s session=${SESSION_ID:-} file=${FILE_PATH:-}"
    exit 0
}

PAYLOAD="$(cat)"
[ -z "$PAYLOAD" ] && finish "empty-payload"

eval "$(mc_extract_fields "$PAYLOAD" session_id tool_input.file_path agent_id)" 2>/dev/null

[ -z "${SESSION_ID:-}" ] && finish "no-session-id"
[ -z "${FILE_PATH:-}" ] && finish "no-file-path"

UNDER_CODE=0
UNDER_STORE=0
if [ -n "${MEMCONTINUUM_CODE_ROOT:-}" ]; then
    case "$FILE_PATH" in
        "${MEMCONTINUUM_CODE_ROOT%/}"/*) UNDER_CODE=1 ;;
    esac
fi
if [ -n "${MEMCONTINUUM_ROOT:-}" ]; then
    case "$FILE_PATH" in
        "${MEMCONTINUUM_ROOT%/}"/*) UNDER_STORE=1 ;;
    esac
fi

if [ "$UNDER_CODE" -eq 0 ] && [ "$UNDER_STORE" -eq 0 ]; then
    finish "out-of-scope"
fi

KIND="code"
[ "$UNDER_STORE" -eq 1 ] && KIND="store"

STATE_FILE="$(mc_state_file_for "$MC_PROJECT" "$SESSION_ID")"

export MC_FILE_PATH="$FILE_PATH"
export MC_KIND="$KIND"
export MC_SESSION_ID="$SESSION_ID"
export MC_PROJECT_ENV="$MC_PROJECT"
export MC_NOW="$(date +%s 2>/dev/null || echo 0)"

mc_update_state_json "$STATE_FILE" '
import hashlib, os, time

path = os.environ.get("MC_FILE_PATH", "")
kind = os.environ.get("MC_KIND", "code")
try:
    now = float(os.environ.get("MC_NOW") or time.time())
except Exception:
    now = time.time()
if path:
    try:
        with open(path, "rb") as f:
            content_sha = hashlib.sha256(f.read()).hexdigest()
    except OSError:
        content_sha = ""

    ledger = state.setdefault("ledger", [])
    found = False
    previous_sha = None
    for entry in ledger:
        if entry.get("path") == path:
            found = True
            previous_sha = entry.get("content_sha256")
            entry["kind"] = kind
            entry["content_sha256"] = content_sha
            entry["seen_at"] = now
            break
    if not found:
        ledger.append({
            "path": path,
            "kind": kind,
            "content_sha256": content_sha,
            "seen_at": now,
        })

    is_new_pair = (not found) or (previous_sha != content_sha)
    if is_new_pair:
        state["last_growth_turn"] = state.get("user_turn_count", 0)
        state["last_growth_ts"] = now

    state["session_id"] = os.environ.get("MC_SESSION_ID") or state.get("session_id")
    state["project"] = os.environ.get("MC_PROJECT_ENV") or state.get("project")
    state.setdefault("user_turn_count", 0)
    state.setdefault("last_inject_turn", -999)
    state.setdefault("last_inject_time", 0)
    state.setdefault("last_inject_ts", 0)
    state.setdefault("last_injected_pairs", [])
    state.setdefault("last_growth_turn", 0)
    state.setdefault("lookback_count", 0)

print(json.dumps(state))
'
RC=$?

if [ $RC -eq 0 ]; then
    finish "appended kind=$KIND"
else
    finish "update-failed rc=$RC"
fi
