#!/usr/bin/env bash
# Stop + SessionStart hook: keeps the code index fresh (TOP-0137 L2 step 4).
#
# Freshness is detected by the EXISTING preflight, not by anything in this
# file: memidx.py code-refresh runs code_index_report (five stat signals, sha
# on drift, the git HEAD trigger -- it sees an edit by any tool or shell and
# every commit) and, on Stop only, heal_code_index (the same in-process
# incremental call code-search makes, text pass, never embeds). SessionStart
# preflights only: that event blocks the session, so a stale report there
# writes the <project>.code-stale marker and the first Stop refreshes. The
# marker, the flock that excludes a concurrent refresh or code-search heal,
# and the one hook.log fragment are all produced by memidx.py code-refresh;
# this script is the guard (watchdog, name validation) and the logger.
#
# No git post-commit hook is installed in code repos (decision, TOP-0137 L2
# step 4): a commit made inside a session is caught by the next Stop, one made
# outside any session by the next SessionStart.
#
# SILENT on every path: a Stop hook that prints is fed back to the model, so
# the worker's stdout is captured here and only ever reaches hook.log. Always
# exits 0.
#
# Env: MEMCONTINUUM_HOME, MEMCONTINUUM_PROJECT, MEMCONTINUUM_PYTHON,
# MEMCONTINUUM_CODE_ROOTS / MEMCONTINUUM_CODE_ROOT (read by memidx.py, same
# rendered block the ledger hook gets), plus:
#   MEMCONTINUUM_CODE_REFRESH_BUDGET  wall-clock seconds for a Stop run
#                     (default 8); a SessionStart run is capped at 2. Passed
#                     to the watchdog launcher as MC_WATCHDOG_BUDGET.
#   MEMCONTINUUM_CODE_REFRESH_LIMIT   --heal-limit for the Stop heal
#                     (default 500).
#
# hook.log: <ts> code-refresh outcome=<refreshed|skip|failed> reason=...
#   project=P roots=N unrecorded=N changed=N removed=N healed=N dropped=N elapsed=Xs
#   event=E session=S cwd=<percent-encoded session working directory|-> (shell-composed skip/failed lines: always -) embed=<spawned|clean|off|failed|unknown>
#   [marker=write-failed] [detail=...]
# embed= is the background embedding worker's state (TOP-0137 L2 step 5):
# on BOTH events, whenever the index has chunks awaiting a vector,
# memidx.py raises <project>.code-embed-pending and starts a detached
# `code-embed-worker` (python subprocess.Popen in a new session -- never
# bash & or setsid; milliseconds, independent of the drift check):
# spawned = marker raised and a worker started (a worker that finds the
# lock held exits 0 itself, so there is no separate "pending" state),
# clean = nothing awaits, off = MEMCONTINUUM_EMBED_WORKER=0 (marker still
# raised; `off reason=no-wired-roots` = the hook names no code roots, so no
# worker is started), failed = marker, log or spawn refused, unknown = the run
# ended before it was evaluated. The worker is handed the wired roots
# (`--root`) and never processes a recorded root they do not name.
#
# dropped=N (INC-0129): recorded roots the wiring no longer names, removed on
# Stop (the code-reindex --drop-root deletion) with a
# `detail=dropped-roots:<path>;<path>` tail; SessionStart never drops, it
# writes the marker `stale-root=<n>` and the first Stop clears it. Two guards
# disable the drop for that run: every wired root must be absolute and strictly
# resolve to a directory, and at least one must be a recorded root:
# `detail=stale-check-skipped:relative-wired-root|unresolvable-wired-root|wired-root-not-a-directory|no-wired-root-recorded`.
#
# bash 3.2 discipline: no arrays, no [[ ]], no process substitution, no
# mapfile, no namerefs; no $(( )) on any env value (the budget is checked with
# case patterns; the limit is normalised once, in memidx.py).

set -u
export PYTHONPATH=

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
MEMIDX="$SCRIPT_DIR/../memidx.py"

# The event decides the watchdog budget (Stop: the refresh budget; anything
# else: the 2 s SessionStart class), so it is peeked from a bounded prefix of
# the payload BEFORE the guard, the same way ledger-post-edit.sh peeks
# tool_name; the guarded child receives that prefix again from the pipe below
# (memidx.py code-refresh reads the two fields it needs from a prefix).
# The read is bounded in BYTES (4096) and in TIME (about 2 s), builtins only:
# a writer that keeps stdin open must not hang the hook before the watchdog
# exists. It reads one character at a time because bash 3.2 DISCARDS what a
# timed-out `read -n N -t T` had already collected; here whatever arrived is
# kept. EOF, a timeout or a NUL ends the loop.
PAYLOAD=""
MC_RD_N=0
MC_RD_T0=$SECONDS
while [ "$MC_RD_N" -lt 4096 ] && [ $((SECONDS - MC_RD_T0)) -lt 2 ]; do
    MC_RD_CH=""
    IFS= read -r -d '' -n 1 -t 1 MC_RD_CH || break
    [ -n "$MC_RD_CH" ] || break
    PAYLOAD="$PAYLOAD$MC_RD_CH"
    MC_RD_N=$((MC_RD_N + 1))
