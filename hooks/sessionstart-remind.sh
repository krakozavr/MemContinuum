#!/usr/bin/env bash
# SessionStart hook. Behavior branches on the payload's `source` field
# in-script (SessionStart supports a matcher on `source` at the settings
# level too, but docs/DESIGN.md requires in-script gating as the
# defense of record):
#
#   startup | resume | clear
#                     -- initialize this session's state (start_code_sha /
#                        start_store_sha captured from the code/store roots'
#                        current git HEAD, via setdefault so a *resume* never
#                        resets a startup's original values), then prune
#                        state files older than 24h across this project.
#                        Silent UNLESS the standing-decisions digest (next
#                        paragraph) has something new to inject.
#
#   TOP-0132 L1/L2 (every source): after the per-source work above, always
#                        attempts the standing-decisions digest --
#                        `memidx.py standing --json`'s citation-only
#                        projection of every topic's own `standing:`
#                        pointer list, computed AFTER every state mutation
#                        the branch already makes (init/prune/rotate on
#                        startup; pending consumption on compact) so a
#                        watchdog kill during the digest's own subprocess
#                        calls loses only the digest, never that
#                        bookkeeping. A non-current index (missing,
#                        uninitialized, upgrade-required, stale, OR
#                        quarantined -- `standing` refuses on every one of
#                        these, unlike every other reader here) or an empty
#                        standing set injects nothing, logged
#                        `standing=skipped-<state>` / `standing=empty`.
#                        Otherwise: `startup`/`resume` dedupe by comparing
#                        the digest's hash against `state["standing_hash"]`
#                        inside the SAME locked state-update transform that
#                        sets it (never a separate read-then-write -- see
#                        mc_compute_standing's callers), logging
#                        `standing=dedup` on a match; `clear` and `compact`
#                        ALWAYS inject regardless of any stored hash (the
#                        context is gone either way), logging
#                        `standing=injected links=N bytes=B hash=H`. On
#                        `compact`, the digest is appended AFTER coverage's
#                        (or, failing that, look-back's) own text in the
#                        same additionalContext -- coverage stays first
#                        when it has evidence. Loaded is not applied -- this
#                        raises the odds a standing ruling is honored, it
#                        is not a guarantee.
#
#                        `clear` (INC-0108) is a fresh session, not a
#                        continuation: /clear commonly fires on the SAME
#                        session_id an earlier startup already created state
#                        for (a /clear mid-process, same CLI run), so unlike
#                        resume it must DISCARD most of whatever state is
#                        already on disk for this session_id before the
#                        setdefault init below runs -- a leftover turn-count/
#                        pending/look-back baseline from before the clear
#                        would misfire the coverage/look-back nudges against
#                        turns the cleared context no longer has, and a stale
#                        SessionEnd `ended_at` stamp has no business
#                        surviving into a session that is still running. ONE
#                        field survives the discard: `ledger` (the edited-
#                        but-not-yet-mapped-to-a-decision file list) is
#                        carried over as-is -- userprompt-remind.sh's
#                        coverage check (`memidx.py unmapped`) classifies
#                        candidates *only* from `state["ledger"]`, never from
#                        a tree walk, so wiping it would make any file edited
#                        before the clear that is still genuinely unmapped
#                        invisible to the coverage nudge for the rest of the
#                        session, unless it happens to be touched again post-
#                        clear -- silent evidence loss of exactly the kind
#                        this project treats as zero-tolerance. The
#                        discard-plus-carry-ledger happens inside the same
#                        locked mc_update_state_json transform (state
#                        rebuilt to just {"ledger": ...} at the top, only for
#                        clear) so there is no separate unlocked delete step
#                        and no window where a concurrent read sees a half-
#                        reset file.
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

# hook.log grammar: sessionstart outcome=<o> [extra...] session=S source=SRC
# cwd=<session working directory|-> project=P  (cwd: the payload's `cwd`,
# control characters stripped by mc_cwd_token; `-` when absent -- INC-0130).
finish() {
    local extra="${2:-}" cwd_tok
    cwd_tok="$(mc_cwd_token "${CWD:-}")"
    if [ -n "$extra" ]; then
        mc_log "sessionstart outcome=$1 $extra session=${SESSION_ID:-} source=${SOURCE:-} $cwd_tok"
    else
        mc_log "sessionstart outcome=$1 session=${SESSION_ID:-} source=${SOURCE:-} $cwd_tok"
    fi
    exit 0
}

