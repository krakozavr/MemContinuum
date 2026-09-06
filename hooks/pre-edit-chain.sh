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
# not sessionend-stamp.sh's tighter 1.2s: one for-path call per candidate,
# and a miss walks every candidate, so the fuller budget still covers
# that multi-candidate worst case), so 2s is confirmed by measurement,
# not assumed. Sourcing this also resolves MEMCONTINUUM_HOME and MC_GUARD_PY
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
# `matched`.
#
# Round 5 (ruling 137 / CI evidence: the macOS runner measured this hook
# at 1.003-1.022s against its own 1.0s bar, 27% runner-speed variance
# between runs -- the cost is process starts, not real work). `for-path`
# now takes `--with-chain-text` (memidx.py item 1): FORPATH_ARGS always
# carries it, so the SAME call that finds the matching candidate and its
# state also returns the plain-text chain rendering, in the same JSON
# envelope -- there is no longer a second, separate `for-path` call once a
# match is found (the old CHAIN_TEXT_ARGS call is gone). The three
# separate `python -c` parsers this loop used to run per candidate
# (results-only, candidate-state, topic-count via a `grep -c` besides)
# collapse into the one `python -c` call below, which prints all four
# values -- matched flag, state, topic count, chain text -- NUL-separated.
# Read via `read -d ''` off a process substitution (`< <(...)`), not a
# `|` pipe, so the values land in THIS shell rather than a subshell that
# would discard them on exit -- no mapfile, so this stays bash-3.2-safe.
MATCHED_CANDIDATE=""
MATCHED_STATE="current"
MATCHED_TOPIC_COUNT="0"
CHAIN_TEXT=""
ANY_QUERY_SUCCEEDED=0
for candidate in "${CANDIDATES[@]}"; do
    FORPATH_ARGS=(for-path "$candidate" --project "$PROJECT" --db "$DB_PATH")
    [ -n "${MEMCONTINUUM_ROOT:-}" ] && FORPATH_ARGS+=(--root "$MEMCONTINUUM_ROOT")
    FORPATH_ARGS+=(--json --with-chain-text)
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
    # --json --with-chain-text always wraps as an object -- {"results":
    # [...], "chain_text": "...", maybe "state": ...} -- but this parser
    # stays defensive about a malformed/bare-list payload (same fallbacks
    # the old two parsers each had) since it is fed straight from
    # $RESULT_JSON, not re-validated first.
    #
    # Round 6 fix: `topic_count` is now a REAL count of the DISTINCT
    # topics whose chains actually appear in chain_text -- every direct
    # topic match in `results`, plus, per matched concept ("kind":
    # "concept"), the topics in its own "governed_by" list (the exact set
    # for_path_chain_lines in memidx.py iterates for that concept; the
    # concept entry itself is never counted -- it isn't a topic). Ids are
    # collected into a SET, not just summed, so a topic that is both a
    # direct match and a governor of a matched concept (or governs two
    # matched concepts at once) is counted once in the header, matching
    # the header's own wording ("N topic(s) REFERENCE this file" -- a
    # count of distinct topics, not of chain renderings). chain_text
    # itself is unaffected by this and keeps rendering that topic's chain
    # once per role (for_path_chain_lines has no dedup of its own) -- the
    # header and the body are allowed to disagree in that one direction.
    # Round 5 had instead replicated the
    # OLD `grep -c '"id":'` behavior verbatim, quirk included: `grep -c`
    # counts matching LINES, not occurrences, and the old RESULTS_ONLY (a
    # plain `json.dumps`, no `indent=`) was always exactly one line -- so
    # the header always said "1 topic(s)" no matter how many topics or
    # concepts actually matched. That quirk is what this round fixes: a
    # two-topic match now reports "2", not "1" (tests/test_hooks.py's
    # oracle-parity class normalises this one field before its
    # byte-for-byte comparison against the frozen pre-round-5 script,
    # documenting why there).
    MATCHED_FLAG=""
    CANDIDATE_STATE=""
    CANDIDATE_TOPIC_COUNT=""
    CANDIDATE_CHAIN_TEXT=""
    {
        IFS= read -r -d '' MATCHED_FLAG
        IFS= read -r -d '' CANDIDATE_STATE
        IFS= read -r -d '' CANDIDATE_TOPIC_COUNT
        IFS= read -r -d '' CANDIDATE_CHAIN_TEXT
    } < <(printf '%s' "$RESULT_JSON" | PYTHONPATH= "$PY" -c '
import json, sys

try:
    d = json.load(sys.stdin)
except Exception:
    d = None

if isinstance(d, dict):
    results = d.get("results", [])
    state = d.get("state", "current") or "current"
    chain_text = d.get("chain_text", "") or ""
else:
    results = d if isinstance(d, list) else []
    state = "current"
    chain_text = ""

matched = "1" if results else "0"

topic_ids = set()
for entry in results:
    if not isinstance(entry, dict):
        continue
    if entry.get("kind") == "concept":
        governed = entry.get("governed_by")
        if isinstance(governed, list):
            for grow in governed:
                if isinstance(grow, dict) and "id" in grow:
                    topic_ids.add(grow["id"])
    elif "id" in entry:
        topic_ids.add(entry["id"])
topic_count = str(len(topic_ids))

# Round 7 fix (Codex MAJOR): this stream is field-delimited by chr(0) and
# read back with read -d "", which treats ANY NUL byte as the end of the
# CURRENT read -- not just the one this loop appends after each field. A
# record whose decoded text embeds a real NUL (e.g. YAML "before\0after"
# in a ruling/rationale/owner_boundary string -- json.dumps escapes it as
# six ASCII characters, backslash-u-0-0-0-0, in transit, and json.load
# decodes that back to an actual NUL byte here) used to truncate that
# read call current field AND silently discard every field still queued
# behind it in the SAME stream (here chain_text is last, so nothing
# downstream was lost, but the same one-shared-stream risk applies to any
# future field added after it) -- the topic_count computed above from the
# untruncated results still reported the full count, while
# additionalContext itself went missing everything past the embedded
# NUL. The pre-round-5 transport (three separate command substitutions,
# one value per call) never hit this: plain command substitution in bash
# silently DROPS embedded NUL bytes from captured output, it does not
# truncate the surrounding text. Matching that behavior -- not somehow
# delivering a real NUL through a NUL-delimited protocol -- is the fix:
# strip NULs from each field before it enters the shared stream, so a
# NUL can never be mistaken for the chr(0) delimiter, and no text past
# it is ever lost.
for field in (matched, state, topic_count, chain_text):
    sys.stdout.write(field.replace(chr(0), ""))
    sys.stdout.write(chr(0))
' 2>/dev/null)
    [ -z "$CANDIDATE_STATE" ] && CANDIDATE_STATE="current"
    if [ "$MATCHED_FLAG" = "1" ]; then
        MATCHED_CANDIDATE="$candidate"
        MATCHED_STATE="$CANDIDATE_STATE"
        MATCHED_TOPIC_COUNT="$CANDIDATE_TOPIC_COUNT"
        CHAIN_TEXT="$CANDIDATE_CHAIN_TEXT"
        break
    fi
done

if [ -z "$MATCHED_CANDIDATE" ]; then
    if [ "$ANY_QUERY_SUCCEEDED" -eq 0 ]; then
        finish "query-failed"
    fi
    finish "no-match"
fi

TOPIC_COUNT="$MATCHED_TOPIC_COUNT"

# CHAIN_TEXT came off the SAME matched candidate's for-path call above
# (--with-chain-text) -- no second `for-path` invocation fetches it.
if [ -z "$CHAIN_TEXT" ]; then
    finish "empty-chain-text"
fi

# Round 7 fix (Codex MINOR): the pre-round-5 transport captured this text
# via `$(...)` command substitution, which strips every trailing newline
# unconditionally. The round-5 transport threads CHAIN_TEXT through the
# NUL-delimited `read -d ''` parser instead, which preserves it exactly
# as memidx.py rendered it -- and for_path_chain_lines can end in a
# newline when its LAST line is a concept row whose owner_boundary came
# from a YAML `|` block scalar (block-scalar clipping keeps exactly one
# trailing newline). Left alone, that trailing newline plus the "\n\n"
# join separator below produces an extra blank line before
# CITATION_REMINDER in additionalContext, diverging from the frozen
# pre-round-5 oracle byte-for-byte. Strip every trailing newline here, at
# the hook boundary, to restore the old `$(...)` parity -- a `case`/`%`
# loop, not `${var: -1}` or any bash-4-only trick, so this stays bash
# 3.2-safe; never touches a newline embedded INSIDE the text.
while true; do
    case "$CHAIN_TEXT" in
        *$'\n') CHAIN_TEXT="${CHAIN_TEXT%$'\n'}" ;;
        *) break ;;
    esac
done

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
