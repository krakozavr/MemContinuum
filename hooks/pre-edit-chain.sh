#!/usr/bin/env bash
# PreToolUse hook: before an Edit/Write touches a file, look up whether any
# decision-chain topic references it (memidx.py for-path) and, if so, inject
# the compressed chain view(s) as additionalContext.
#
# Delivery point (measured, both Claude Code and Codex 0.154.0, TOP-0131
# L2): "before an Edit/Write touches a file" above is about when THIS SCRIPT
# runs, not when the model sees additionalContext. A PreToolUse hook matches
# on tool_input, which exists only once the model has already emitted the
# Edit/Write call with its final old_string/new_string -- so no PreToolUse
# hook on either runtime can shape the content of the edit that triggered
# it. What this script's output CAN still do: run before that edit reaches
# disk, and arrive with that same tool call's result in the same turn --
# governing the agent's next move, not the one that triggered the lookup.
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
# The worktree gap (docs/internal/SESSION-HANDOFF-releases-0.3-to-0.6.md
# SS"0.2.0 final" item 1): once every candidate below (raw path, cwd-
# relative, STRIP_PREFIX forms) has failed for FILE_PATH itself,
# mc_remap_worktree_path (hooks/mc-path-lib.sh) is tried: if FILE_PATH
# sits inside a `git worktree` whose MAIN checkout is one of the roots
# MEMCONTINUUM_STRIP_PREFIX names, it is remapped to its main-checkout-
# equivalent path and fed back through the SAME candidate machinery -- never
# onto an unwired repo's records, and never by indexing the worktree's own
# content. Two outcomes name the ways this can still fail visibly instead of
# collapsing into a plain `no-match`: `worktree-unwired` (confirmed to be a
# worktree, but its main repo isn't a configured root) and
# `worktree-unresolved` (git itself could not settle the question).
#
# This closes the gap in THIS SCRIPT's own logic; it cannot make the harness
# LAUNCH the script for a file outside every "if" glob repo-init.sh renders
# (templates/code-root-filter-pair.json.tmpl scopes each invocation's "if"
# to one literal RETRIEVAL-root path: the git toplevel of each --code-root,
# or the root itself outside git; code roots narrower than the checkout
# scope only the index) -- a worktree checked out as a SIBLING of
# the wired root never reaches this script at all under current wiring, so
# it gets NO retrieval here (ledger-post-edit.sh, which has no such filter,
# still ledgers it -- see that hook's own header). One checked out INSIDE
# the wired root (e.g. `<root>/.worktrees/x`) DOES reach this script, and
# gets full retrieval -- round 1 of this fix claimed this case was "closed
# end-to-end" while its own callers still silently skipped the remap for
# ANY FILE_PATH already physically under a configured root (Grok's round-2
# BLOCKER finding): the in-root worktree case is what that round-2 fix
# actually closes; round 1 only wired the machinery for it. A project that
# wants retrieval for its own worktrees should therefore keep them INSIDE
# the code root (e.g. `<root>/.worktrees/`, gitignored by that project) --
# a sibling worktree stays ledger-only until the settings-rendering
# follow-up (see this task's own report) widens the "if" glob itself.
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
# not assumed.
#
# Fix-round measurement update (search fallback, TOP-0133 L1): a genuine
# MISS now also runs `memidx.py search --hydrate` on the same budget --
# measured end to end (hook launch through the search subprocess's own
# rc) at 0.91-0.96s for hybrid mode (its RRF fusion pays for two ranking
# passes plus one embedding-model load/query per run; fts mode alone
# measured well under 0.1s in the same runs, vector mode close to
# hybrid's own cost). Still comfortably inside the 2s budget, but with
# far less headroom than the matched-branch measurement above -- a store
# on a slow drvfs mount (SQLite WAL locking is unreliable there; see this
# repo's own Windows/WSL environment notes) is the candidate case worth
# watching, and MEMCONTINUUM_FALLBACK_MODE=fts (docs/INTERNALS.md) is the
# per-installation escape hatch for it. The shipped default stays hybrid
# either way -- this is a per-installation override, not a default change;
# ship on the engine's own default, let real queries decide whether that
# should move. Sourcing this also resolves MEMCONTINUUM_HOME and MC_GUARD_PY
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
    # $1 = one-word outcome for the log line; $2 = optional extra
    # "key=value" text spliced in between elapsed= and project= (eval-
    # topic-logging: the only caller today is the matched/index-stale-
    # served path, passing "topics=<ids>" -- see MATCHED_TOPIC_IDS below).
    # project=/file= keep their existing trailing position and order no
    # matter what $2 is, so memidx.py's own parser (which locates the
    # LINE's project=/file= structurally, not by counting fields) never
    # sees a shape it doesn't already handle. Everything after stays 0.
    local outcome="$1"
    local extra="${2:-}"
    local now elapsed extra_part
    now=$(date +%s 2>/dev/null || echo "$START_TS")
    elapsed=$(( now - START_TS ))
    # The wall clock can step BACKWARDS mid-run (an NTP correction, a VM
    # resume, a manual set), which made this field negative and produced
    # `elapsed=-2s` in a real CI run. Fix-round correction (Codex 6, Grok
    # 6): the ORIGINAL comment here claimed memidx's stats parser takes
    # medians/p95 over this value -- verified false by grepping every
    # non-comment `elapsed` in memidx.py: `_hook_log_line_kind` only checks
    # for the SUBSTRING " elapsed=" to classify a hook.log line into the
    # "pre-edit" bucket, and never parses the number at all. The one real
    # consumer of the number's SHAPE is tests/test_hooks.py's own
    # `elapsed=\d+s` regex assertion, which a leading `-` cannot match --
    # that is the actual flake this fixes. Nothing downstream expects a
    # sign either way; a clock that went backwards means the true elapsed
    # is unknowable, and 0 is the honest floor.
    [ "$elapsed" -lt 0 ] && elapsed=0
    extra_part=""
    [ -n "$extra" ] && extra_part=" $extra"
    log "$(date -Iseconds 2>/dev/null || date) outcome=$outcome elapsed=${elapsed}s${extra_part} project=${PROJECT:-} file=${FILE_PATH:-}"
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
MATCHED_TOPIC_IDS=""
CHAIN_TEXT=""
ANY_QUERY_SUCCEEDED=0
# try_candidate CANDIDATE -- queries for-path for exactly one candidate and,
# on a match, sets the MATCHED_* globals above and returns 0 (a `break`-
# worthy match); returns 1 on no-match or a failed query (ANY_QUERY_SUCCEEDED
# still records whether the QUERY itself ran, same as the inline loop this
# was factored out of). Factored into a function (worktree gap fix,
# docs/internal/SESSION-HANDOFF-releases-0.3-to-0.6.md SS"0.2.0 final" item
# 1) so the SAME query logic can be reused for the worktree-remapped
# candidates below without a second copy of this body -- bash 3.2 has no
# namerefs, so this operates on the same globals the original inline loop
# always did, not a passed-by-reference array.
try_candidate() {
    local candidate="$1"
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
        return 1
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
    CANDIDATE_TOPIC_IDS=""
    CANDIDATE_CHAIN_TEXT=""
    {
        IFS= read -r -d '' MATCHED_FLAG
        IFS= read -r -d '' CANDIDATE_STATE
        IFS= read -r -d '' CANDIDATE_TOPIC_COUNT
        IFS= read -r -d '' CANDIDATE_TOPIC_IDS
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

# eval-topic-logging: WHICH topics matched, not just how many -- turns a
# matched edit into a gradeable sample (file, decisions shown, a later
# judgement of whether they were the right ones). Sorted so the log line is
# deterministic (byte-identical across runs of the same match) and capped at
# 10 ids (a trailing "+N" names how many more were left out) so a
# pathological file governed by dozens of topics can never blow up a single
# hook.log line. Isolated in its own try/except: this is new, cosmetic-only
# formatting layered on top of the topic_ids set the topic_count line above
# already computed and relies on -- a failure HERE must never turn a real
# match into a false "no-match" (fail-open discipline), it must just come
# back as an empty NUL field, same as an old build that never sent one at
# all.
try:
    _sorted_ids = sorted(str(i) for i in topic_ids)
    if len(_sorted_ids) > 10:
        topics_field = ",".join(_sorted_ids[:10]) + ",+" + str(len(_sorted_ids) - 10)
    else:
        topics_field = ",".join(_sorted_ids)
except Exception:
    topics_field = ""

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
#
# eval-topic-logging is exactly the "future field added after it" this
# comment warned about -- topics_field is inserted here, BEFORE chain_text,
# never after, so chain_text keeps the guarantee above (it is still the
# LAST field in the stream, so nothing can ever queue behind IT to be lost
# the way this comment describes). Same NUL-stripping treatment as every
# other field, for the same reason.
for field in (matched, state, topic_count, topics_field, chain_text):
    sys.stdout.write(field.replace(chr(0), ""))
    sys.stdout.write(chr(0))
' 2>/dev/null)
    [ -z "$CANDIDATE_STATE" ] && CANDIDATE_STATE="current"
    if [ "$MATCHED_FLAG" = "1" ]; then
        MATCHED_CANDIDATE="$candidate"
        MATCHED_STATE="$CANDIDATE_STATE"
        MATCHED_TOPIC_COUNT="$CANDIDATE_TOPIC_COUNT"
        MATCHED_TOPIC_IDS="$CANDIDATE_TOPIC_IDS"
        CHAIN_TEXT="$CANDIDATE_CHAIN_TEXT"
        return 0
    fi
    return 1
}

for candidate in "${CANDIDATES[@]}"; do
    try_candidate "$candidate" && break
done

# --- worktree gap: reached ONLY once every existing candidate has already
# failed for FILE_PATH itself (docs/internal/SESSION-HANDOFF-releases-0.3-
# to-0.6.md SS"0.2.0 final" item 1) ---------------------------------------
# THIS block never sources hooks/memlib.sh (the mkdir/config.sh/MC_PY cost
# that would add on every already-a-miss lookup is exactly what this
# hook's own header explains it exists to avoid) -- so the "configured
# code roots" it can check a remap against are read from
# MEMCONTINUUM_STRIP_PREFIX instead of mc_code_roots. MINOR fix-round
# correction: this used to say the SCRIPT never sources memlib.sh at all
# -- no longer true since the search fallback (TOP-0133 L1) added its own
# session-state write further down, which lazily sources memlib.sh ONLY
# on that already-rare branch (a genuine miss AND at least one search
# hit) -- see that branch's own comment. This worktree-gap block, reached
# on every miss regardless of what the fallback later does, still pays
# nothing extra. repo-init.sh
# renders exactly ONE STRIP_PREFIX entry per invocation (= the single code
# root that invocation's settings.json "if" filter is already scoped to,
# templates/code-root-filter-pair.json.tmpl), so this is a faithful reading
# of "the configured roots this invocation knows about", not a
# looser/lossier stand-in for mc_code_roots. A hand-wired STRIP_PREFIX that
# is not really a repo root can never produce a false remap either way --
# mc_remap_worktree_path's own match test is exact common-dir identity,
# never a prefix/substring test (see its header in hooks/mc-path-lib.sh)
# -- it would just make the remap silently not fire.
#
# Round 2 fix (Grok BLOCKER): a FILE_PATH already physically under one of
# these roots used to skip this whole block outright -- wrong for a
# worktree checked out INSIDE the root (e.g. `<root>/.worktrees/feat/`),
# the ONLY worktree location the settings-level "if" glob
# (templates/code-root-filter-pair.json.tmpl) ever lets THIS SCRIPT be
# invoked for at all (a sibling worktree's Edit never matches that glob,
# so it never reaches this script -- see this file's own header). Fixed:
# still zero extra `git` calls for the ordinary in-root case (no nested
# worktree at all -- by far the common shape of an in-root miss), via
# mc_nested_worktree_gitfile's bash-only ancestor walk; mc_remap_worktree_path's
# own calls -- one identity call for FILE_PATH itself, plus one more per
# configured root while it searches for a match -- are paid only when that
# walk actually finds a nested `.git` FILE between FILE_PATH and the
# matching root.
if [ -z "$MATCHED_CANDIDATE" ] && [ "$ANY_QUERY_SUCCEEDED" -eq 1 ] && [ -n "${MEMCONTINUUM_STRIP_PREFIX:-}" ]; then
    # shellcheck source=mc-path-lib.sh
    source "$SCRIPT_DIR/mc-path-lib.sh"
    WT_ROOTS="$(mc_strip_prefix_roots "$MEMCONTINUUM_STRIP_PREFIX")"
    declare -a WT_ROOT_ARR=()
    while IFS= read -r wt_root; do
        [ -n "$wt_root" ] && WT_ROOT_ARR+=("$wt_root")
    done <<EOF2
$WT_ROOTS
EOF2

    if [ -n "$WT_ROOTS" ]; then
        # An in-root path with no nested worktree, or whose nested gitfile
        # could not be resolved, is not a worktree-gap situation: the
        # helper reports it as rc 3, exactly the plain "in root, no
        # decision bound to it" miss.
        # The in-root gate and the remap itself live in mc_remap_worktree_gated
        # (hooks/mc-path-lib.sh), shared with hooks/newfile-nudge.sh.
        WT_REMAPPED="$(mc_remap_worktree_gated "$FILE_PATH" "$WT_ROOTS")"
        WT_REMAP_RC=$?
        case $WT_REMAP_RC in
            0)
                # Feed the remapped (main-checkout) path through the
                # EXISTING candidate machinery -- the bare relative
                # form (what code_refs are actually written in) plus
                # every STRIP_PREFIX form, same as a raw FILE_PATH
                # would get above -- and reuse try_candidate rather
                # than a second query implementation.
                if ! try_candidate "$WT_REMAPPED"; then
                    for wt_root in "${WT_ROOT_ARR[@]}"; do
                        wt_rel="${WT_REMAPPED#"$wt_root"/}"
                        if [ "$wt_rel" != "$WT_REMAPPED" ]; then
                            try_candidate "$wt_rel" && break
                        fi
                    done
                fi
                ;;
            1) finish "worktree-unwired" ;;
            2) finish "worktree-unresolved" ;;
            *) ;; # 3: not applicable (not a worktree, in-root with nothing to remap, or no .git at all)
        esac
    fi