PAYLOAD="$(cat)"
[ -z "$PAYLOAD" ] && finish "empty-payload"

eval "$(mc_extract_fields "$PAYLOAD" session_id source cwd)" 2>/dev/null
[ -z "${SESSION_ID:-}" ] && finish "no-session-id"

STATE_FILE="$(mc_state_file_for "$MC_PROJECT" "$SESSION_ID")"

# mc_compute_standing -- TOP-0132 L1/L2. Runs `memidx.py standing --json`
# EXACTLY ONCE (never touches session state itself; callers below decide
# dedupe/persistence) and sets these globals:
#   STANDING_STATUS  "ready" | "empty" | "skipped-<state>"
#   STANDING_TEXT    the exact plain-text digest to inject (only set when
#                     STANDING_STATUS=ready), reconstructed from --json's
#                     own "lines" as HEADER + "\n" + "\n".join(lines)
#   STANDING_HASH / STANDING_LINKS / STANDING_BYTES  from --json, verbatim
#                     -- never recomputed here, so this hook can never
#                     disagree with `memidx.py standing --json` about what
#                     the digest's own hash or size is.
# Fix-round MAJOR (both reviewers, Opus measured 0.95-1.18s on drvfs with
# two links against the 2s watchdog): a prior version called `memidx.py
# standing` TWICE -- once --json for the structured fields, once plain for
# the exact text -- each a full module import (yaml, chunkers). Two calls
# cost roughly double, AND a store commit landing between them could make
# the logged hash disagree with the injected text (the very thing the
# dedupe/log line exists to describe accurately). ONE call now: the same
# python step that already parses the JSON for kind/links/hash/bytes also
# reconstructs the plain text from "lines" and writes it to a second
# tmpfile -- HEADER below is a literal copy of memidx.py's own
# STANDING_HEADER constant, not re-derived (there is no cheap way to ask a
# python module for one constant without importing the whole module,
# which is the exact cost this fix removes); the two ship in the same
# commit/repo, so they can never drift version-to-version by accident --
# grep STANDING_HEADER in memidx.py before ever changing either.
# Bash 3.2: no associative arrays, no `local -n`; globals by convention
# (this file is a leaf script, never sourced elsewhere).
# STANDING_IDS_CSV (TOP-0133 L2, Addendum B): comma-joined, deduped TOPIC
# ids (never link ids) behind every pointer this call's own digest would
# deliver -- so the prompt-query channel can treat "already surfaced by
# the standing digest" the same as "already surfaced by a search
# fallback" and never re-guess a topic the model already has in context.
# Derived from `standing --json`'s own "lines" (never a second query):
# each line is `standing_line`'s own rendering, `f"{topic_id} {link_id}
# (...): ..."`, so the FIRST whitespace-delimited token of a line is
# always that pointer's topic id -- see memidx.py's standing_line before
# ever changing either.
STANDING_STATUS=""
STANDING_TEXT=""
STANDING_HASH=""
STANDING_LINKS=0
STANDING_BYTES=0
STANDING_IDS_CSV=""