done
MC_PREFIX="${PAYLOAD:0:4096}"
MC_EVENT=""
case "$MC_PREFIX" in
    *'"hook_event_name"'*)
        MC_EVENT="${MC_PREFIX#*\"hook_event_name\"}"
        MC_EVENT="${MC_EVENT#*:}"
        case "$MC_EVENT" in
            " "*) MC_EVENT="${MC_EVENT# }" ;;
        esac
        case "$MC_EVENT" in
            \"*) MC_EVENT="${MC_EVENT#\"}"; MC_EVENT="${MC_EVENT%%\"*}" ;;
            *) MC_EVENT="" ;;
        esac
        ;;
esac

# The session's working directory (`cwd=`): only memidx.py decodes the payload
# as JSON, so only the lines IT composes carry the percent-encoded path
# (_log_path_token). The shell never parses JSON strings: the skip/failed lines
# composed below print `cwd=-` (INC-0130).
MC_CWD="-"

# shellcheck source=mc-watchdog.sh
source "${MC_WATCHDOG_LIB_PATH:-$SCRIPT_DIR/mc-watchdog.sh}" 2>/dev/null
MC_NO_WATCHDOG=0
if [ -z "${MC_UNDER_TIMEOUT:-}" ]; then
    export MC_UNDER_TIMEOUT=1
    MC_BUDGET=2
    if [ "$MC_EVENT" = "Stop" ]; then
        MC_BUDGET="${MEMCONTINUUM_CODE_REFRESH_BUDGET:-8}"
        # A positive decimal only (checked by pattern, never by arithmetic);
        # anything else is the 8 s default.
        case "$MC_BUDGET" in
            ""|*[!0-9.]*|.*|*.|*.*.*) MC_BUDGET=8 ;;
        esac
        case "${MC_BUDGET//[0.]/}" in
            "") MC_BUDGET=8 ;;
        esac
    fi
    export MC_WATCHDOG_BUDGET="$MC_BUDGET"
    if [ -x "${MC_GUARD_PY:-}" ] && [ -n "${MC_WATCHDOG_LAUNCHER_PY:-}" ]; then
        printf '%s' "$PAYLOAD" | "$MC_GUARD_PY" -c "$MC_WATCHDOG_LAUNCHER_PY" "${BASH:-bash}" "${BASH_SOURCE[0]}" "$@"
        exit 0
    fi
    # No guard python: this run is NOT under a watchdog, and a refresh must
    # never run unwatched -- it logs a skip and does nothing.
    MC_NO_WATCHDOG=1
fi

# From here on: the guarded child (its stdin is the payload the parent piped
# back in, already read into PAYLOAD above). MEMCONTINUUM_HOME is resolved and
# exported by mc-watchdog.sh above.
MEMCONTINUUM_HOME="${MEMCONTINUUM_HOME:-$HOME/.memcontinuum}"
PY="${MEMCONTINUUM_PYTHON:-$SCRIPT_DIR/../.venv/bin/python}"
LOG="$MEMCONTINUUM_HOME/hook.log"

# This hook never creates the home directory: nowhere to log, nothing to do.
[ -d "$MEMCONTINUUM_HOME" ] || exit 0

PROJECT="${MEMCONTINUUM_PROJECT:-default}"
PATH_UNSAFE=0
case "$PROJECT" in
    ""|.|..|*/*|*[[:cntrl:]]*) PATH_UNSAFE=1 ;;
esac

mc_cr_log() {
    printf '%s code-refresh %s\n' "$(date -Iseconds 2>/dev/null || date)" "$1" >>"$LOG" 2>/dev/null || true
}

if [ "$PATH_UNSAFE" -eq 1 ]; then
    mc_cr_log "outcome=skip reason=bad-project project=$(printf '%s' "$PROJECT" | tr -d '[:cntrl:]') event=${MC_EVENT:-unknown} cwd=$MC_CWD"
    exit 0
fi
if [ "$MC_NO_WATCHDOG" -eq 1 ]; then
    mc_cr_log "outcome=skip reason=no-watchdog project=$PROJECT event=${MC_EVENT:-unknown} cwd=$MC_CWD"
    exit 0
fi
if [ ! -x "$PY" ]; then
    mc_cr_log "outcome=skip reason=no-python project=$PROJECT event=${MC_EVENT:-unknown} cwd=$MC_CWD"
    exit 0
fi

OUT="$(printf '%s' "$PAYLOAD" | "$PY" "$MEMIDX" code-refresh --project "$PROJECT" 2>/dev/null)"
RC=$?
FIRST="${OUT%%
*}"
case "$FIRST" in
    outcome=*) mc_cr_log "$FIRST" ;;
    *) mc_cr_log "outcome=failed reason=no-result rc=$RC project=$PROJECT event=${MC_EVENT:-unknown} cwd=$MC_CWD" ;;
esac
exit 0