fi

if [ -z "$MATCHED_CANDIDATE" ]; then
    if [ "$ANY_QUERY_SUCCEEDED" -eq 0 ]; then
        finish "query-failed"
    fi

    # --- search fallback (TOP-0133 L1) --------------------------------
    # `for-path` found NOTHING bound to this file -- the ONLY branch this
    # runs on (never a match, never worktree-unwired/-unresolved, both of
    # which already `finish`ed above before this point is ever reached).
    # `memidx.py search` -- the one channel that can deliver a decision
    # NOT bound to the file being edited -- is queried on the path's OWN
    # words instead, and the nearest decisions (if any) are handed to the
    # agent labelled as a guess, never as a match. QUERY_SRC_PATH is
    # WT_REMAPPED (the worktree block's own main-checkout-equivalent
    # path) whenever that block actually produced one -- never a raw
    # `.worktrees/x/...` path's words, which name the worktree, not the
    # file. Never runs when QUERY_SRC_PATH sits under $MEMCONTINUUM_ROOT
    # (the store itself -- that is duplicate-detection's job, out of
    # scope here).
    QUERY_SRC_PATH="${WT_REMAPPED:-$FILE_PATH}"

    if [ -n "${MEMCONTINUUM_ROOT:-}" ]; then
        # shellcheck source=mc-path-lib.sh
        source "$SCRIPT_DIR/mc-path-lib.sh"
        mc_path_under_root "$QUERY_SRC_PATH" "$MEMCONTINUUM_ROOT"
        if [ $? -eq 0 ]; then
            finish "search-fallback-empty" "reason=store-root"
        fi
    fi

    # shellcheck source=mc-query-lib.sh
    source "$SCRIPT_DIR/mc-query-lib.sh"
    QUERY_REL_PATH="$(mc_query_source_path "$QUERY_SRC_PATH" "$CWD" "${MEMCONTINUUM_STRIP_PREFIX:-}")"
    FALLBACK_QUERY="$(mc_query_tokens "$QUERY_REL_PATH")"
    if [ -z "$FALLBACK_QUERY" ]; then
        finish "search-fallback-empty" "reason=no-query"
    fi

    # MEMCONTINUUM_FALLBACK_MODE (documented in docs/INTERNALS.md): hybrid|
    # vector|fts, else the engine's own default (hybrid) -- deliberately
    # the SAME default `memidx.py search` itself already uses when --mode
    # is omitted, so an unset env var changes nothing about which mode
    # runs. An unrecognized value falls back to that same default rather
    # than failing the whole fallback over a typo'd env var.
    FALLBACK_MODE="${MEMCONTINUUM_FALLBACK_MODE:-hybrid}"
    case "$FALLBACK_MODE" in
        hybrid | vector | fts) ;;
        *) FALLBACK_MODE="hybrid" ;;
    esac

    # ONE process (round 5 precedent: `--with-chain-text` folded a second
    # `for-path` call away the same way) -- `--hydrate` carries each hit's
    # own chain_text in the same JSON envelope, so no separate `chain`
    # call per hit. `--limit 2`, no `--status`/`--authority` filter (the
    # engine's own defaults apply). `--read-only` (MAJOR fix-round item a):
    # this channel reads the index to answer a guess, never writes to it --
    # a legacy/unmigrated schema is refused by name (state=upgrade-required,
    # reason=index-needs-migration) instead of being silently migrated on
    # open, which the pre-fix-round `open_db_noncreating` path did. `--root`
    # is now passed WHEN KNOWN (MAJOR item b: staleness must be visible,
    # not swallowed) -- the old "never --root, it'd be noise" comment
    # covered a real concern (index-stale-served already reports staleness
    # on the MATCH path above) but left the FALLBACK path unable to tell a
    # genuinely current miss from a stale one; `_decision_warn`'s own
    # stderr line for it is discarded below same as every other subprocess
    # stderr (MINOR item: routed to /dev/null, never hook.log).
    FB_ARGS=(search "$FALLBACK_QUERY" --mode "$FALLBACK_MODE" --project "$PROJECT" --db "$DB_PATH" --limit 2 --json --hydrate --read-only --decisions-only)
    if [ -n "${MEMCONTINUUM_ROOT:-}" ]; then
        FB_ARGS+=(--root "$MEMCONTINUUM_ROOT")
    fi
    # fb_ms (MAJOR fix-round item d): millisecond-resolution timing of the
    # search subprocess itself -- `elapsed=`'s 1-second `date +%s` floor
    # made p50/p95 meaningless (a bounded-under-a-second call rounds to
    # "0s" or "1s" depending only on which side of a tick boundary it
    # started). mc_now_ms is bash-3.2-safe (see hooks/mc-query-lib.sh).
    # Measured only around THIS call, on purpose: it is the one variable-
    # cost step in this branch (hybrid mode's model load/embed), and
    # timing it around the query-build/state-write steps too would just
    # add noise from unrelated I/O. Emitted only past this point, so a
    # `no-query`/`store-root` early-exit above (finish already called)
    # never contributes an fb_ms sample -- stats' own p50/p95 pool is
    # this-subprocess-ran-only by construction, not by a later filter.
    # MINOR fix-round item: a marker naming THIS PROCESS's own pid (the
    # watchdog launcher's `proc.pid` sees the identical value -- see
    # hooks/mc-watchdog.sh's own comment) exists ONLY for the duration of
    # the search subprocess call below. A watchdog kill mid-call finds it
    # still there and logs `fb_started=1` on its own `watchdog-killed`
    # line, closing the stats blind spot where a kill on this branch left
    # no trace of the search having even started.
    FB_STARTED_MARKER="$MEMCONTINUUM_HOME/.fb-started.$$"
    # Re-gate round 4 NIT: a trap, not only the explicit `rm -f` right
    # after the search call below -- that explicit remove covers the
    # normal (search returned, hit or miss) path, but an ABNORMAL exit
    # between the marker's creation and that point (a crash this script
    # itself raises, not just a watchdog SIGKILL, which no trap here can
    # ever catch -- see hooks/mc-watchdog.sh's own marker-check instead)
    # would otherwise leave the marker file behind. `exit 0` (finish())
    # runs registered EXIT traps like any other exit.
    trap '[ -n "${FB_STARTED_MARKER:-}" ] && rm -f "$FB_STARTED_MARKER" 2>/dev/null' EXIT
    : >"$FB_STARTED_MARKER" 2>/dev/null || true
    FB_MS_T0="$(mc_now_ms "$PY")"
    FALLBACK_JSON="$(PYTHONPATH= "$PY" "$MEMIDX" "${FB_ARGS[@]}" 2>/dev/null)"
    FALLBACK_RC=$?
    rm -f "$FB_STARTED_MARKER" 2>/dev/null || true
    FB_MS_T1="$(mc_now_ms "$PY")"
    FB_MS=""
    case "$FB_MS_T0$FB_MS_T1" in
        *[!0-9]*|"") ;;
        *) FB_MS=$((FB_MS_T1 - FB_MS_T0)); [ "$FB_MS" -lt 0 ] && FB_MS=0 ;;
    esac

    # Query encoding for the log line (item 5): `_hook_log_fields`
    # (memidx.py stats) splits on whitespace with no quote-awareness --
    # `q="core scan unbound"` would truncate at the first space and spray
    # the rest as bogus bare tokens. `+`-joined survives untouched.
    FALLBACK_QUERY_LOGGED="${FALLBACK_QUERY// /+}"

    # MAJOR fix-round item b (re-gate round 3, MINOR duplication: shared
    # with hooks/newfile-nudge.sh via mc_fallback_parse, hooks/mc-fallback-
    # lib.sh -- see that file's own header for the full reason-vocabulary
    # rationale). Runs whenever EITHER something reached stdout OR the
    # subprocess exited non-zero -- a genuine crash (rc != 0, nothing on
    # stdout at all) still needs `reason=search-failed rc=N` named, not
    # silently defaulting to "no-hits" the way gating on stdout alone would.
    FB_HITS=""
    FB_IDS=""
    FB_TEXT=""
    FB_HITS_FOR_STATE=""
    FB_REASON=""
    FB_FLOOR_DROPPED=""
    if [ -n "$FALLBACK_JSON" ] || [ "$FALLBACK_RC" -ne 0 ]; then
        # Re-gate round 4 NIT: guarded (2>/dev/null, checked) -- an
        # unguarded `source` of a MISSING/unreadable file would otherwise
        # print its own error to stderr and, if this ran with `set -e`
        # somewhere up the sourcing chain, abort the run before FB_REASON
        # is ever set below. A missing lib degrades to `reason=lib-
        # missing` (FB_HITS stays empty, so the existing no-hits finish
        # a few lines down fires with this reason instead of "no-hits").
        # shellcheck source=mc-fallback-lib.sh
        if source "$SCRIPT_DIR/mc-fallback-lib.sh" 2>/dev/null; then
            # TOP-0133 L5: the second hit must clear the RRF score floor
            # (mc_fallback_second_floor: default + MEMCONTINUUM_FALLBACK_
            # SECOND_FLOOR env, one definition, hooks/mc-fallback-lib.sh).
            mc_fallback_parse "$PY" "$FALLBACK_RC" \
                "$MC_FB_LABEL_FILE" \
                1 "$FALLBACK_JSON" "" "" "$(mc_fallback_second_floor)"
        else
            FB_REASON="lib-missing"
        fi
    fi

    FB_MS_PART=""
    [ -n "$FB_MS" ] && FB_MS_PART=" fb_ms=$FB_MS"

    if [ -z "$FB_HITS" ] || [ "$FB_HITS" = "0" ]; then
        [ -z "$FB_REASON" ] && FB_REASON="no-hits"
        finish "search-fallback-empty" "reason=$FB_REASON mode=$FALLBACK_MODE q=$FALLBACK_QUERY_LOGGED$FB_MS_PART"
    fi

    # Re-gate round 3, MAJOR 1: stdout is ALWAYS emitted -- the round-2
    # "print last, after the state write" reorder traded the two-document
    # bug for a WORSE one: under full lock contention, mc_update_state_
    # json's own 2.0s deadline (hooks/memlib.sh) outlives the SAME 2s
    # watchdog budget, so the watchdog kills the whole process group
    # before the (already-fully-computed, already-sitting-in-$FB_TEXT)
    # guess is ever printed -- the model then reads the WATCHDOG's own
    # generic timeout envelope ("absence of a matching decision was not
    # established"), which is actively FALSE (a decision WAS found; it
    # was simply never shown). The guess is the feature; the state write
    # is garnish. Fix: the state write below now runs with a SHORT
    # deadline (0.25s, mc_fallback_write_state's own DEADLINE_SECONDS --
    # hooks/mc-fallback-lib.sh) instead of the default 2.0s, and $FB_TEXT
    # is printed UNCONDITIONALLY right after, regardless of whether that
    # write succeeded, timed out, or never ran at all (no session_id, no
    # memlib.sh). A lock timeout here degrades to `fb_state=skipped-lock`
    # on the outcome line -- the look-back mention of THIS hit is lost,
    # never the hit itself. See the flock-holding fixture test in tests/
    # test_hooks.py pinning this exact property.
    #
    # Session-state title storage (docs/INTERNALS.md "search fallback"):
    # hook.log's own `ids=` field carries no title (item 5's field list is
    # fixed) -- the look-back block (hooks/userprompt-remind.sh) needs a
    # human-readable name per hit to render "search surfaced <title>
    # (<id>) for <file>", so each hit is ALSO appended to this session's
    # own state file (mc_state_file_for, the SAME per-session JSON state
    # every write-side hook already shares -- WRITE-LOCK ruling E,
    # hooks/memlib.sh) under `search_fallbacks`, capped at the last 20.
    # This is the ONE state write this hook ever makes, sourced and paid
    # for ONLY on this already-rare (a genuine miss AND at least one
    # search hit) branch -- the store and the index stay read-only either
    # way (see docs/INTERNALS.md's own no-write-path paragraph for the
    # exact scope of that claim). A session_id this hook cannot resolve
    # (missing from the payload, or memlib.sh unreachable) just skips the
    # state write -- $FB_TEXT is printed either way.
    SESSION_ID=""
    if [ -n "$PAYLOAD" ]; then
        if command -v jq >/dev/null 2>&1; then
            SESSION_ID="$(printf '%s' "$PAYLOAD" | jq -r '.session_id // empty' 2>/dev/null)"
        else
            SESSION_ID="$(printf '%s' "$PAYLOAD" | "$PY" -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(1)