mc_compute_standing() {
    STANDING_STATUS="skipped-unavailable"
    STANDING_TEXT=""
    STANDING_HASH=""
    STANDING_LINKS=0
    STANDING_BYTES=0
    STANDING_IDS_CSV=""

    local args
    args=(standing --project "$MC_PROJECT" --db "$MC_DB_PATH")
    [ -n "${MEMCONTINUUM_ROOT:-}" ] && args+=(--root "$MEMCONTINUUM_ROOT")

    local json_out
    json_out="$(env PYTHONPATH= "$MC_PY" "$MC_MEMIDX" "${args[@]}" --json 2>>"$MC_LOG")"

    local meta_tmp text_tmp
    meta_tmp="$(mktemp 2>/dev/null)" || return
    text_tmp="$(mktemp 2>/dev/null)" || { rm -f "$meta_tmp"; return; }
    env PYTHONPATH= "$MC_PY" -c '
import json, sys

# Literal copy of memidx.STANDING_HEADER -- see the comment above
# mc_compute_standing in hooks/sessionstart-remind.sh before changing
# either one; they must always match.
HEADER = "Standing decisions -- citations only; the store is the source of truth."

try:
    obj = json.loads(sys.argv[1] or "{}")
except Exception:
    obj = {}

if "hash" in obj:
    lines = obj.get("lines") or []
    text = HEADER + ("\n" + "\n".join(lines) if lines else "")
    try:
        with open(sys.argv[2], "w") as f:
            f.write(text)
    except OSError:
        pass
    # TOP-0133 L2, Addendum B: the delivered TOPIC ids, deduped
    # (first-occurrence order), comma-joined -- each line own first
    # whitespace-delimited token is always its topic id (standing_line
    # own rendering in memidx.py, "{topic_id} {link_id} (...): ..."; a
    # topic id itself never contains a space or a comma). "-" when there
    # is nothing to join (mirrors this same line own "-" placeholder for
    # a missing hash), so the field is never empty/ambiguous for the
    # fixed-field `read` below.
    _seen_ids = []
    for _l in lines:
        _tid = _l.split(" ", 1)[0] if _l else ""
        if _tid and _tid not in _seen_ids:
            _seen_ids.append(_tid)
    ids_csv = ",".join(_seen_ids) if _seen_ids else "-"
    print("ok - %d %s %d %s" % (obj.get("links", 0), obj.get("hash", "-"), obj.get("bytes", 0), ids_csv))
elif "reason" in obj:
    # Fix-round MINOR (Grok): the over-cap envelope carries "reason":
    # "over-cap" and no "state" at all -- falling into the generic
    # obj.get("state", "unknown") branch below used to report this as
    # `standing=skipped-unknown`, losing the one piece of information
    # (the store is over its own cap) an operator most needs to see.
    print("refused %s 0 - 0 -" % obj.get("reason", "unknown"))
else:
    print("refused %s 0 - 0 -" % obj.get("state", "unknown"))
' "$json_out" "$text_tmp" >"$meta_tmp" 2>>"$MC_LOG"

    local kind rstate links hash nbytes ids
    read -r kind rstate links hash nbytes ids <"$meta_tmp" 2>/dev/null
    rm -f "$meta_tmp" 2>/dev/null

    if [ "${kind:-}" != "ok" ]; then
        STANDING_STATUS="skipped-${rstate:-unknown}"
        rm -f "$text_tmp" 2>/dev/null
        return
    fi
    if [ "${links:-0}" = "0" ]; then
        STANDING_STATUS="empty"
        rm -f "$text_tmp" 2>/dev/null
        return
    fi

    STANDING_TEXT="$(cat "$text_tmp" 2>/dev/null)"
    rm -f "$text_tmp" 2>/dev/null
    STANDING_HASH="$hash"
    STANDING_LINKS="$links"
    STANDING_BYTES="$nbytes"
    [ -n "${ids:-}" ] && [ "$ids" != "-" ] && STANDING_IDS_CSV="$ids"
    STANDING_STATUS="ready"
}

# mc_wrap_context TEXT -- prints `{"hookSpecificOutput": {"hookEventName":
# "SessionStart", "additionalContext": TEXT}}` on stdout, or nothing if the
# python call itself fails (fail-open, same as every other JSON build in
# this file). Shared by every branch below that injects, so the envelope
# shape is written exactly once.
mc_wrap_context() {
    env PYTHONPATH= "$MC_PY" -c '
import json, sys
print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": sys.argv[1]}}))
' "$1" 2>>"$MC_LOG"
}

# Fix-round MAJOR (both reviewers): a generation bump is not self-healing
# (see docs/INTERNALS.md's "Upgrading" -- neither unmapped's self-heal,
# memcontinuum-update.sh check mode, nor a template change reindexes on
# its own; only a store commit or a manual `reindex` does). A project
# with no store commit after the bump would sit at
# `standing=skipped-upgrade-required` forever with nothing visible to a
# human. mc_maybe_upgrade_hint sets STANDING_HINT_CTX to this one-line
# nudge exactly ONCE per session (a state flag, compare-and-set inside
# ONE locked transform, same race-safety shape as the hash write) --
# never a repeat nag on every subsequent SessionStart in the same run.
STANDING_HINT_CTX=""
STANDING_UPGRADE_FACT_FALLBACK="Standing decisions unavailable this session: the decision index generation does not match this engine's."

