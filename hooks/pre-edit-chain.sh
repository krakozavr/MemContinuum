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
#   - runs under hooks/mc-watchdog.sh's own budget (see "Watchdog guard"
#     below): a bounded run finishes in well under a second on measurement;
#     a run past the inner budget still exits 0, still logs a named
#     outcome, and still emits a minimal additionalContext stating the
#     retrieval timed out rather than staying silent. Every run appends one
#     timing (or watchdog-kill) line to $MEMCONTINUUM_HOME/hook.log.
#   - hard-clears PYTHONPATH itself (the hook environment trap, DECISION SS8):
#     PreToolUse hooks spawn shells that re-source .bashrc, which re-exports a
#     Windows-site-packages PYTHONPATH that breaks the venv's own packages.
#
# Env:
#   MEMCONTINUUM_ROOT     store markdown root; used to derive a default
#                    project name ($(basename "$MEMCONTINUUM_ROOT")) when
#                    MEMCONTINUUM_PROJECT is unset. Also passed to `for-path`
#                    as `--root` (final-fix-wave item 2 -- `for-path` gained
#                    an optional --root so it can see the "stale" state
#                    instead of silently answering "current" off a store
#                    edited since the last reindex) whenever it's set; when
#                    unset, `for-path` is still called, just rootless
#                    (exactly its old behavior -- never "stale"). See
#                    "engine request" below for the still-open suffix-match
#                    ask, unrelated to this.
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

# Watchdog guard (F6, external-review fix round) -- same pattern every
# other guarded hook uses (hooks/ledger-post-edit.sh's own header explains
# what running it costs). Budget: measured against this hook's real wired
# command line on three live stores -- the engine's own, plus two other
# real, live projects, one of them hosted entirely on a slow drvfs
# (/mnt/c) mount, code root and store both -- BEFORE this value was
# chosen. Measured p95/p99 across 34 timed runs of the exact rendered
# command line: 0.198s / 0.206s overall, max 0.206s -- see this task's
# own report for the full per-store table.
# That leaves roughly 10x headroom under the unmodified default budget
# (MC_WATCHDOG_BUDGET unset here -- the five-write-side-hooks 2s default,
# not sessionend-stamp.sh's tighter 1.2s: two sequential for-path calls on
# a miss need the fuller budget), so 2s is confirmed by measurement, not
# assumed. Sourcing this also resolves MEMCONTINUUM_HOME and MC_GUARD_PY
# (env -> config.sh -> engine venv), so the duplicate resolution this file
# used to carry inline is gone -- PY below reads MC_GUARD_PY directly
# instead of re-deriving it.
#
# MC_WATCHDOG_TIMEOUT_FALLBACK is exported BEFORE the re-exec block below:
# on a watchdog timeout the child (this same script, re-exec'd under the
# launcher) is killed before it can write anything to stdout, so a plain
# "silent exit 0" would leave Claude Code reading an EMPTY additionalContext
# -- indistinguishable from "retrieval ran and found nothing". This value
# is what the launcher (hooks/mc-watchdog.sh's embedded python) writes to
# stdout instead on that path -- see this repo's docs/INTERNALS.md "The
# watchdog" section for the full contract.
export MC_WATCHDOG_TIMEOUT_FALLBACK='{"hookSpecificOutput":{"hookEventName":"PreToolUse","additionalContext":"Decision-chain retrieval timed out; absence of a matching decision was not established -- treat this edit as unverified against recorded decisions, not as confirmed clear."}}'
# shellcheck source=mc-watchdog.sh
source "${MC_WATCHDOG_LIB_PATH:-$SCRIPT_DIR/mc-watchdog.sh}" 2>/dev/null
if [ -z "${MC_UNDER_TIMEOUT:-}" ]; then
    export MC_UNDER_TIMEOUT=1
    if [ -x "${MC_GUARD_PY:-}" ] && [ -n "${MC_WATCHDOG_LAUNCHER_PY:-}" ]; then
        "$MC_GUARD_PY" -c "$MC_WATCHDOG_LAUNCHER_PY" "${BASH:-bash}" "${BASH_SOURCE[0]}" "$@"
        exit 0
    fi
fi