print(d.get("session_id", "") or "")
' 2>/dev/null)"
        fi
    fi
    FB_STATE_PART=""
    if [ -n "$SESSION_ID" ] && [ -n "${FB_HITS_FOR_STATE:-}" ]; then
        # shellcheck source=memlib.sh
        source "$SCRIPT_DIR/memlib.sh"
        # Re-gate round 4 NIT: same guard as the parse call site above --
        # a missing/unreadable lib here degrades to `fb_state=lib-
        # missing` instead of an unguarded `source` error (or worse,
        # aborting the run before $FB_TEXT is ever printed).
        # shellcheck source=mc-fallback-lib.sh
        if source "$SCRIPT_DIR/mc-fallback-lib.sh" 2>/dev/null; then
            STATE_FILE="$(mc_state_file_for "$PROJECT" "$SESSION_ID")"
            if ! mc_fallback_write_state "$STATE_FILE" "$FILE_PATH" "$FB_HITS_FOR_STATE" "pre-edit-chain" 0.25; then
                FB_STATE_PART=" fb_state=skipped-lock"
            fi
        else
            FB_STATE_PART=" fb_state=lib-missing"
        fi
    fi

    printf '%s\n' "$FB_TEXT"

    # TOP-0133 L5: `floor_dropped=<id>` names the second hit the RRF score
    # floor withheld (absent when nothing was dropped) -- the re-check data.
    FB_FLOOR_PART=""
    [ -n "$FB_FLOOR_DROPPED" ] && FB_FLOOR_PART=" floor_dropped=$FB_FLOOR_DROPPED"
    finish "search-fallback" "hits=$FB_HITS ids=$FB_IDS mode=$FALLBACK_MODE q=$FALLBACK_QUERY_LOGGED$FB_MS_PART$FB_STATE_PART$FB_FLOOR_PART"
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
# eval-topic-logging: name which topics were actually injected, on the log
# line only -- never in additionalContext (the model-facing payload above is
# already final by this point). Omitted entirely (never an empty `topics=`
# token) when MATCHED_TOPIC_IDS is empty -- e.g. a matched CONCEPT with no
# governing topics -- so a reader can always tell "named" from "nothing to
# name" apart from a truncated/older line.
TOPICS_EXTRA=""
[ -n "$MATCHED_TOPIC_IDS" ] && TOPICS_EXTRA="topics=$MATCHED_TOPIC_IDS"
if [ "$MATCHED_STATE" = "stale" ]; then
    finish "index-stale-served" "$TOPICS_EXTRA"
fi
finish "matched" "$TOPICS_EXTRA"