# mc_standing_upgrade_fact -- prints ONE line, a FACT stated to the human,
# never a verb addressed to the model. Fix-round MINOR (both reviewers):
# the previous wording ("run memcontinuum-update.sh --apply") was an
# IMPERATIVE sitting inside additionalContext, against this hook's own
# "never an imperative" rule (see the header comment above) -- an agent
# with shell access could simply run it, and --apply walks and re-renders
# every wired project, not just this one. Reads the real generation
# numbers itself (mode=ro, no write -- STANDING_STATUS alone only carries
# the state name, never the numbers) so the fact names them, and says
# which direction the mismatch runs: a BEHIND index has a real repair
# (`memcontinuum-update.sh --apply` reindexes it, stated as a fact about
# what that command does, not a command aimed at the reader); an AHEAD
# index has no repair this hook can name -- reindexing it here would only
# be refused by the write-side generation guard, so the fact says what is
# true (this checkout is older) and stops there.
mc_standing_upgrade_fact() {
    local gen_tmp
    gen_tmp="$(mktemp 2>/dev/null)" || { printf '%s' "$STANDING_UPGRADE_FACT_FALLBACK"; return; }
    env PYTHONPATH= "$MC_PY" -c '
import sqlite3, sys

sys.path.insert(0, sys.argv[2])
import memidx

db = sys.argv[1]
g = None
try:
    conn = sqlite3.connect("file:" + db + "?mode=ro", uri=True)
    row = conn.execute("SELECT value FROM db_meta WHERE key=" + chr(39) + "index_generation" + chr(39)).fetchone()
    conn.close()
    if row is not None:
        g = int(row[0])
except Exception:
    g = None
c = memidx.CURRENT_INDEX_GENERATION
print((str(g) if g is not None else "?") + " " + str(c))
' "$MC_DB_PATH" "$(dirname "$MC_MEMIDX")" >"$gen_tmp" 2>>"$MC_LOG"
    local gen cur
    read -r gen cur <"$gen_tmp" 2>/dev/null
    rm -f "$gen_tmp" 2>/dev/null

    if [ -z "${gen:-}" ] || [ "$gen" = "?" ]; then
        printf '%s' "$STANDING_UPGRADE_FACT_FALLBACK"
        return
    fi
    if [ "$gen" -lt "$cur" ] 2>/dev/null; then
        printf 'Standing decisions unavailable this session: the decision index predates this engine (generation %s < %s); memcontinuum-update.sh --apply repairs it.' "$gen" "$cur"
    elif [ "$gen" -gt "$cur" ] 2>/dev/null; then
        printf 'Standing decisions unavailable this session: the decision index is newer than this engine (generation %s > %s); this checkout is older than whatever last reindexed it.' "$gen" "$cur"
    else
        printf '%s' "$STANDING_UPGRADE_FACT_FALLBACK"
    fi
}

# mc_relint_note -- prints ONE fact line when the store's post-commit hook
# left a sticky `<project>.relint-failed` marker (a commit that skipped the
# commit checks, e.g. `git commit --no-verify`, and would have been refused;
# hooks/post-commit-reindex.sh writes it, the next clean store commit removes
# it). A fact, never an imperative. Nothing printed when there is no marker.
mc_relint_note() {
    local f="$MEMCONTINUUM_HOME/$MC_PROJECT.relint-failed" sha="" summary="" line root=""
    [ -f "$f" ] || return 0
    while IFS= read -r line || [ -n "$line" ]; do
        case "$line" in
            root=*) root="${line#root=}" ;;
            sha=*) sha="${line#sha=}" ;;
            summary=*) summary="${line#summary=}" ;;
        esac
    done <"$f"
    [ -n "$sha" ] || return 0
    printf 'MemContinuum: store %s%s commit %s skipped the commit checks and would have been refused (%s) -- fix with a new commit; see %s' \
        "$MC_PROJECT" "${root:+ ($root)}" "${sha:0:8}" "$summary" "$f"
}