MEMCONTINUUM_HOME="${MEMCONTINUUM_HOME:-$HOME/.memcontinuum}"
# Coordinator review fix: MC_GUARD_PY is unset whenever mc-watchdog.sh
# fails to source (e.g. MC_WATCHDOG_LIB_PATH pointing nowhere) -- falling
# straight to the hardcoded engine-venv default in that case silently
# dropped an explicitly baked MEMCONTINUUM_PYTHON (verified: a fake
# python that only mc-watchdog.sh's own resolution step would ever
# invoke was skipped entirely). MEMCONTINUUM_PYTHON is now the second
# fallback, ahead of the hardcoded venv path -- same env->config.sh->venv
# precedence every other hook uses, restored for this one path.
PY="${MC_GUARD_PY:-${MEMCONTINUUM_PYTHON:-$SCRIPT_DIR/../.venv/bin/python}}"
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
#
# Final-fix-wave item 2: `--root "$MEMCONTINUUM_ROOT"` is now always
# passed (when set -- see FORPATH_ARGS below, built as a non-empty array
# from the start so `"${FORPATH_ARGS[@]}"` is always safe under `set -u`
# on bash 3.2) so a stale store no longer silently answers as current.
# for-path's own stderr (captured into $LOG by the `2>>"$LOG"` redirect
# below, same as every other call this script makes) already carries the
# stale warning line -- this hook only needs to notice the state to log
# its OWN distinct outcome name, `index-stale-served`, instead of
# `matched`; the injected additionalContext payload built below is
# unaffected either way (it comes from CHAIN_TEXT, a separate plain-text
# call, never from RESULT_JSON).
MATCHED_CANDIDATE=""
MATCHED_STATE="current"
RESULT_JSON=""
ANY_QUERY_SUCCEEDED=0
for candidate in "${CANDIDATES[@]}"; do
    FORPATH_ARGS=(for-path "$candidate" --project "$PROJECT" --db "$DB_PATH")
    [ -n "${MEMCONTINUUM_ROOT:-}" ] && FORPATH_ARGS+=(--root "$MEMCONTINUUM_ROOT")
    FORPATH_ARGS+=(--json)
    RESULT_JSON="$(PYTHONPATH= "$PY" "$MEMIDX" "${FORPATH_ARGS[@]}" 2>>"$LOG")"
    RC=$?
    # F1 (ruling 68): for-path's own exit codes -- 3 = missing/uninitialized
    # (the same outcome name the pre-loop [ ! -f "$DB_PATH" ] check above
    # already uses), 4 = index-error (a schema a migration guard should
    # already have fixed but didn't). Both get their own named outcome
    # instead of falling into the generic RC != 0 -> continue -> eventual
    # "query-failed"/"no-match" below, which would hide which candidate (if
    # any) actually had a usable index.
    if [ $RC -eq 3 ]; then
        finish "index-missing"
    fi
    if [ $RC -eq 4 ]; then
        finish "index-error"
    fi
    if [ $RC -ne 0 ]; then
        continue
    fi
    ANY_QUERY_SUCCEEDED=1
    # Final-fix-wave item 2: --json now wraps as {"state":...,"results":
    # [...]} whenever --root surfaced a non-current state -- pull the real
    # results array (falling back to the whole payload for the pre-
    # existing bare-list shape) and the state name (defaulting to
    # "current" for that same bare-list shape) out of whichever form this
    # call actually returned.
    RESULTS_ONLY="$(printf '%s' "$RESULT_JSON" | "$PY" -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    print("[]"); sys.exit(0)
print(json.dumps(d.get("results", d) if isinstance(d, dict) else d))
' 2>/dev/null)"
    CANDIDATE_STATE="$(printf '%s' "$RESULT_JSON" | "$PY" -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    print("current"); sys.exit(0)
print(d.get("state", "current") if isinstance(d, dict) else "current")
' 2>/dev/null)"
    [ -z "$CANDIDATE_STATE" ] && CANDIDATE_STATE="current"
    TRIMMED="$(printf '%s' "$RESULTS_ONLY" | tr -d '[:space:]')"
    if [ -n "$TRIMMED" ] && [ "$TRIMMED" != "[]" ]; then
        MATCHED_CANDIDATE="$candidate"
        MATCHED_STATE="$CANDIDATE_STATE"
        break
    fi
done

if [ -z "$MATCHED_CANDIDATE" ]; then
    if [ "$ANY_QUERY_SUCCEEDED" -eq 0 ]; then
        finish "query-failed"
    fi
    finish "no-match"
fi

TOPIC_COUNT="$(printf '%s' "$RESULTS_ONLY" | grep -c '"id":')"

# --- get the pretty chain-view text for the matched candidate --------------
# Fix round 4 (ruling 136 / CI evidence): no --root here. The FORPATH_ARGS
# loop above already resolved decision_index_state(root=...) for this exact
# store/project (walking it via _index_has_drift when --root is set) and
# already emitted any stale/quarantined warning off that same call's own
# stderr (captured into $LOG, same as this one). Passing --root again here
# would make THIS call re-run that same on-disk walk a second time for a
# candidate already known to match -- one hook run, one store walk. Losing
# --root here can only ever downgrade what THIS call itself might report as
# "stale" back to "quarantined" or "current" (never gain a state it
# shouldn't have) -- MATCHED_STATE/the outcome name logged by finish() still
# come from the first call, unaffected.
CHAIN_TEXT_ARGS=(for-path "$MATCHED_CANDIDATE" --project "$PROJECT" --db "$DB_PATH")
CHAIN_TEXT="$(PYTHONPATH= "$PY" "$MEMIDX" "${CHAIN_TEXT_ARGS[@]}" 2>>"$LOG")"

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
if [ "$MATCHED_STATE" = "stale" ]; then
    finish "index-stale-served"
fi
finish "matched"
