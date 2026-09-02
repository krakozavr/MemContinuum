#!/usr/bin/env bash
# SessionStart hook. Behavior branches on the payload's `source` field
# in-script (SessionStart supports a matcher on `source` at the settings
# level too, but docs/DESIGN.md requires in-script gating as the
# defense of record):
#
#   startup | resume  -- initialize this session's state (start_code_sha /
#                         start_store_sha captured from the code/store roots'
#                         current git HEAD, via setdefault so a *resume* never
#                         resets a startup's original values), then prune
#                         state files older than 24h across this project.
#                         Always silent (no stdout).
#   compact           -- read state.pending (written by precompact-persist.sh
#                         right before compaction), and if it holds anything,
#                         inject it ONCE via hookSpecificOutput.additionalContext
#                         (SessionStart's additionalContext is the first thing
#                         the model sees post-compaction). Consumes (clears)
#                         pending afterward so a later resume-after-compact
#                         doesn't re-inject the same evidence. If coverage has
#                         no evidence AND pending's snapshotted
#                         (user_turn_count - last_growth_turn) >= 3, injects
#                         the look-back compact twin instead (WRITE-HOOKS-
#                         CONSENSUS.md addendum 2026-08-30) -- coverage always
#                         wins when it has evidence; the twin ignores the
#                         cooldown entirely (one-shot loss point), and stamps
#                         last_inject_turn/last_inject_ts on firing so the
#                         very next per-turn hook call doesn't immediately
#                         re-fire on the same stretch.
#   anything else      -- no-op, silent, logged only.
#
# Wording: "Coverage signal" / "Look-back signal" fact line + exactly one
# closing question, never an imperative, never "unrecorded", never a
# suggested topic name -- and NEVER a coverage-gap count when the underlying
# index was stale (never a false gap, docs/DESIGN.md ruling F).
#
# Env: see hooks/memlib.sh.

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"

# Watchdog guard (macOS port, docs/DESIGN.md SS8 port note, 2026-08-30;
# deduped into hooks/mc-watchdog.sh, finding 1, 2026-08-31): must be the
# literal first thing after resolving SCRIPT_DIR and sourcing
# mc-watchdog.sh (see its own header for what
# running it costs), strictly BEFORE sourcing memlib.sh (which
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
if [ -z "${MC_UNDER_TIMEOUT:-}" ]; then
    export MC_UNDER_TIMEOUT=1
    # MC_GUARD_PY is set by mc-watchdog.sh above (F6 fix, round 4: env ->
    # config.sh -> engine venv, same order memlib.sh uses for MC_PY).
    if [ -x "${MC_GUARD_PY:-}" ] && [ -n "${MC_WATCHDOG_LAUNCHER_PY:-}" ]; then
        "$MC_GUARD_PY" -c "$MC_WATCHDOG_LAUNCHER_PY" "${BASH:-bash}" "${BASH_SOURCE[0]}" "$@"
        exit 0
    fi
fi

# shellcheck source=memlib.sh
source "$SCRIPT_DIR/memlib.sh"

finish() {
    local extra="${2:-}"
    if [ -n "$extra" ]; then
        mc_log "sessionstart outcome=$1 $extra session=${SESSION_ID:-} source=${SOURCE:-}"
    else
        mc_log "sessionstart outcome=$1 session=${SESSION_ID:-} source=${SOURCE:-}"
    fi
    exit 0
}

PAYLOAD="$(cat)"
[ -z "$PAYLOAD" ] && finish "empty-payload"

eval "$(mc_extract_fields "$PAYLOAD" session_id source)" 2>/dev/null
[ -z "${SESSION_ID:-}" ] && finish "no-session-id"

STATE_FILE="$(mc_state_file_for "$MC_PROJECT" "$SESSION_ID")"

case "${SOURCE:-}" in
    startup|resume)
        CODE_SHA="$(mc_git_head "${MEMCONTINUUM_CODE_ROOT:-}")"
        STORE_SHA="$(mc_git_head "${MEMCONTINUUM_ROOT:-}")"
        export MC_CODE_SHA="$CODE_SHA"
        export MC_STORE_SHA="$STORE_SHA"
        export MC_SESSION_ID="$SESSION_ID"
        export MC_PROJECT_ENV="$MC_PROJECT"

        mc_update_state_json "$STATE_FILE" '