# mc_code_stale_note -- prints ONE fact line when hooks/code-refresh.sh left
# the sticky `<project>.code-stale` marker (`reason=<r> since=<ts> roots=<n>`:
# a refresh that was killed mid-run, failed, was refused by --heal-limit, or a
# SessionStart preflight that found the code index behind). The marker is
# removed by the next refresh that finds nothing left to repair. Pure bash,
# no python. Nothing printed when there is no marker or it is unparseable.
# Entries of one hook event run in parallel, so a marker written by THIS
# session's own code-refresh.sh is shown by the next session start (or never,
# when the first Stop repairs it first) -- no ordering is relied on.
mc_code_stale_note() {
    local f="$MEMCONTINUUM_HOME/$MC_PROJECT.code-stale" line="" reason="" since=""
    [ -f "$f" ] || return 0
    IFS= read -r line <"$f" 2>/dev/null || [ -n "$line" ] || return 0
    case "$line" in
        reason=*" since="*) ;;
        *) return 0 ;;
    esac
    reason="${line#reason=}"
    reason="${reason%% since=*}"
    since="${line#*" since="}"
    since="${since%% roots=*}"
    printf 'MemContinuum: code index for %s may be stale: %s since %s -- memidx.py code-reindex refreshes it' \
        "$MC_PROJECT" "$reason" "$since"
}

mc_maybe_upgrade_hint() {
    STANDING_HINT_CTX=""
    [ "$STANDING_STATUS" = "skipped-upgrade-required" ] || return
    local hint_tmp
    hint_tmp="$(mktemp 2>/dev/null)" || return
    export MC_HINT_VERDICT_OUT="$hint_tmp"
    mc_update_state_json "$STATE_FILE" '
import os

_verdict = "shown" if state.get("standing_upgrade_hint_shown") else "new"
state["standing_upgrade_hint_shown"] = True
_out = os.environ.get("MC_HINT_VERDICT_OUT")
if _out:
    try:
        with open(_out, "w") as _f:
            _f.write(_verdict)
    except OSError:
        pass

print(json.dumps(state))
' >>"$MC_LOG" 2>&1
    local verdict="shown"
    if [ -s "$hint_tmp" ]; then
        verdict="$(cat "$hint_tmp")"
    fi
    rm -f "$hint_tmp" 2>/dev/null
    if [ "$verdict" = "new" ]; then
        STANDING_HINT_CTX="$(mc_standing_upgrade_fact)"
    fi
}

case "${SOURCE:-}" in
    startup|resume|clear)
        CODE_SHA="$(mc_git_head "${MEMCONTINUUM_CODE_ROOT:-}")"
        STORE_SHA="$(mc_git_head "${MEMCONTINUUM_ROOT:-}")"
        # Design R5 (audit MC-P1-05, TOP-0123 L5): start_code_sha stays
        # (first root, kept for older readers) AND start_code_shas
        # ({root: sha}) is added for every configured root -- one extra
        # python spawn (mc_code_roots, memlib.sh), the per-root git HEADs
        # (mc_git_head, no python) folded into the SAME state-update
        # transform below, no additional spawn for the map itself. LOW-3
        # (task-7-review.md): the per-root loop itself now lives once in
        # memlib.sh's mc_code_heads_from.
        CODE_HEADS="$(mc_code_heads_from "$(mc_code_roots)")"
        export MC_CODE_SHA="$CODE_SHA"
        export MC_STORE_SHA="$STORE_SHA"
        export MC_CODE_HEADS="$CODE_HEADS"
        export MC_SESSION_ID="$SESSION_ID"
        export MC_PROJECT_ENV="$MC_PROJECT"
        export MC_SOURCE="${SOURCE:-}"

        mc_update_state_json "$STATE_FILE" '
import os, time

# INC-0108: clear discards whatever this session_id had on disk before the
# setdefault init below runs, EXCEPT ledger -- see the header comment
# above for why the ledger alone survives. Design R6 (audit MC-P1-04,
# TOP-0123 L6): shell_baseline (the shell-diff branch own per-root
# baseline map) is kept alongside it for the same reason -- wiping it
# would silently re-baseline every root on the next Bash call, losing the
# distinction between pre- and post-clear shell dirt for the rest of the
# session.
if os.environ.get("MC_SOURCE") == "clear":
    _prior_ledger = state.get("ledger") or []
    _prior_shell_baseline = state.get("shell_baseline") or {}
    state = {}
    state["ledger"] = _prior_ledger
    state["shell_baseline"] = _prior_shell_baseline

