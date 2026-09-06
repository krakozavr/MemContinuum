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
#   - for ledger entries under any configured code root: runs `memidx.py
#     unmapped` (self-healing: check -> reindex --no-embed on drift),
#     `--code-root` passed once per configured root, to classify them
#   - for ledger entries under the store root with no code entries present:
#     runs a plain `memidx.py reindex --no-embed --auto` directly, so
#     store-only edits still get reconciled into the index even with
#     nothing to check coverage for (ruling E: "store edits trigger
#     RECONCILIATION, not reminders") -- --auto (ruling 69) keeps
#     embedding_mode untouched, since this hook-triggered heal never
#     re-embeds
#   - compares every configured code root's current git HEAD against the
#     session's start_code_shas map (falling back to the single legacy
#     start_code_sha for a root the map has no entry for) and the store
#     root's HEAD against start_store_sha (all captured by
#     sessionstart-remind.sh)
#   - writes the result to state.pending, replacing whatever was there
#
# Timeout: this whole script runs under a 2s watchdog (see the guard just
# below) instead of per-call `timeout 2`; a slow/hung python still leaves
# this hook exiting 0 well under Claude Code's own PreCompact timeout.
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
    mc_log "precompact outcome=$1 session=${SESSION_ID:-}"
    exit 0
}

PAYLOAD="$(cat)"
[ -z "$PAYLOAD" ] && finish "empty-payload"

eval "$(mc_extract_fields "$PAYLOAD" session_id trigger)" 2>/dev/null
[ -z "${SESSION_ID:-}" ] && finish "no-session-id"

STATE_FILE="$(mc_state_file_for "$MC_PROJECT" "$SESSION_ID")"
[ -f "$STATE_FILE" ] || finish "no-state"

# --- pull what we need out of state (ledger) without mutating it -- start
# SHAs are read directly off STATE_FILE further down, not through here --
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

# Design R5 (audit MC-P1-05, TOP-0123 L5): every configured code root,
# captured ONCE (one python spawn via mc_code_roots, memlib.sh) and reused
# below by BOTH the `unmapped` call and the per-root HEAD comparison.
CODE_ROOTS_TEXT="$(mc_code_roots)"

# --- store-only reconciliation (no code paths to classify) ------------------
if [ "${#CODE_PATHS[@]}" -eq 0 ]; then
    if [ "$STORE_TOUCHED" = "true" ] && [ -n "${MEMCONTINUUM_ROOT:-}" ]; then
        env PYTHONPATH= "$MC_PY" "$MC_MEMIDX" reindex \
            --root "$MEMCONTINUUM_ROOT" --project "$MC_PROJECT" --db "$MC_DB_PATH" \
            --no-embed --auto >>"$MC_LOG" 2>&1
    fi
fi

# --- classify code-root edits via the self-healing `unmapped` engine call ---
UNMAPPED_JSON="{}"
if [ "${#CODE_PATHS[@]}" -gt 0 ] && [ -n "${MEMCONTINUUM_ROOT:-}" ]; then
    ARGS=(unmapped)
    ARGS+=("${CODE_PATHS[@]}")
    ARGS+=(--root "$MEMCONTINUUM_ROOT" --project "$MC_PROJECT" --db "$MC_DB_PATH" --json)
    while IFS= read -r CR; do
        [ -n "$CR" ] || continue
        ARGS+=(--code-root "$CR")
    done <<<"$CODE_ROOTS_TEXT"
    RAW="$(env PYTHONPATH= "$MC_PY" "$MC_MEMIDX" "${ARGS[@]}" 2>>"$MC_LOG")"
    RC=$?
    # F1 (ruling 68): see userprompt-remind.sh's identical block -- accept
    # RC 0 or 1, log a distinct outcome per coverage_status via mc_log
    # directly (never finish(), which would exit 0 and skip the rest of
    # this script's normal reconciliation work).
    if { [ $RC -eq 0 ] || [ $RC -eq 1 ]; } && [ -n "$RAW" ]; then
        UNMAPPED_JSON="$RAW"
        COVERAGE_STATUS="$(printf '%s' "$RAW" | env PYTHONPATH= "$MC_PY" -c '
import json, sys
try:
    print((json.load(sys.stdin) or {}).get("coverage_status", "unknown"))
except Exception:
    print("unknown")
' 2>/dev/null)"
        case "$COVERAGE_STATUS" in
            uninitialized)      mc_log "precompact outcome=index-uninitialized session=${SESSION_ID:-}" ;;
            upgrade-required)   mc_log "precompact outcome=index-upgrade-required session=${SESSION_ID:-}" ;;
            index-error)        mc_log "precompact outcome=index-error session=${SESSION_ID:-}" ;;
            quarantined)        mc_log "precompact outcome=index-quarantined session=${SESSION_ID:-}" ;;
        esac
        # Design R7 (audit MC-P2-03, TOP-0123 L7): see userprompt-remind.sh's
        # identical block -- a typed internal error stays coverage_status
        # "unknown" but carries its own `degraded` object; one more
        # hook.log token, same case-arm style, so `stats` can count it.
        DEGRADED_REASON="$(printf '%s' "$RAW" | env PYTHONPATH= "$MC_PY" -c '
import json, sys
try:
    d = (json.load(sys.stdin) or {}).get("degraded")
except Exception:
    d = None
print(d.get("reason_code", "") if isinstance(d, dict) else "")
' 2>/dev/null)"
        if [ -n "$DEGRADED_REASON" ]; then
            mc_log "precompact outcome=index-degraded reason=${DEGRADED_REASON} session=${SESSION_ID:-}"
        fi
    fi
fi

# --- git HEAD comparison -----------------------------------------------------
# Design R5 (audit MC-P1-05, TOP-0123 L5): per-root HEAD comparison --
# code_head_changed = ANY configured root moved since session start. One
# combined python call (reading STATE_FILE directly, not READ_TMP) covers
# BOTH code (per-root map, LOW-4-fixed: a root missing from the map falls
# back to the single legacy start_code_sha only when it IS the first
# configured root) and store (unchanged, single root) -- replacing the two
# READ_TMP-based start_code_sha/start_store_sha read_field spawns above
# with one call here, so this hook's total python-spawn count stays flat
# despite the new mc_code_roots call above. LOW-3/LOW-4 (task-7-review.md):
# both the per-root HEAD loop and this comparison now live once in
# memlib.sh (mc_code_heads_from / mc_head_changed).
CODE_HEADS="$(mc_code_heads_from "$CODE_ROOTS_TEXT")"
CUR_STORE_SHA="$(mc_git_head "${MEMCONTINUUM_ROOT:-}")"

# Safe defaults in case the python call below prints nothing (interpreter
# gone, unexpected crash) -- under `set -u` an unset CODE_CHANGED/
# STORE_CHANGED would otherwise abort the hook with no log line.
CODE_CHANGED="false"
STORE_CHANGED="false"
eval "$(mc_head_changed "$STATE_FILE" "$CODE_HEADS" "$CUR_STORE_SHA" "${MEMCONTINUUM_CODE_ROOT:-}")"

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