import os, time

state.setdefault("session_id", os.environ.get("MC_SESSION_ID", ""))
state.setdefault("project", os.environ.get("MC_PROJECT_ENV", ""))
state.setdefault("start_code_sha", os.environ.get("MC_CODE_SHA", ""))
state.setdefault("start_store_sha", os.environ.get("MC_STORE_SHA", ""))
state.setdefault("created_at", time.time())
state.setdefault("ledger", [])
state.setdefault("user_turn_count", 0)
state.setdefault("last_inject_turn", -999)
state.setdefault("last_inject_time", 0)
state.setdefault("last_inject_ts", 0)
state.setdefault("last_injected_pairs", [])
# Look-back addendum (docs/DESIGN.md 2026-08-30):
# last_growth_turn baselines to turn 0 (session start, in turn-space);
# last_growth_ts baselines to this same moment in wall-clock terms
# (created_at), never to epoch 0 -- an epoch-0 default would make the
# 20-minute branch fire on turn 1 of every session.
state.setdefault("last_growth_turn", 0)
state.setdefault("last_growth_ts", state["created_at"])
state.setdefault("lookback_count", 0)

print(json.dumps(state))
' >>"$MC_LOG" 2>&1

        mc_prune_old_state "$MC_PROJECT" 1440

        finish "init"
        ;;

    compact)
        [ -f "$STATE_FILE" ] || finish "compact-no-state"

        OUTPUT_JSON="$(MEMCONTINUUM_ROOT="${MEMCONTINUUM_ROOT:-}" env PYTHONPATH= "$MC_PY" -c '
import json, os, sys

try:
    with open(sys.argv[1]) as f:
        state = json.load(f)
    if not isinstance(state, dict):
        state = {}
except Exception:
    state = {}

pending = state.get("pending") or {}
if not pending:
    sys.exit(3)

unmapped = pending.get("unmapped") or []
coverage_status = pending.get("coverage_status", "unknown")
code_changed = pending.get("code_head_changed")
store_changed = pending.get("store_head_changed")


def yn(v):
    if v is None:
        return "unknown"
    return "yes" if v else "no"


if coverage_status != "ok":
    fact_line = (
        "Coverage signal — decision-topic coverage unknown (store index "
        f"stale); code HEAD changed: {yn(code_changed)}; store HEAD changed: {yn(store_changed)}"
    )
    has_evidence = bool(code_changed) or bool(store_changed)
else:
    n = len(unmapped)
    shown = unmapped[:8]
    extra = n - len(shown)
    paths_line = ", ".join(shown) if shown else "(none)"
    if extra > 0:
        paths_line += f", +{extra}"
    fact_line = (
        f"Coverage signal — {n} edited file(s) with no decision topic: {paths_line}; "
        f"code HEAD changed: {yn(code_changed)}; store HEAD changed: {yn(store_changed)}"
    )
    has_evidence = (n > 0) or bool(code_changed) or bool(store_changed)

if not has_evidence:
    sys.exit(3)

store_root = os.environ.get("MEMCONTINUUM_ROOT") or "<store root not configured>"
question = (
    "Any ruling, incident, or rejected alternative from this session that "
    "the MemContinuum store should hold? Store: " + store_root + " — a ruling "
    "is a new link in topics/<area>/<topic>.md, an incident is a file in "
    "incidents/ (see docs/SCHEMA.md); NOT Claude Code auto-memory. If none, "
    "say so once."
)
ctx = fact_line + "\n\n" + question
print(json.dumps({
    "hookSpecificOutput": {
        "hookEventName": "SessionStart",
        "additionalContext": ctx,
    }
}))
' "$STATE_FILE" 2>>"$MC_LOG")"
        RC=$?

        # Look-back compact twin: only evaluated (and only reads pending,
        # never mutates it) when coverage found no evidence above. Coverage
        # always wins; this must run BEFORE pending is cleared below.
        LB_JSON=""
        LB_RC=3
        LB_SINCE=""
        LB_TURN=""
        if [ $RC -ne 0 ]; then
            LB_META_TMP="$(mktemp 2>/dev/null)"
            LB_JSON="$(MEMCONTINUUM_ROOT="${MEMCONTINUUM_ROOT:-}" env PYTHONPATH= "$MC_PY" -c '