_code_heads = {}
for _line in (os.environ.get("MC_CODE_HEADS") or "").splitlines():
    if _line and "\t" in _line:
        _root, _sha = _line.split("\t", 1)
        _code_heads[_root] = _sha

state.setdefault("session_id", os.environ.get("MC_SESSION_ID", ""))
state.setdefault("project", os.environ.get("MC_PROJECT_ENV", ""))
state.setdefault("start_code_sha", os.environ.get("MC_CODE_SHA", ""))
state.setdefault("start_code_shas", _code_heads)
# TOP-0122 L1 rule 2a (the commit nudge): last_seen_heads is a SEPARATE,
# per-PROMPT baseline (userprompt-remind.sh advances it turn by turn),
# distinct from the per-SESSION start_code_shas above -- seeded from the
# same current-HEAD map. clear (INC-0108, see the header comment) rebuilds
# state to just {ledger, shell_baseline} before this setdefault block
# runs, so a clear re-seeds last_seen_heads (and nudged_commits) from the
# CURRENT heads exactly like start_code_shas, never carrying either across
# a clear -- a commit already nudged before the clear is simply eligible
# again if that root HEAD ever revisits that sha, which is moot once
# last_seen_heads itself has just been reset to the current HEAD.
state.setdefault("last_seen_heads", dict(_code_heads))
state.setdefault("nudged_commits", [])
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

        # eval-topic-logging section 5 (owner-approved add-on): rotation
        # lives here, once per session at the session-INIT boundary
        # (startup/resume/clear) only -- never on mc_log's own append
        # path, and never re-checked on a mid-session `compact` (see
        # hooks/memlib.sh's mc_rotate_hook_log for the mechanics and the
        # fail-open/race-safety discussion).
        mc_rotate_hook_log

        # TOP-0132 L2: computed AFTER every state mutation above (init,
        # prune, rotate) -- a watchdog kill during mc_compute_standing's
        # own subprocess calls then loses only the digest, never the
        # session-init bookkeeping those already-committed writes hold.
        mc_compute_standing
        STANDING_CTX=""
        STANDING_LOG_TOKEN="$STANDING_STATUS"
        if [ "$STANDING_STATUS" = "ready" ]; then
            # Dedupe by hash, compare-and-set inside ONE locked transform
            # (never a plain read here followed by a separate write below
            # -- two concurrent SessionStart fires for this session could
            # otherwise both read the old hash and both decide "inject").
            # `clear` already rebuilt `state` to just {ledger,
            # shell_baseline} earlier in THIS SAME case arm, so it never
            # carries a stale standing_hash into this comparison --
            # `state.get("standing_hash")` is unset post-clear and never
            # equals a real hash, so clear always injects (when there is
            # something to inject) without needing its own branch here.
            export MC_STANDING_HASH="$STANDING_HASH"
            export MC_STANDING_IDS="$STANDING_IDS_CSV"
            STANDING_VERDICT_TMP="$(mktemp 2>/dev/null)"
            export MC_STANDING_VERDICT_OUT="${STANDING_VERDICT_TMP:-}"
            mc_update_state_json "$STATE_FILE" '
import os

_new_hash = os.environ.get("MC_STANDING_HASH", "")
_verdict = "dedup"
if state.get("standing_hash") != _new_hash:
    state["standing_hash"] = _new_hash
    # TOP-0133 L2, Addendum B: the delivered topic ids ride the SAME
    # locked write as standing_hash, and only when this comparison
    # actually decides to inject -- a "dedup" turn (the digest was
    # already shown, unchanged) writes neither, since nothing new was
    # delivered for the prompt-query channel to treat as surfaced.
    state["standing_ids"] = [
        _i for _i in (os.environ.get("MC_STANDING_IDS", "") or "").split(",") if _i
    ]
    _verdict = "inject"
elif "standing_ids" not in state:
    # Fix round 1 (MAJOR, Grok): a session whose state predates this key
    # (has standing_hash already stamped by an older engine build, but no
    # standing_ids) would otherwise dedupe here forever, leaving the key
    # absent -- the prompt-query exclusion set would then re-guess topics
    # the standing digest already delivered, until the next clear/compact
    # for this session. The hash comparison already decided nothing changed,
    # so the digest is not re-injected (verdict stays "dedup"), but the
    # ids it would have carried are backfilled from the SAME digest just
    # computed (STANDING_IDS_CSV, no extra work) -- a resumed session is
    # exactly the one a measurement channel needs to get right.
    state["standing_ids"] = [
        _i for _i in (os.environ.get("MC_STANDING_IDS", "") or "").split(",") if _i
    ]
_out = os.environ.get("MC_STANDING_VERDICT_OUT")
if _out:
    try:
        with open(_out, "w") as _f:
            _f.write(_verdict)
    except OSError:
        pass

print(json.dumps(state))
' >>"$MC_LOG" 2>&1
            STANDING_VERDICT="inject"
            if [ -n "${STANDING_VERDICT_TMP:-}" ] && [ -s "$STANDING_VERDICT_TMP" ]; then
                STANDING_VERDICT="$(cat "$STANDING_VERDICT_TMP")"
            fi
            rm -f "${STANDING_VERDICT_TMP:-}" 2>/dev/null
            if [ "$STANDING_VERDICT" = "inject" ]; then
                STANDING_CTX="$STANDING_TEXT"
                STANDING_LOG_TOKEN="injected links=$STANDING_LINKS bytes=$STANDING_BYTES hash=$STANDING_HASH"
            else
                STANDING_LOG_TOKEN="dedup"
            fi
        elif [ "$STANDING_STATUS" = "skipped-upgrade-required" ]; then
            mc_maybe_upgrade_hint
            if [ -n "$STANDING_HINT_CTX" ]; then
                STANDING_CTX="$STANDING_HINT_CTX"
                STANDING_LOG_TOKEN="skipped-upgrade-required hint=shown"
            fi
        fi

        RELINT_NOTE="$(mc_relint_note)"
        RELINT_TOKEN=""
        if [ -n "$RELINT_NOTE" ]; then
            if [ -n "$STANDING_CTX" ]; then
                STANDING_CTX="$STANDING_CTX"$'\n\n'"$RELINT_NOTE"
            else
                STANDING_CTX="$RELINT_NOTE"
            fi
            RELINT_TOKEN=" relint=shown"
        fi
        CODE_STALE_NOTE="$(mc_code_stale_note)"
        if [ -n "$CODE_STALE_NOTE" ]; then
            if [ -n "$STANDING_CTX" ]; then
                STANDING_CTX="$STANDING_CTX"$'\n\n'"$CODE_STALE_NOTE"
            else
                STANDING_CTX="$CODE_STALE_NOTE"
            fi
            RELINT_TOKEN="$RELINT_TOKEN code-stale=shown"
        fi

        if [ -n "$STANDING_CTX" ]; then
            PY_OUT="$(mc_wrap_context "$STANDING_CTX")"
            [ -n "$PY_OUT" ] && printf '%s\n' "$PY_OUT"
        fi

        finish "init" "standing=$STANDING_LOG_TOKEN$RELINT_TOKEN"
        ;;

    compact)
        # TOP-0132 L2: attempted on EVERY compact -- unlike startup/resume
        # it never dedup-skips (the context is gone after compaction), and
        # unlike coverage/look-back below it needs no pre-existing state
        # file (only their own `pending` read does). Computed before the
        # "no state" early-exit so that exit can still carry the digest.
        mc_compute_standing
        STANDING_CTX=""
        STANDING_LOG_TOKEN="$STANDING_STATUS"
        # Captured BEFORE the standing hash write below, which -- fix-
        # round MINOR (Codex) -- now runs even with no prior state file
        # (mc_update_state_json tolerates a missing file, treating it as
        # {} and creating it): a fresh `-f` test after that write would
        # always see the file it just created and never take the
        # "compact-no-state" branch below again, even on a genuinely
        # bare first compact.
        HAD_STATE_FILE=0
        [ -f "$STATE_FILE" ] && HAD_STATE_FILE=1
        if [ "$STANDING_STATUS" = "ready" ]; then
            STANDING_CTX="$STANDING_TEXT"
            STANDING_LOG_TOKEN="injected links=$STANDING_LINKS bytes=$STANDING_BYTES hash=$STANDING_HASH"
            # Persist unconditionally, even when no state file exists yet
            # -- the OLD `[ -f "$STATE_FILE" ]` guard here meant a compact
            # with no prior state injected but could never STORE the
            # hash, so the very next resume in that same session had
            # nothing to dedupe against and re-injected the identical
            # digest a second time.
            export MC_STANDING_HASH="$STANDING_HASH"
            export MC_STANDING_IDS="$STANDING_IDS_CSV"
            mc_update_state_json "$STATE_FILE" '