import json, os, sys

try:
    with open(sys.argv[1]) as f:
        state = json.load(f)
    if not isinstance(state, dict):
        state = {}
except Exception:
    state = {}

pending = state.get("pending") or {}
if not pending:
    sys.exit(3)

turn = pending.get("user_turn_count", 0)
last_growth_turn = pending.get("last_growth_turn", 0)
since = turn - last_growth_turn
if since < 3:
    sys.exit(3)

Q = chr(39)
store_root = os.environ.get("MEMCONTINUUM_ROOT") or "<store root not configured>"
fact = (
    f"Look-back signal — {since} user turns with no edited-file evidence; "
    "context was just compacted."
)
question = (
    "Did the conversation since then establish any ruling, incident, "
    "rejected alternative, priority, wording choice, money decision, or "
    f"{Q}not now{Q} that the MemContinuum store should hold? Store: "
    + store_root + " — a ruling is a new link in topics/<area>/<topic>.md, "
    "an incident is a file in incidents/ (see docs/SCHEMA.md); NOT Claude "
    "Code auto-memory. If none, say so once."
)
ctx = fact + "\n\n" + question
print(json.dumps({
    "hookSpecificOutput": {
        "hookEventName": "SessionStart",
        "additionalContext": ctx,
    }
}))
print(f"{turn} {since}", file=sys.stderr)
' "$STATE_FILE" 2>"${LB_META_TMP:-/dev/null}")"
            LB_RC=$?
            if [ -n "${LB_META_TMP:-}" ] && [ -s "$LB_META_TMP" ]; then
                read -r LB_TURN LB_SINCE < "$LB_META_TMP" 2>/dev/null
            fi
            rm -f "${LB_META_TMP:-}" 2>/dev/null
        fi

        # Consume pending unconditionally (idempotent no-op if already empty)
        # so a compact source never re-injects the same evidence twice; when
        # the look-back twin fired, also stamp last_inject_turn/last_inject_ts
        # AND last_inject_time (dual-gate review finding 4 -- coverage's own
        # cooldown clock, so a per-turn coverage candidate right after this
        # compact doesn't read a stale/zero last_inject_time and fire
        # straight through the cooldown that is meant to follow any
        # injection) in this same locked update so the very next per-turn
        # hook call requires a full new thin stretch rather than immediately
        # re-firing.
        LB_COUNT=""
        if [ $LB_RC -eq 0 ] && [ -n "$LB_JSON" ]; then
            export MC_LB_TURN="$LB_TURN"
            mc_update_state_json "$STATE_FILE" '
import os, time

state["pending"] = {}
try:
    turn = int(os.environ.get("MC_LB_TURN") or 0)
except Exception:
    turn = 0
now = time.time()
state["last_inject_turn"] = turn
state["last_inject_time"] = now
state["last_inject_ts"] = now
state["lookback_count"] = state.get("lookback_count", 0) + 1

print(json.dumps(state))
' >>"$MC_LOG" 2>&1
            # dual-gate review finding 7: lookback_count is tracked but was
            # never logged -- read it back (cheap, one extra call; this
            # hook has no overall-budget constraint) so the outcome line
            # below can include it.
            LB_COUNT="$(env PYTHONPATH= "$MC_PY" -c '
import json, sys
with open(sys.argv[1]) as f:
    state = json.load(f)
print(state.get("lookback_count", 0))
' "$STATE_FILE" 2>>"$MC_LOG")"
        else
            mc_update_state_json "$STATE_FILE" '
state["pending"] = {}
print(json.dumps(state))
' >>"$MC_LOG" 2>&1
        fi

        if [ $RC -eq 0 ] && [ -n "$OUTPUT_JSON" ]; then
            printf '%s\n' "$OUTPUT_JSON"
            finish "compact-injected"
        elif [ $LB_RC -eq 0 ] && [ -n "$LB_JSON" ]; then
            printf '%s\n' "$LB_JSON"
            finish "compact-lookback" "turn=$LB_TURN since=$LB_SINCE count=${LB_COUNT:-0}"
        else
            finish "compact-no-evidence"
        fi
        ;;

    *)
        finish "source-not-handled"
        ;;
esac