import os
state["standing_hash"] = os.environ.get("MC_STANDING_HASH", "")
# TOP-0133 L2, Addendum B: same locked write as standing_hash -- a
# compact always injects when ready (no dedup branch here, see the
# header comment above), so the ids ride unconditionally too.
state["standing_ids"] = [
    _i for _i in (os.environ.get("MC_STANDING_IDS", "") or "").split(",") if _i
]
print(json.dumps(state))
' >>"$MC_LOG" 2>&1
        elif [ "$STANDING_STATUS" = "skipped-upgrade-required" ]; then
            mc_maybe_upgrade_hint
            if [ -n "$STANDING_HINT_CTX" ]; then
                STANDING_CTX="$STANDING_HINT_CTX"
                STANDING_LOG_TOKEN="skipped-upgrade-required hint=shown"
            fi
        fi

        if [ "$HAD_STATE_FILE" -eq 0 ]; then
            if [ -n "$STANDING_CTX" ]; then
                PY_OUT="$(mc_wrap_context "$STANDING_CTX")"
                [ -n "$PY_OUT" ] && printf '%s\n' "$PY_OUT"
            fi
            finish "compact-no-state" "standing=$STANDING_LOG_TOKEN"
        fi

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
        "Coverage signal — decision-topic coverage unknown "
        f"(store index {coverage_status}); code HEAD changed: {yn(code_changed)}; "
        f"store HEAD changed: {yn(store_changed)}"
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

        # Merge: coverage's own ctx (if RC==0) else look-back's (if
        # LB_RC==0) else nothing, THEN the standing digest appended after
        # it -- coverage (and, failing that, look-back) stays first when
        # it has evidence; standing is delivered every compact regardless
        # (computed above, unconditionally). One python call does the
        # whole merge -- it never re-derives coverage/look-back's own
        # ctx text, only lifts it back out of the JSON those two branches
        # above already built, so this can never disagree with what they
        # actually decided.
        MERGE_META_TMP="$(mktemp 2>/dev/null)"
        FINAL_JSON="$(env PYTHONPATH= "$MC_PY" -c '
import json, sys


def ctx_of(raw):
    if not raw:
        return None
    try:
        obj = json.loads(raw)
    except Exception:
        return None
    return obj.get("hookSpecificOutput", {}).get("additionalContext")


output_json, lb_json, standing_ctx = sys.argv[1], sys.argv[2], sys.argv[3]

base = ctx_of(output_json)
kind = "coverage" if base is not None else None
if base is None:
    base = ctx_of(lb_json)
    kind = "lookback" if base is not None else None

parts = [p for p in (base, standing_ctx or None) if p]
final = "\n\n".join(parts)
if final:
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": final}}))
print(kind or "none", file=sys.stderr)
' "$OUTPUT_JSON" "$LB_JSON" "$STANDING_CTX" 2>"${MERGE_META_TMP:-/dev/null}")"
        MERGE_KIND="none"
        if [ -n "${MERGE_META_TMP:-}" ] && [ -s "$MERGE_META_TMP" ]; then
            read -r MERGE_KIND <"$MERGE_META_TMP" 2>/dev/null
        fi
        rm -f "${MERGE_META_TMP:-}" 2>/dev/null

        [ -n "$FINAL_JSON" ] && printf '%s\n' "$FINAL_JSON"

        case "$MERGE_KIND" in
            coverage) finish "compact-injected" "standing=$STANDING_LOG_TOKEN" ;;
            lookback) finish "compact-lookback" "turn=$LB_TURN since=$LB_SINCE count=${LB_COUNT:-0} standing=$STANDING_LOG_TOKEN" ;;
            *) finish "compact-no-evidence" "standing=$STANDING_LOG_TOKEN" ;;
        esac
        ;;

    *)
        finish "source-not-handled"
        ;;
esac
