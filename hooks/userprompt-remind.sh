#!/usr/bin/env bash
# UserPromptSubmit hook: the live, mid-session reminder point. Injects a
# "Coverage signal" fact block via hookSpecificOutput.additionalContext when
# ALL of these hold (docs/DESIGN.md rulings A/C):
#
#   - agent_id/agent_type are NOT set (never inside a subagent, and never a
#     session run under `--agent`) -- fix-round 2026-08-31: this hook used to
#     also require source=="user", but a real UserPromptSubmit payload NEVER
#     carries a `source` field at all (that field belongs to SessionStart's
#     startup/resume/clear/compact/fork; the two events were confused --
#     docs: code.claude.com/docs/en/hooks). Empirically confirmed: 30/30 real
#     invocations in one session died as outcome=non-user-source. The gate
#     is gone; agent_id/agent_type (documented as present only under
#     `--agent` or inside a subagent) is the real "never in a subagent, never
#     the main thread run as a persona" signal.
#   - the ledger's evidence fingerprint GREW since the last injection --
#     defined as: the set of (path, content_sha256) pairs currently in the
#     ledger contains at least one pair that was not in the pair-set
#     captured at the last injection. A path re-edited with different
#     content counts as growth; an unchanged ledger does not.
#   - cooldown has elapsed: >= 3 user turns OR >= 15 minutes since the last
#     injection (whichever comes first -- Grok's OR wording)
#
# When coverage does NOT inject this turn (either it was never a candidate,
# or it was but classification found no evidence), this hook falls through
# to the T-thin look-back reminder instead (docs/DESIGN.md
# addendum 2026-08-30, "the look-back reminder"): fire when the conversation
# has advanced but the edit ledger has not --
#
#   since_turn = user_turn_count - max(last_inject_turn, last_growth_turn)
#   since_time = now - max(last_inject_ts, last_growth_ts)
#   thin = (since_turn >= 5) OR (since_time >= 1200)
#
# -- with NO per-session cap (owner ruling 2026-08-30 14:02: a cap means a
# decision made after the last permitted nudge is never asked about; the
# thin condition itself already prevents wallpaper during active building).
# `lookback_count` is tracked in state and logged on every look-back fire --
# it never gates eligibility. Coverage always wins: this hook never emits
# two blocks in the same turn. Every look-back write (per-turn here, and
# the SessionStart(compact) twin) also stamps last_inject_time -- coverage's
# own cooldown clock -- so a coverage candidate on the very next turn can't
# read a stale/zero last_inject_time and fire right through the cooldown
# that's supposed to follow a look-back (dual-gate review finding 4).
#
# The commit nudge (TOP-0122 L1 rule 2a; docs/INTERNALS.md "The commit
# nudge"): the moved-HEAD check itself runs on EVERY prompt, independent of
# coverage's own candidacy (ledger-growth/cooldown) gate -- only the actual
# nudge work (the `unmapped` call, the commit-message read, the fact line)
# is skipped on a turn that is neither a coverage candidate nor has any
# root moved. For each configured code root whose HEAD differs from
# state's last_seen_heads since the last prompt, reads that commit's own
# message (one `git log -1`, never the diff, never the prompt) and, when
# it names no decision id and this turn's own `unmapped` call already
# finds a file edited under that root during the session with no topic,
# adds one fact line and logs `outcome=commit-nudge` once per commit
# (state's `nudged_commits`). On a coverage-candidate turn the fact line
# joins coverage's own additionalContext block; on a turn that is not a
# candidate but a root moved, it is the WHOLE additionalContext (no
# coverage content), the turn's own outcome is `nudge-only`, and
# coverage's own cooldown/delivery bookkeeping is left untouched. It never
# gets a second turn, a second cooldown of its own, or a second `unmapped`
# call.
#
# This hook NEVER reads transcript_path or last_assistant_message from the
# payload (ruling B) -- only session_id, agent_id, agent_type, prompt_id,
# and the sorted top-level KEY NAMES (payload-shape capture, below). It
# also never reads user_input/prompt UNLESS the opted-in prompt-query
# channel (TOP-0133 L2, "Prompt-derived queries" below) is ON for this
# project, in which case ruling B is amended: mc_extract_fields may read
# payload["prompt"] (or ["user_input"] as a fallback) to derive search
# terms, in one process, and discard the text immediately -- only the
# resulting terms (never the text itself) ever leave that call, and only
# when PQ_ON=1 requests the "_prompt_terms" field at all (an OFF project's
# mc_extract_fields call stays byte-identical to the pre-L2 shape). See
# "Prompt-derived queries" further down and docs/INTERNALS.md for the
# full amendment. prompt_id itself is never extracted, stored, or logged: only
# sha256(prompt_id)[:16] ever exists past the one extraction call (dual-gate
# review finding 2), stored as `last_prompt_hash`, for dedupe only. A
# duplicate delivery (the same prompt_id redelivered) suppresses the WHOLE
# hook for that turn -- no turn advance, no coverage candidacy, no
# look-back eligibility, no injection of any kind -- UNLESS `delivery_open`
# is still true (re-gate finding, HIGH, Codex+Grok: the outer-timeout
# swallow case, where phase 1 committed this hash+turn but the invocation
# was killed before it ever produced stdout). In that case the redelivery
# is a RETRY, not a duplicate: it runs the normal decision path again, but
# without a second turn advance or hash re-stamp. Once a delivery actually
# completes (real stdout, or a genuinely-finished silent turn), delivery_open
# closes and any further same-hash redelivery goes back to full suppression.
#
# The raw payload (which may carry real prompt content under user_input/
# prompt) is read ONCE, on stdin, by the single field-extraction call
# (mc_extract_fields, memlib.sh) -- it is NEVER placed in an exported
# environment variable, so no subprocess this hook spawns (dirname/mkdir/
# cat/env/python) ever inherits it (dual-gate review
# finding 1, BLOCKER). Only the extracted scalar fields (session_id,
# agent_id/agent_type presence, a prompt_id fingerprint, and -- ON turns
# only -- the derived, terms-only PROMPT_TERMS) and the sorted
# top-level payload KEY NAMES (never values -- the payload-shape capture
# addendum below) ever exist past that call.
#
# Payload-shape capture (Codex fixture request): on this hook's first turn
# with a resolvable session_id and an already-started session (state file
# exists), logs the sorted list of top-level payload KEY NAMES ONLY (never
# values) as `payload_keys=...`, once per session -- BEFORE the
# agent_id/agent_type gate below (fix-round 2026-08-31: the previous
# placement was inside phase 1's locked transform, reached only once every
# earlier gate had already let the turn through; the source=="user" gate
# rejected every real payload before that point, so `payload_keys=` never
# once appeared in hook.log for a real session -- the exact evidence that
# would have shown the contract mismatch was itself gated behind the bug).
# The common-case (already logged this session) path is a plain grep
# against the state file, no subprocess -- only the genuinely-first turn
# pays for the one locked write.
#
# Evidence is computed LIVE here (not read from state.pending, which is only
# populated around a compaction) -- but only once cooldown+growth already
# passed, via ONE `memidx.py unmapped` call (self-healing) per such turn. The
# call carries only the code paths NEW since the previous prompt (or none at
# all: it is the index-state check and the stale self-heal either way; the
# contract is "the engine is consulted whenever the cache is": a prompt that
# is not a coverage candidate reads no cache and makes no call); the
# verdicts of the other paths come from the session's unmapped_cache, which is
# reused only while the store HEAD is unchanged, the store worktree is clean
# (git status --porcelain, untracked included), the call did not heal the
# index and the path was not edited again since its verdict (the ledger
# seen_at, fractional epoch seconds like classified_at, is not older than the
# whole second of classified_at); otherwise every path is reclassified and the cache
# rebuilt. The classification therefore only runs on turns that were already
# going to inject something, and costs only what is new.
#
# Always exits 0 and prints nothing on any failure (fail-open). This hook
# makes many SEQUENTIAL python calls with no timeout of their own any more
# (macOS port, 2026-08-30): the whole hook re-execs itself under an OUTER
# python watchdog on entry instead (see the guard just below) -- a single 2s
# wall-clock budget for the ENTIRE run, enforced by killing the guarded
# child's whole process group on expiry, so a chain of slow-but-not-hung
# calls summing past budget is bounded exactly the same way a single hung
# call is.
#
# Env: see hooks/memlib.sh.

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"

# Watchdog guard (macOS port, docs/DESIGN.md SS8 port note, 2026-08-30;
# deduped into hooks/mc-watchdog.sh, finding 1, 2026-08-31): this guard
# must be the LITERAL first thing this script does after `set -u` and
# resolving its own location -- in particular, strictly BEFORE sourcing
# memlib.sh (which does its own mkdir -p work). Sourcing mc-watchdog.sh
# itself is safe here: it costs at most one `[ -f ]` stat and sourcing a
# few config.sh assignment lines while resolving MC_GUARD_PY (F6 fix,
# round 4) -- no external process, no shelling out (see its own header),
# unlike memlib.sh, whose own body used to run unbounded on the
# outer, un-timed invocation before this fix -- a slow/hung memlib.sh
# could blow the whole invocation's wall time with no bound at all.
# Sourcing memlib.sh stays strictly AFTER this guard, i.e. only ever
# inside the guarded child, already under the watchdog below (see
# test_outer_deadline_covers_memlib_sourcing).
#
# Re-gate finding (MED, Codex), still true under the port: same reasoning,
# new mechanism. macOS bash 3.2 ships neither `timeout` nor `flock`, so the
# old `timeout 2 bash "$0"` re-exec is replaced with a tiny python launcher
# (mc-watchdog.sh's MC_WATCHDOG_LAUNCHER_PY): it starts this same script as
# a child in its own process group and kills the WHOLE group on a 2s
# wall-clock budget, so an orphaned grandchild (a hung python call several
# layers deep -- the exact failure the old MC_TIMEOUT_FG/--foreground dance
# in memlib.sh existed to prevent, or the launcher itself being killed --
# finding 1's SIGTERM/SIGINT/atexit fix) cannot outlive the deadline
# either. Every python call this script and memlib.sh's helpers make
# therefore needs no timeout of its own any more -- this one watchdog
# bounds the entire run (memlib.sh's MC_TIMEOUT_FG plumbing is gone along
# with every per-call `timeout`). Always exits 0; stdin/stdout/stderr are
# the real, inherited file descriptors (never piped through python), so
# passthrough is unbuffered. The re-exec'd child is started via `$BASH`
# (the invoking shell's own resolved path), never a bare literal `bash`,
# so a harness that overrides which bash interpreter runs this script
# (e.g. the bash-3.2 verification harness, tests/run_bash32.sh) is
# honored all the way down. If MEMCONTINUUM_PYTHON (or the venv fallback)
# does not resolve to an executable, or mc-watchdog.sh failed to source
# (MC_WATCHDOG_LAUNCHER_PY unset), this falls through UNGUARDED instead
# of exec-ing a dead path -- memlib.sh's own "no python resolved"
# detection then fires exactly as it would with no guard at all.
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
source "${MC_MEMLIB_PATH:-$SCRIPT_DIR/memlib.sh}"

DECIDE_TMP=""
DELIVERY_CLOSE_DONE=""
# TOP-0133 L2: PQ_TEXT holds the rendered guess (label + chain text) once
# the prompt-query channel finds a hit further down this script -- empty
# on every OFF/no-hit/excluded turn. PQ_EMITTED tracks whether that guess
# has already ridden inside one of the three existing additionalContext
# envelopes this hook can print (coverage, nudge-only, look-back), OR gone
# out standalone from finish() itself -- set immediately after whichever
# printf actually delivered it (never deferred to the state-append call;
# see pq_mark_delivered below, fix round 1b). When a turn reaches finish()
# with PQ_TEXT still non-empty and PQ_EMITTED still unset, the turn was
# about to end SILENTLY (no stdout at all) with an already-computed guess
# sitting unused -- finish() below emits it standalone rather than losing
# it, exactly the same "the guess is the feature, never let a later step
# lose it" precedent pre-edit-chain.sh's own fallback already set (see
# that hook's own re-gate round 3 comment). Declared here (before the
# first possible finish() call, under `set -u`) so referencing either one
# inside finish() is always safe.
PQ_TEXT=""
PQ_EMITTED=""

# pq_mark_delivered (fix round 1b, TOP-0133 L2, was MAJOR): the ONE place
# the search_fallbacks state write for a prompt-query hit happens -- called
# exactly once, from finish() itself, AFTER finish()'s own delivery_open
# close write (for "injected"/"lookback-injected" that close is the
# caller's own bookkeeping write, which already ran before finish() was
# even invoked; for every other outcome it is finish()'s own close block a
# few lines above the call site). Fix round 1 moved this append to after
# the PRINTF that delivers the guess -- correct against a kill landing
# before stdout, but a kill landing in the window between that append and
# the SEPARATE locked write that closes delivery_open (a few lines/callers
# away) still left delivery_open true with the hit already in
# search_fallbacks: the redelivered prompt then read as a RETRY, re-ran
# the search with that id excluded, and surfaced a SECOND topic for one
# prompt -- Grok's fixture, shrunk to this one lock round-trip. Fix round
# 1b's fix ordered this append AFTER whichever write closes delivery_open,
# never before.
#
# Fix round 2 (TOP-0133 L2, Grok NIT): ordering two SEPARATE locked writes
# still leaves a window between them in principle (a lock-wait timeout, a
# skipped close on some future outcome branch) where this append could run
# with delivery_open never actually closed. Since this call only ever
# happens once PQ_EMITTED is set -- i.e. the guess is already past
# stdout -- closing delivery_open is ALWAYS the correct thing to do here,
# so the append transform now does it itself (CLOSE_DELIVERY=1, the same
# locked write, never a second one): a genuinely-already-closed turn just
# re-writes the same `false` it already had; a turn whose separate close
# never landed gets closed right here instead. Either way, the redelivered
# prompt_id finds delivery_open already false, reads as a plain duplicate,
# and is suppressed -- never a second search, never a second topic. A kill
# after this append itself (guaranteed atomic with the close, one lock)
# now only ever loses the search_fallbacks TITLING entry (the look-back
# list simply omits this hit one turn longer).
#
# Fix round 3 (TOP-0133 L2, was MAJOR, Codex final26): rounds 1b/2 above
# ordered TWO writes relative to each other, but a kill can also land
# BEFORE either one runs at all -- after the printf already delivered the
# guess, but before this function (or finish()'s own close write) gets to
# execute or complete. That leaves delivery_open true with NOTHING
# written to search_fallbacks, so the actual guarantee against a second
# topic no longer lives here: it lives in the RETRY check near the top of
# the `if [ "$PQ_ON" = "1" ]` block, which skips the search outright on
# any redelivered prompt_id that finds delivery_open still true -- see
# that check's own comment. This ordering (append after whichever close,
# atomic with its own close) is still correct and still worth keeping: it
# is what makes search_fallbacks/look-back TITLING match what genuinely
# went to stdout. It just does not, by itself, bound how many topics one
# prompt can ever show -- the retry skip does that now, by construction.
pq_mark_delivered() {
    if [ -n "${SESSION_ID:-}" ] && [ -n "${FB_HITS_FOR_STATE:-}" ]; then
        mc_fallback_write_state "$STATE_FILE" "prompt" "$FB_HITS_FOR_STATE" "userprompt" 0.25 1
    fi
}

finish() {
    local outcome="$1"
    local extra="${2:-}"
    # TOP-0133 L2: standalone delivery. Printed BEFORE the delivery_open
    # close write just below (same stdout-first reasoning pre-edit-chain.sh's
    # own re-gate round 3, MAJOR 1 established: the close write's own lock
    # deadline could still lose an already-computed guess to the watchdog
    # if this ran after it). Guarded by PQ_EMITTED so this never
    # double-prints -- every one of the three merge points below sets
    # PQ_EMITTED=1 itself the moment it actually splices PQ_TEXT into its
    # own envelope, so by the time finish("injected"/"lookback-injected"/
    # "nudge-only") runs here, PQ_EMITTED is already set and this block is
    # a no-op for those three outcomes; it only ever fires for a turn that
    # would otherwise print nothing at all. On a real print, the outcome
    # token itself becomes "prompt-query-only" (Addendum C: a standalone
    # guess is not a nudge -- this must never sit silently under whatever
    # the turn's own would-be outcome was, e.g. "no-evidence"), and `extra`
    # is dropped (it described the turn's own now-superseded silent
    # reason). The search_fallbacks append itself no longer happens here --
    # fix round 1b (see pq_mark_delivered's own header comment) moved it to
    # AFTER the delivery_open close write below, run once at the bottom of
    # this function for every outcome.
    if [ -n "$PQ_TEXT" ] && [ -z "$PQ_EMITTED" ]; then
        PQ_STANDALONE_JSON="$(env PYTHONPATH= "$MC_PY" -c '
import json, sys
print(json.dumps({
    "hookSpecificOutput": {
        "hookEventName": "UserPromptSubmit",
        "additionalContext": sys.argv[1],
    }
}))
' "$PQ_TEXT" 2>>"$MC_LOG")"
        if [ -n "$PQ_STANDALONE_JSON" ]; then
            printf '%s\n' "$PQ_STANDALONE_JSON"
            PQ_EMITTED=1
            outcome="prompt-query-only"
            extra=""
        fi
    fi
    # Re-gate finding (HIGH, Codex+Grok): if phase 1 opened delivery this
    # invocation (DELIVERY_OPEN, decoded from the decision file above) and
    # we are about to exit through a SILENT-but-COMPLETED outcome (not
    # "injected"/"lookback-injected", which already close delivery
    # themselves as part of their own bookkeeping write), close it here so
    # a genuinely-finished turn (evidence considered and found wanting,
    # decision-failed, etc.) never leaves delivery_open dangling true for
    # a future redelivery to misread as a retry. Guarded to at most one
    # extra locked write per invocation, and skipped entirely (zero added
    # calls) whenever phase 1 never opened delivery -- the common case for
    # every payload without a prompt_id -- so this stays within the 2s
    # budget the existing latency test enforces. Addendum C: this write
    # touches delivery_open ONLY -- a standalone guess stamps nothing else
    # (not last_inject_turn/time/ts, not lookback_count).
    if [ "${DELIVERY_OPEN:-0}" = "1" ] && [ -z "$DELIVERY_CLOSE_DONE" ] \
        && [ "$outcome" != "injected" ] && [ "$outcome" != "lookback-injected" ]; then
        DELIVERY_CLOSE_DONE=1
        mc_update_state_json "$STATE_FILE" '
state["delivery_open"] = False
print(json.dumps(state))
' >>"$MC_LOG" 2>&1
    fi
    # Fix round 1b (TOP-0133 L2, was MAJOR): the search_fallbacks append for
    # a delivered guess happens HERE, once, AFTER the close write just
    # above -- for "injected"/"lookback-injected" the real close already
    # ran in the caller's own bookkeeping write before finish() was even
    # called, so this is still "after close" for every outcome. PQ_EMITTED
    # is the only guard needed (finish() always exits, so this function
    # body runs at most once per invocation) -- see pq_mark_delivered's own
    # header comment for the full reasoning. Fix round 2 (Grok NIT): the
    # append below also sets delivery_open=False in its own locked write
    # (pq_mark_delivered -> mc_fallback_write_state's CLOSE_DELIVERY arg),
    # so this still ends correctly even if the close write just above (or
    # the caller's own bookkeeping write, for "injected"/"lookback-
    # injected") never actually landed.
    if [ -n "$PQ_EMITTED" ]; then
        pq_mark_delivered
    fi
    [ -n "$DECIDE_TMP" ] && rm -f "$DECIDE_TMP" 2>/dev/null
    if [ -n "$extra" ]; then
        mc_log "userprompt outcome=$outcome $extra session=${SESSION_ID:-} $(mc_elapsed_field)"
    else
        mc_log "userprompt outcome=$outcome session=${SESSION_ID:-} $(mc_elapsed_field)"
    fi
    exit 0
}

# TOP-0133 L2: the opt-in switch, decided BEFORE the payload is even read
# (a plain env check plus, at most, one `grep -qxF` against a small file --
# no python, no state) -- see docs/INTERNALS.md "Prompt-derived queries".
# Fix round 1 (MINOR, Grok): `-qx` alone treats $MC_PROJECT as a REGEX
# matched against each line, not a literal string -- a project name
# containing a regex metacharacter (a bare `.` is the common one) then
# matches lines it should not (`grep -qx 'foo.bar'` matches a line
# `foo-bar`). `-F` makes the match literal.
# ON when MEMCONTINUUM_PROMPT_QUERY=1 (explicit override), or the literal
# string "0" forces OFF and wins over the file either way; otherwise ON
# only when $MEMCONTINUUM_HOME/prompt-query.projects exists and contains a
# line equal to $MC_PROJECT. This is what lets the field-extraction call
# just below skip requesting the derived-terms field ENTIRELY when OFF --
# the byte-identical guarantee for every project that never opts in.
PQ_ON=0
case "${MEMCONTINUUM_PROMPT_QUERY:-}" in
    1) PQ_ON=1 ;;
    0) PQ_ON=0 ;;
    *)
        if [ -f "$MEMCONTINUUM_HOME/prompt-query.projects" ] \
            && grep -qxF "$MC_PROJECT" "$MEMCONTINUUM_HOME/prompt-query.projects" 2>/dev/null; then
            PQ_ON=1
        fi
        ;;
esac

PAYLOAD="$(cat)"
[ -z "$PAYLOAD" ] && finish "empty-payload"

# TOP-0133 L2: "_prompt_terms" is appended to the requested field list
# ONLY when the switch above is ON -- when OFF, this call is byte-
# identical to the pre-L2 call (docs/DESIGN.md ruling B, unamended for
# every project that never opts in). mc_extract_fields (hooks/memlib.sh)
# is the ONLY place the raw prompt text is ever read; PROMPT_TERMS is the
# one derived, terms-only value this script itself ever sees.
MC_EXTRACT_FIELD_LIST="session_id agent_id agent_type _prompt_hash _top_keys_csv"
[ "$PQ_ON" = "1" ] && MC_EXTRACT_FIELD_LIST="$MC_EXTRACT_FIELD_LIST _prompt_terms"
eval "$(mc_extract_fields "$PAYLOAD" $MC_EXTRACT_FIELD_LIST)" 2>/dev/null
unset PAYLOAD

[ -z "${SESSION_ID:-}" ] && finish "no-session-id"

STATE_FILE="$(mc_state_file_for "$MC_PROJECT" "$SESSION_ID")"
[ -f "$STATE_FILE" ] || finish "no-state"

# Payload-shape capture (Codex fixture request, WRITE-HOOKS-CONSENSUS.md
# addendum point 4) -- deliberately BEFORE the agent_id/agent_type gate
# below (fix-round 2026-08-31, see header comment): a future contract
# mismatch must show up in hook.log even on a turn a later gate goes on to
# skip. Sorted top-level KEY NAMES only, never values, logged once per
# session. Fast path: a plain grep against the state file on disk (no
# subprocess) short-circuits every turn after the first; only a session's
# genuinely-first turn pays for the one locked write.
if ! grep -q '"payload_keys_logged"[[:space:]]*:[[:space:]]*true' "$STATE_FILE" 2>/dev/null; then
    export MC_LOG_PATH="$MC_LOG"
    export MC_TOP_KEYS_CSV="${TOP_KEYS_CSV:-}"
    export MC_PROJECT_ENV="$MC_PROJECT"
    mc_update_state_json "$STATE_FILE" '
import os

if not state.get("payload_keys_logged"):
    keys_csv = os.environ.get("MC_TOP_KEYS_CSV") or ""
    project = os.environ.get("MC_PROJECT_ENV") or ""
    try:
        with open(os.environ["MC_LOG_PATH"], "a") as lf:
            lf.write("payload_keys=" + keys_csv + " project=" + project + "\n")
    except OSError:
        pass
    state["payload_keys_logged"] = True

print(json.dumps(state))
' >>"$MC_LOG" 2>&1
fi

if [ -n "${AGENT_ID:-}" ] || [ -n "${AGENT_TYPE:-}" ]; then
    finish "agent-source"
fi

DECIDE_TMP="$(mktemp 2>/dev/null)" || finish "mktemp-failed"

export MC_DECIDE_OUT="$DECIDE_TMP"
export MC_NOW="$(date +%s 2>/dev/null || echo 0)"
export MC_PROMPT_HASH="${PROMPT_HASH:-}"

# Phase 1 (locked): bump the turn counter (prompt_hash-deduped), decide,
# from cheap in-state data alone, whether this turn is a coverage
# candidate, and also compute T-thin look-back eligibility from the same
# state snapshot. Writes the decision (+ the code-root ledger paths to
# classify, if any) to MC_DECIDE_OUT rather than stdout, since this
# function's stdout is reserved for the persisted state.
mc_update_state_json "$STATE_FILE" '
import hashlib, json, os, time

now = float(os.environ.get("MC_NOW") or time.time())

# Legacy-key purge (re-gate finding, HIGH, Codex): a pre-existing state
# file may still carry a raw last_prompt_id from before the sha256-
# fingerprint dedupe scheme existed (dual-gate review finding 2). Drop it
# unconditionally on every write, regardless of the dup/retry branch
# below -- it must never survive a state write, let alone reach a child
# env via the existing-state channel that mc_update_state_json itself
# feeds this transform on (fixed separately in memlib.sh: stdin now,
# never an exported env var).
state.pop("last_prompt_id", None)

# prompt_id dedupe: only a truncated sha256 fingerprint of prompt_id is
# ever stored (never the id itself -- dual-gate review finding 2). A
# duplicate delivery (a redelivered prompt_id) suppresses the WHOLE hook
# this turn: no turn advance, no coverage candidacy, no look-back
# eligibility, no injection of any kind -- not just the turn-count bump.
#
# Re-gate finding (HIGH, Codex+Grok), outer-timeout swallow: the hash and
# turn for this turn get committed to disk right here in phase 1, but the
# actual injection text is only rendered much later (phase 2/3, outside
# this lock). A deadline landing in between used to lose that prompts
# inject forever -- the next identical redelivery read as an ordinary duplicate
# and was suppressed with no chance to retry. `delivery_open` tracks
# whether a hash+turn commit is still waiting on a confirmed outcome: set
# True right here whenever a genuinely new hash is stamped, cleared False
# by every write that follows a successful stdout (phase 3, the look-back
# bookkeeping write) or by finish() itself for a silent-but-COMPLETED
# outcome (not a swallow -- see finish() below). A same-hash redelivery
# that finds delivery_open still True is a RETRY, not a duplicate: it
# re-runs the normal decision path but must not double-advance
# user_turn_count or re-stamp the hash. A same-hash redelivery that finds
# delivery_open already False (the prior run genuinely completed, whether
# or not it injected) stays a plain, fully-suppressed duplicate.
prompt_hash = os.environ.get("MC_PROMPT_HASH") or ""
is_dup = bool(prompt_hash) and prompt_hash == state.get("last_prompt_hash", "")
retry = is_dup and bool(state.get("delivery_open"))

decision = {"duplicate": is_dup and not retry, "retry": retry}

if (not is_dup) or retry:
    if retry:
        turn = state.get("user_turn_count", 0)
    else:
        turn = state.get("user_turn_count", 0) + 1
        state["user_turn_count"] = turn
        if prompt_hash:
            state["last_prompt_hash"] = prompt_hash
            state["delivery_open"] = True

    ledger = state.get("ledger") or []
    pairs = sorted(
        (e.get("path", ""), e.get("content_sha256", ""))
        for e in ledger
        if e.get("path")
    )
    last_pairs = {tuple(p) for p in (state.get("last_injected_pairs") or [])}
    grew = any(p not in last_pairs for p in pairs)

    last_turn = state.get("last_inject_turn", -999)
    # Re-gate finding (LOW, Grok): fall back to last_inject_ts when
    # last_inject_time is 0/absent -- mirrors the look-back logic below
    # (last_inject_ts falling back to last_inject_time), but in the other
    # direction, so an upgraded/partial state does not make coverage own
    # cooldown read as "never injected" when a real inject timestamp
    # exists under the other key.
    last_time = state.get("last_inject_time") or state.get("last_inject_ts") or 0
    cooldown_ok = (turn - last_turn) >= 3 or (now - last_time) >= 900

    candidate = bool(pairs) and grew and cooldown_ok

    code_paths = sorted({
        e["path"] for e in ledger if e.get("kind") == "code" and e.get("path")
    })
    # Latest edit time per code path, so a cached verdict older than its
    # path latest edit is not reused (see the classification cache below).
    code_seen_at = {}
    for e in ledger:
        if e.get("kind") == "code" and e.get("path"):
            t = e.get("seen_at")
            if isinstance(t, (int, float)) and not isinstance(t, bool):
                code_seen_at[e["path"]] = max(code_seen_at.get(e["path"], 0), t)

    # T-thin look-back candidacy (docs/DESIGN.md):
    # evaluated independently of coverage candidacy -- it must still fire
    # on turns where coverage never even became a candidate.
    # last_growth_ts falls back to created_at (session start), never epoch
    # 0, so a session with no edits yet does not read as "20 minutes
    # stale" on turn 1. last_inject_ts falls back to last_inject_time when
    # absent (dual-gate review finding 5: a pre-existing/upgraded session
    # state that predates this key would otherwise read as if it never
    # injected, even though a real coverage inject already stamped
    # last_inject_time).
    last_growth_turn = state.get("last_growth_turn", 0)
    last_growth_ts = state.get("last_growth_ts", state.get("created_at", now))
    lb_last_inject_turn = state.get("last_inject_turn", -999)
    lb_last_inject_ts = state.get("last_inject_ts")
    if not lb_last_inject_ts:
        lb_last_inject_ts = state.get("last_inject_time", 0)

    since_turn = turn - max(lb_last_inject_turn, last_growth_turn)
    since_time = now - max(lb_last_inject_ts, last_growth_ts)
    lookback_eligible = (since_turn >= 5) or (since_time >= 1200)

    decision.update({
        "candidate": candidate,
        "turn": turn,
        "now": now,
        "pairs": [list(p) for p in pairs],
        "code_paths": code_paths,
        "seen_at": code_seen_at,
        "unmapped_cache": state.get("unmapped_cache"),
        "lookback_eligible": lookback_eligible,
        "since_turn": since_turn,
    })

decision["delivery_open"] = bool(state.get("delivery_open", False))

# TOP-0133 L2 / Addendum B: the prompt-query exclusion set, computed here
# (state is already loaded under this same lock -- no extra process on
# the common, channel-OFF path) so the query further down needs no second
# state read of its own. Union of every id already surfaced this session
# via a search fallback (the L1 search_fallbacks list, written by
# pre-edit-chain.sh, newfile-nudge.sh, AND this channel itself) and every
# topic id the standing digest already delivered this session (state own
# standing_ids, written by sessionstart-remind.sh). Computed
# unconditionally (cheap: two already-in-memory list reads) regardless of
# whether the channel is ON this turn.
_sf_ids = {
    str(_e.get("id", "")) for _e in (state.get("search_fallbacks") or [])
    if isinstance(_e, dict) and _e.get("id")
}
_standing_ids = {str(_i) for _i in (state.get("standing_ids") or []) if _i}
decision["surfaced_ids"] = sorted(_sf_ids | _standing_ids)

try:
    with open(os.environ["MC_DECIDE_OUT"], "w") as f:
        json.dump(decision, f)
except OSError:
    pass

print(json.dumps(state))
' >>"$MC_LOG" 2>&1

if [ ! -s "$DECIDE_TMP" ]; then
    finish "decision-failed"
fi

# One shot: read every scalar the rest of this script needs out of the
# decision file (duplicate/candidate/turn/lookback_eligible/since_turn/
# delivery_open/retry) --
# kept to a single call so the happy path doesn't add spawns on top of the
# 2s outer budget above. RETRY (fix round 3, TOP-0133 L2, was MAJOR,
# Codex): phase 1 already computed `retry` (a same-prompt_hash redelivery
# while delivery_open was still true) -- exported here so the prompt-query
# block below can skip the search entirely on a retry turn instead of
# re-selecting. See that block's own comment for why.
eval "$(env PYTHONPATH= "$MC_PY" -c '
import json, sys, shlex
with open(sys.argv[1]) as f:
    d = json.load(f)
out = {
    "DUPLICATE": "1" if d.get("duplicate") else "0",
    "CANDIDATE": "1" if d.get("candidate") else "0",
    "TURN": str(d.get("turn", 0)),
    "LB_ELIGIBLE": "1" if d.get("lookback_eligible") else "0",
    "SINCE_TURN": str(d.get("since_turn", 0)),
    "DELIVERY_OPEN": "1" if d.get("delivery_open") else "0",
    "RETRY": "1" if d.get("retry") else "0",
    "SURFACED_IDS": ",".join(d.get("surfaced_ids") or []),
}
for k, v in out.items():
    print(f"{k}={shlex.quote(v)}")
' "$DECIDE_TMP" 2>>"$MC_LOG")"

if [ "${DUPLICATE:-0}" = "1" ]; then
    finish "duplicate-delivery"
fi

# --- prompt-derived search query (TOP-0133 L2) -------------------------
# Runs AFTER the duplicate-delivery gate and the agent gate above (a
# subagent/persona turn already `finish`ed at the agent_id/agent_type
# check well before this point; a redelivered prompt_id already
# `finish`ed just above) and BEFORE any coverage-candidate work below, so
# its own outcome (a guess, or nothing) is already known at every one of
# the three places this hook can still print additionalContext. Guard:
# the channel must be ON (PQ_ON, decided before the payload was even
# read) AND PROMPT_TERMS must be non-empty (mc_extract_fields already
# enforces the four-content-word gate; an empty value here means either
# zero terms or 1-3 of them -- this hook cannot and need not tell those
# apart, both log the same reason). PQ_TEXT stays "" (the channel found
# nothing, or never ran) unless a real hit survives below.
#
# RETRY (fix round 3, TOP-0133 L2, was MAJOR, Codex final26): checked
# FIRST, before even the term/root checks, and unconditionally skips the
# search. Codex reproduced a real gap in the close/append ORDERING fix
# (fix rounds 1b/2): a kill landing after the guess already reached
# stdout but before EITHER close write (finish()'s own, or
# pq_mark_delivered's atomic append+close) can still leave delivery_open
# true with the hit's own search_fallbacks entry never written -- the
# redelivered prompt_id then read as a RETRY, re-ran the search with
# nothing excluded, and could surface a SECOND, different topic
# (TOP-8002) for what is really the SAME prompt. The close/append
# ordering those earlier rounds built still matters -- it is what keeps
# search_fallbacks/look-back TITLING consistent with what actually went
# out on stdout -- but it can never by itself guarantee "at most one
# topic per prompt", because the kill window sits BEFORE either close
# write runs at all, not between them. Skipping the search on every
# RETRY turn closes that gap by construction instead: the first delivery
# either reached stdout (one topic, period) or it did not (zero); a
# retry never selects, so it can never add a second one. The cost is
# that a retry no longer redelivers the original guess either -- an
# acceptable trade (a lost guess, never a doubled one).
if [ "$PQ_ON" = "1" ]; then
    PQ_TERMS_LOGGED="${PROMPT_TERMS:-}"
    PQ_TERMS_LOGGED="${PQ_TERMS_LOGGED// /+}"
    if [ "${RETRY:-0}" = "1" ]; then
        mc_log "userprompt outcome=prompt-query-empty reason=retry q=$PQ_TERMS_LOGGED session=${SESSION_ID:-} $(mc_elapsed_field)"
    elif [ "${PROMPT_SOURCE:-}" = "machine" ]; then
        # TOP-0133 L3: Claude Code delivers subagent hand-back reports,
        # background-task notifications, and cross-session messages
        # through this SAME UserPromptSubmit event a typed prompt arrives
        # on. mc_extract_fields (memlib.sh) already classified the text
        # BEFORE tokenizing it and reports it here as PROMPT_SOURCE --
        # never tokenized, never searched, q= stays empty the same way
        # too-few-terms's own line does.
        mc_log "userprompt outcome=prompt-query-empty reason=non-user q= session=${SESSION_ID:-} $(mc_elapsed_field)"
    elif [ -z "${PROMPT_TERMS:-}" ]; then
        mc_log "userprompt outcome=prompt-query-empty reason=too-few-terms q= session=${SESSION_ID:-} $(mc_elapsed_field)"
    elif [ -z "${MEMCONTINUUM_ROOT:-}" ]; then
        mc_log "userprompt outcome=prompt-query-empty reason=store-root-unset q=$PQ_TERMS_LOGGED session=${SESSION_ID:-} $(mc_elapsed_field)"
    else
        PQ_LIB_OK=1
        # shellcheck source=mc-query-lib.sh
        source "$SCRIPT_DIR/mc-query-lib.sh" 2>/dev/null || PQ_LIB_OK=0
        if [ "$PQ_LIB_OK" = "1" ]; then
            # shellcheck source=mc-fallback-lib.sh
            source "$SCRIPT_DIR/mc-fallback-lib.sh" 2>/dev/null || PQ_LIB_OK=0
        fi
        if [ "$PQ_LIB_OK" != "1" ]; then
            mc_log "userprompt outcome=prompt-query-empty reason=lib-missing q=$PQ_TERMS_LOGGED session=${SESSION_ID:-} $(mc_elapsed_field)"
        else
            # ONE process, mirroring pre-edit-chain.sh's own FB_ARGS
            # exactly except `--mode fts` always (never
            # MEMCONTINUUM_FALLBACK_MODE -- this channel never touches the
            # embedding backend at all) and `--limit 3` (one more than
            # L1's own `--limit 2`, since an already-surfaced hit can
            # still consume a slot the exclusion filter below then has to
            # skip past).
            PQ_ARGS=(search "$PROMPT_TERMS" --mode fts --project "$MC_PROJECT" --db "$MC_DB_PATH" --limit 3 --json --hydrate --read-only --decisions-only)
            if [ -n "${MEMCONTINUUM_ROOT:-}" ]; then
                PQ_ARGS+=(--root "$MEMCONTINUUM_ROOT")
            fi
            # fb-started marker: same mechanism pre-edit-chain.sh/
            # newfile-nudge.sh already use (hooks/mc-watchdog.sh's own
            # kill handler checks for it), so a watchdog kill mid-search
            # on this channel is visible the same way theirs already is.
            PQ_STARTED_MARKER="$MEMCONTINUUM_HOME/.fb-started.$$"
            trap '[ -n "${PQ_STARTED_MARKER:-}" ] && rm -f "$PQ_STARTED_MARKER" 2>/dev/null' EXIT
            : >"$PQ_STARTED_MARKER" 2>/dev/null || true
            PQ_MS_T0="$(mc_now_ms "$MC_PY")"
            # Search subprocess stderr -> /dev/null, never hook.log (same
            # reason as L1's own fallback: an untimestamped stray line
            # would break the one-line-per-invocation contract).
            PQ_JSON="$(env PYTHONPATH= "$MC_PY" "$MC_MEMIDX" "${PQ_ARGS[@]}" 2>/dev/null)"
            PQ_RC=$?
            rm -f "$PQ_STARTED_MARKER" 2>/dev/null || true
            PQ_MS_T1="$(mc_now_ms "$MC_PY")"
            PQ_MS=""
            case "$PQ_MS_T0$PQ_MS_T1" in
                *[!0-9]*|"") ;;
                *) PQ_MS=$((PQ_MS_T1 - PQ_MS_T0)); [ "$PQ_MS" -lt 0 ] && PQ_MS=0 ;;
            esac
            PQ_MS_PART=""
            [ -n "$PQ_MS" ] && PQ_MS_PART=" pq_ms=$PQ_MS"

            # "the first hit whose id is not already surfaced this
            # session": EXCLUDE_IDS is SURFACED_IDS (search_fallbacks
            # union standing_ids, phase 1 above), MAX_HITS=1 (at most one
            # topic per prompt).
            mc_fallback_parse "$MC_PY" "$PQ_RC" "$MC_FB_LABEL_PROMPT" 0 "$PQ_JSON" "${SURFACED_IDS:-}" 1

            if [ -z "$FB_HITS" ] || [ "$FB_HITS" = "0" ]; then
                [ -z "$FB_REASON" ] && FB_REASON="no-hits"
                mc_log "userprompt outcome=prompt-query-empty reason=$FB_REASON q=$PQ_TERMS_LOGGED$PQ_MS_PART session=${SESSION_ID:-} $(mc_elapsed_field)"
            else
                PQ_TEXT="$FB_TEXT"
                mc_log "userprompt outcome=prompt-query hits=$FB_HITS ids=$FB_IDS q=$PQ_TERMS_LOGGED$PQ_MS_PART session=${SESSION_ID:-} $(mc_elapsed_field)"
                # No-write property: the search call above is
                # --read-only; the ONE state write this channel ever
                # makes is titling, same as L1 -- FILE_PATH is the
                # literal string "prompt" (there is no file), HOOK_NAME
                # is "userprompt", so the look-back block's existing
                # `search surfaced <title> (<id>) for <file>` rendering
                # names this channel's own hit correctly with no code
                # change to that block at all. Never stores the terms.
                # Fix round 1 (MAJOR), refined round 1b: NOT written here
                # any more -- FB_HITS_FOR_STATE (already set by
                # mc_fallback_parse above) stays a plain global variable,
                # untouched, until finish() calls pq_mark_delivered() (see
                # both of their own header comments) once, AFTER whichever
                # write closes delivery_open for this turn's outcome. A
                # write here, before any stdout, is exactly the ordering
                # Grok's original fixture broke: a kill landing after this
                # append but before delivery_open closes turns the next
                # same-prompt_id redelivery into a RETRY. Fix round 3
                # (Codex final26) is what actually bounds that RETRY to
                # never selecting a second topic -- it never reaches this
                # search call at all (see the RETRY check near the top of
                # this `if` block); this comment's own ordering only keeps
                # the eventual search_fallbacks entry consistent with what
                # stdout actually carried, never a second topic on its own.
            fi
        fi
    fi
fi

# Codex 9 (BLOCKING, fix wave 1 G4): a moved HEAD is now checked on EVERY
# prompt, independent of coverage candidacy (bool(pairs) and grew and
# cooldown_ok, above -- the LEDGER's own growth/cooldown signal, unrelated
# to whether a code root's commit history moved). The old placement lived
# entirely inside `if CANDIDATE=1`, so once a coverage reminder had already
# fired once (consuming `grew` against last_injected_pairs), a commit with
# no decision id followed by any number of further prompts with no NEW
# ledger growth produced zero commit nudges -- last_seen_heads never even
# advanced, because the moved-HEAD comparison itself never ran. Design R5
# (audit MC-P1-05, TOP-0123 L5): every configured code root, captured ONCE
# (one python spawn via mc_code_roots, memlib.sh) and reused below by BOTH
# the `unmapped` call and the per-root HEAD comparison -- a herestring-fed
# while-read loop (bash 3.2 safe, no array-of-roots to guard against
# `set -u`'s empty-array expansion). `git rev-parse HEAD` per root
# (mc_git_head, no python) is unavoidable (bash 3.2 cannot do a dict
# lookup without one), but the state-file lookup/comparison is ONE
# combined python call covering BOTH code (per-root map) and store
# (unchanged, single root) -- LOW-3/LOW-4 (task-7-review.md): both the
# per-root HEAD loop and this comparison live once in memlib.sh
# (mc_code_heads_from / mc_head_changed). MOVED_ROOTS itself is a CHEAP
# GATE only -- git rev-parse per root plus one python call, no `unmapped`
# invocation yet -- so the overwhelmingly common turn (no root moved, not
# a coverage candidate either) still costs zero extra spawns beyond this.
CODE_ROOTS_TEXT="$(mc_code_roots)"
CODE_HEADS="$(mc_code_heads_from "$CODE_ROOTS_TEXT")"
CUR_STORE_SHA="$(mc_git_head "${MEMCONTINUUM_ROOT:-}")"

# Safe defaults in case the python call below prints nothing (interpreter
# gone, unexpected crash) -- under `set -u` an unset CODE_CHANGED/
# STORE_CHANGED/MOVED_ROOTS would otherwise abort the hook with no log line.
CODE_CHANGED="false"
STORE_CHANGED="false"
MOVED_ROOTS=""
eval "$(mc_head_changed "$STATE_FILE" "$CODE_HEADS" "$CUR_STORE_SHA" "${MEMCONTINUUM_CODE_ROOT:-}")"

# The coverage candidacy gate stays, but ONLY for the coverage nudge itself
# (Codex 9's own wording) -- this block now also runs whenever a root
# actually moved, regardless of candidacy, so the moved-HEAD attribution
# (last_seen_heads advance, the commit-nudge count, nudged_commits dedup)
# is checked on every prompt a root moved on, never gated behind ledger
# growth.
if [ "${CANDIDATE:-0}" = "1" ] || [ -n "$MOVED_ROOTS" ]; then
    # bash 3.2 has no `mapfile`/`readarray` -- process substitution (never a
    # pipe, which would run the loop in a subshell and drop the assignments)
    # feeding a plain while-read loop is the portable equivalent.
    CODE_PATHS=()
    NEW_PATHS=()
    UC_REUSE=0
    UC_STARTED=""
    # The store worktree must be CLEAN for a cached verdict to be reused: an
    # uncommitted bind or a new topics/*.md is the usual filing state, and
    # waiting for the commit would be too late for the coverage nudge.
    # Untracked files count (a new topic is untracked until it is added).
    STORE_DIRTY=0
    if [ -n "${MEMCONTINUUM_ROOT:-}" ] && [ -n "$CUR_STORE_SHA" ]; then
        if [ -n "$(git -C "$MEMCONTINUUM_ROOT" status --porcelain --untracked-files=all 2>>"$MC_LOG")" ]; then
            STORE_DIRTY=1
        fi
    fi
    while IFS= read -r p; do
        case "$p" in
            P:*) CODE_PATHS+=("${p#P:}") ;;
            N:*) NEW_PATHS+=("${p#N:}") ;;
            R:*) UC_REUSE="${p#R:}" ;;
            T:*) UC_STARTED="${p#T:}" ;;
        esac
    done < <(env PYTHONPATH= "$MC_PY" -c '
import json, sys, time
with open(sys.argv[1]) as f:
    d = json.load(f)
# The session keeps the verdicts of the previous prompts (unmapped_cache).
# They are reusable only at the store HEAD they were made at AND while the
# store worktree is clean; a missing, malformed, other-HEAD or dirty-store
# cache means every path is new. A path whose latest ledger edit (seen_at)
# is not older than the whole second of its verdict (classified_at; seen_at
# may be a whole second from an older hook version) is new too: a re-edit, a
# symlink swap or a replace reclassifies that path alone. T: is the moment
# this classification starts (recorded as the verdict time of every path
# classified now, so an edit landing during the call is still newer).
cache = d.get("unmapped_cache")
sha, dirty = sys.argv[2], sys.argv[3] == "1"
verdicts, stamps = {}, {}
reuse = False
if sha and not dirty and isinstance(cache, dict) and cache.get("store_head") == sha \
        and isinstance(cache.get("verdicts"), dict) \
        and isinstance(cache.get("displays"), dict) \
        and isinstance(cache.get("classified_at"), dict):
    verdicts, stamps = cache["verdicts"], cache["classified_at"]
    reuse = True
seen = d.get("seen_at") if isinstance(d.get("seen_at"), dict) else {}
def num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else 0
print("R:" + ("1" if reuse else "0"))
print("T:" + repr(time.time()))
for p in d.get("code_paths", []):
    print("P:" + p)
    sv, cv = num(seen.get(p)), num(stamps.get(p))
    # A fractional seen_at (current ledger) compares strictly; a whole-second
    # one (an older hook version, stored as 1731.0 or 1731) keeps >= floor.
    newer = sv >= int(cv) if sv == int(sv) else sv > cv
    if p not in verdicts or newer:
        print("N:" + p)
' "$DECIDE_TMP" "$CUR_STORE_SHA" "$STORE_DIRTY" 2>>"$MC_LOG")

    # Phase 2 (outside the lock): the real, possibly-heavier classification call.
    UNMAPPED_JSON="{}"
    UC_CALLED=0
    UC_RAW=""
    UC_TMP=""
    UC_HEALED="false"
    COVERAGE_STATUS=""
    # One engine call: with the paths new since the last prompt, or with none
    # at all -- it is made whenever the cache is consulted, because the call is also the index-state
    # check and the stale self-heal that every reused verdict depends on.
    # Sets RC, UC_RAW, COVERAGE_STATUS and UC_HEALED; no logging (the caller
    # logs once, for the final answer).
    _uc_call() {
        local ARGS CR RAW
        ARGS=(unmapped)
        if [ "$#" -gt 0 ]; then ARGS+=("$@"); fi
        ARGS+=(--root "$MEMCONTINUUM_ROOT" --project "$MC_PROJECT" --db "$MC_DB_PATH" --json)
        while IFS= read -r CR; do
            [ -n "$CR" ] || continue
            ARGS+=(--code-root "$CR")
        done <<<"$CODE_ROOTS_TEXT"
        RAW="$(env PYTHONPATH= "$MC_PY" "$MC_MEMIDX" "${ARGS[@]}" 2>>"$MC_LOG")"
        RC=$?
        UC_RAW=""
        COVERAGE_STATUS=""
        UC_HEALED="false"
        # F1 (ruling 68): `unmapped` exits 1 (not just 0) on a coverage_status
        # still worth reading -- uninitialized/upgrade-required/index-error
        # all print a real JSON body with an empty unmapped list. Accept RC 0
        # or 1.
        if { [ $RC -eq 0 ] || [ $RC -eq 1 ]; } && [ -n "$RAW" ]; then
            UC_RAW="$RAW"
            { IFS= read -r COVERAGE_STATUS; IFS= read -r UC_HEALED; } < <(printf '%s' "$RAW" | env PYTHONPATH= "$MC_PY" -c '
import json, sys
try:
    d = json.load(sys.stdin) or {}
    print(d.get("coverage_status", "unknown"))
    print("true" if d.get("healed") is True else "false")
except Exception:
    print("unknown")
    print("false")
' 2>/dev/null)
        fi
    }
    # Drops the session's verdict cache in one short locked write.
    _uc_drop_cache() {
        mc_update_state_json "$STATE_FILE" '
state.pop("unmapped_cache", None)
print(json.dumps(state))
' >>"$MC_LOG" 2>&1
    }
    if [ "${#CODE_PATHS[@]}" -gt 0 ] && [ -n "${MEMCONTINUUM_ROOT:-}" ]; then
        UC_CALLED=1
        # A reusable cache is removed from the session file BEFORE the first
        # call (the merge below reads the snapshot taken at decision time, not
        # the file): the first call is the one that can heal the index, and a
        # kill anywhere between it and the write-back must leave NO cache for a
        # later prompt to reuse. It is republished after a good classification.
        if [ "$UC_REUSE" = "1" ]; then _uc_drop_cache; fi
        if [ "${#NEW_PATHS[@]}" -gt 0 ]; then _uc_call "${NEW_PATHS[@]}"; else _uc_call; fi
        # The call healed a stale index: the verdicts reused from earlier
        # prompts were made against the index as it was BEFORE the rebuild
        # (an unmapped may have just become bound), so every path is
        # reclassified against the healed one. The cache is already gone (above).
        if [ "$UC_HEALED" = "true" ] && [ "$UC_REUSE" = "1" ]; then
            UC_REUSE=0
            NEW_PATHS=("${CODE_PATHS[@]}")
            _uc_call "${NEW_PATHS[@]}"
        fi
        if [ -n "$UC_RAW" ]; then
            UNMAPPED_JSON="$UC_RAW"
            case "$COVERAGE_STATUS" in
                uninitialized)      mc_log "userprompt outcome=index-uninitialized session=${SESSION_ID:-} $(mc_elapsed_field)" ;;
                upgrade-required)   mc_log "userprompt outcome=index-upgrade-required session=${SESSION_ID:-} $(mc_elapsed_field)" ;;
                index-error)        mc_log "userprompt outcome=index-error session=${SESSION_ID:-} $(mc_elapsed_field)" ;;
                quarantined)        mc_log "userprompt outcome=index-quarantined session=${SESSION_ID:-} $(mc_elapsed_field)" ;;
            esac
            # Design R7 (audit MC-P2-03, TOP-0123 L7): a typed internal
            # error collapses coverage_status to "unknown" like any other
            # read failure, but carries its own `degraded` object naming the
            # reason. One more hook.log token so `stats` can count it
            # separately from a plain unknown.
            DEGRADED_REASON="$(printf '%s' "$UC_RAW" | env PYTHONPATH= "$MC_PY" -c '
import json, sys
try:
    d = (json.load(sys.stdin) or {}).get("degraded")
except Exception:
    d = None
print(d.get("reason_code", "") if isinstance(d, dict) else "")
' 2>/dev/null)"
            if [ -n "$DEGRADED_REASON" ]; then
                mc_log "userprompt outcome=index-degraded reason=${DEGRADED_REASON} session=${SESSION_ID:-} $(mc_elapsed_field)"
            fi
        fi
    fi

    # Incremental classification: fold the verdicts of the paths just
    # classified into the session's cache and rebuild the FULL answer the
    # rest of this hook reads (the lists, by_path, coverage_status), so the
    # message is the same whether a path was classified now or earlier. The
    # cache is replaced only on a coverage_status of "ok" and dropped
    # otherwise (and when the answer does not account for a fresh path); an
    # other-HEAD or dirty-store cache, a healed index and a re-edited path
    # already made their paths new above (UC_REUSE, NEW_PATHS).
    if [ "${#CODE_PATHS[@]}" -gt 0 ] && [ -n "${MEMCONTINUUM_ROOT:-}" ]; then
        UC_TMP="$(mktemp 2>/dev/null)" || UC_TMP=""
        if [ -n "$UC_TMP" ]; then
            UC_NEW_TEXT=""
            if [ "${#NEW_PATHS[@]}" -gt 0 ]; then UC_NEW_TEXT="$(printf '%s\n' "${NEW_PATHS[@]}")"; fi
            UC_MERGED="$(MC_UC_RAW="$UC_RAW" MC_UC_CALLED="$UC_CALLED" MC_UC_OUT="$UC_TMP" \
                MC_UC_REUSE="$UC_REUSE" MC_UC_STARTED="$UC_STARTED" MC_UC_NEW="$UC_NEW_TEXT" \
                env PYTHONPATH= "$MC_PY" -c '
import json, os, sys, time
with open(sys.argv[1]) as f:
    d = json.load(f)
sha = sys.argv[2]
code_paths = d.get("code_paths", [])
cache = d.get("unmapped_cache")
verdicts, displays, stamps = {}, {}, {}
had_cache = isinstance(cache, dict) and bool(cache)
new = {p for p in os.environ.get("MC_UC_NEW", "").splitlines() if p}
if os.environ.get("MC_UC_REUSE") == "1" and had_cache \
        and isinstance(cache.get("verdicts"), dict) and isinstance(cache.get("displays"), dict) \
        and isinstance(cache.get("classified_at"), dict):
    verdicts, displays = dict(cache["verdicts"]), dict(cache["displays"])
    stamps = dict(cache["classified_at"])
    for p in new:
        verdicts.pop(p, None)
        displays.pop(p, None)
        stamps.pop(p, None)
try:
    started = float(os.environ.get("MC_UC_STARTED") or time.time())
except ValueError:
    started = time.time()
called = os.environ.get("MC_UC_CALLED") == "1"
raw = None
if os.environ.get("MC_UC_RAW"):
    try:
        raw = json.loads(os.environ["MC_UC_RAW"])
    except Exception:
        raw = None
    if not isinstance(raw, dict):
        raw = None


def drop_cache():
    # {} in the file removes the stored cache.
    with open(os.environ["MC_UC_OUT"], "w") as f:
        f.write(json.dumps({}) if had_cache else "")


new_cache = None  # None: leave the stored cache alone
if called:
    if raw is None:
        print("{}")
        raise SystemExit(0)
    if raw.get("coverage_status") != "ok":
        # No coverage claim: the answer is the call own, the cache goes.
        print(json.dumps(raw))
        drop_cache()
        raise SystemExit(0)
    fresh = [p for p in code_paths if p not in verdicts]
    queues = {k: list(raw.get(k) or []) for k in ("mapped_topic", "mapped_concept_only", "unmapped")}
    by = raw.get("by_path") if isinstance(raw.get("by_path"), dict) else {}
    for p in fresh:
        st = by.get(p)
        if st not in queues or not queues[st]:
            # The answer does not account for this path: no claim, and no
            # cache either (the next prompt reclassifies everything).
            print(json.dumps(raw))
            drop_cache()
            raise SystemExit(0)
        verdicts[p] = st
        displays[p] = queues[st].pop(0)
        stamps[p] = started
    new_cache = {"store_head": sha, "verdicts": {p: verdicts[p] for p in code_paths},
                 "displays": {p: displays[p] for p in code_paths},
                 "classified_at": {p: stamps[p] for p in code_paths}} if sha else None
elif not verdicts:
    print("{}")
    raise SystemExit(0)
out = dict(raw) if raw else {}
out["coverage_status"] = "ok"
for k in ("mapped_topic", "mapped_concept_only", "unmapped"):
    out[k] = [displays[p] for p in code_paths if verdicts.get(p) == k]
out["by_path"] = {p: verdicts[p] for p in code_paths if p in verdicts}
print(json.dumps(out))
if new_cache is not None:
    with open(os.environ["MC_UC_OUT"], "w") as f:
        f.write(json.dumps(new_cache))
' "$DECIDE_TMP" "$CUR_STORE_SHA" 2>>"$MC_LOG")"
            if [ -n "$UC_MERGED" ]; then
                UNMAPPED_JSON="$UC_MERGED"
            fi
            if [ -s "$UC_TMP" ]; then
                MC_UC_FILE="$UC_TMP" mc_update_state_json "$STATE_FILE" '
import os
with open(os.environ["MC_UC_FILE"]) as _f:
    _c = json.load(_f)
if _c:
    state["unmapped_cache"] = _c
else:
    state.pop("unmapped_cache", None)
print(json.dumps(state))
' >>"$MC_LOG" 2>&1
            fi
            rm -f "$UC_TMP" 2>/dev/null
        fi
    fi

    # Commit nudge (TOP-0122 L1 rule 2a; ruling 140/144): MOVED_ROOTS (already
    # computed, unconditionally, above CANDIDATE's own gate) is a CHEAP GATE
    # only -- a root whose HEAD differs from state's last_seen_heads[root]
    # since the last prompt. When it is
    # non-empty, do the real work in ONE locked transform: re-derive the
    # authoritative comparison from a freshly-loaded state (never trust the
    # gate's own unlocked read for the write), read each moved root's new
    # commit message (subprocess, 2s timeout, failure -> skip that root
    # silently -- no traceback, no advance), and decide the nudge from the
    # SAME `unmapped` call already made above -- never a second `unmapped`
    # call, never the diff, never the prompt. last_seen_heads always
    # advances to the new HEAD for a root this turn actually read the
    # message for, whether or not it ends up nudging; nudged_commits is
    # bounded to the last 20. Nothing here runs when MOVED_ROOTS is empty --
    # the overwhelmingly common turn (no root moved) costs zero extra spawns
    # beyond mc_head_changed's own existing one.
    NUDGE_FACT_TEXT=""
    if [ -n "$MOVED_ROOTS" ]; then
        CODE_PATHS_TEXT=""
        if [ "${#CODE_PATHS[@]}" -gt 0 ]; then
            CODE_PATHS_TEXT="$(printf '%s\n' "${CODE_PATHS[@]}")"
        fi
        NUDGE_TMP="$(mktemp 2>/dev/null)"
        if [ -n "$NUDGE_TMP" ]; then
            export MC_CODE_HEADS="$CODE_HEADS"
            export MC_CODE_ROOTS_TEXT="$CODE_ROOTS_TEXT"
            export MC_CODE_PATHS_TEXT="$CODE_PATHS_TEXT"
            export MC_UNMAPPED_JSON="$UNMAPPED_JSON"
            export MC_NUDGE_OUT="$NUDGE_TMP"
            mc_update_state_json "$STATE_FILE" '
import json, os, re, subprocess
from pathlib import Path


# G9 (Codex 16 class carried into this hook): TOP-\\d+, not TOP-\\d{4} --
# SCHEMA.md own running example topic is id: TOP-42, two digits, and no
# fixed digit count is enforced on id: anywhere else in this store,
# matching memlint.py own decision-marker regex (fix wave 1 G2). A commit
# naming a genuinely shorter or longer id used to read as naming no
# decision at all and got wrongly nudged.
TOP_RE = re.compile(r"TOP-\d+")

code_heads = {}
for _line in (os.environ.get("MC_CODE_HEADS") or "").splitlines():
    if _line and "\t" in _line:
        _r, _h = _line.split("\t", 1)
        code_heads[_r] = _h

last_seen = state.get("last_seen_heads")
if not isinstance(last_seen, dict):
    last_seen = {}
nudged = state.get("nudged_commits")
if not isinstance(nudged, list):
    nudged = []
nudged_set = set(nudged)

# (resolved_root, original_config_string) pairs -- resolving mirrors
# memidx._code_roots_arg exactly (a code root is always matched resolved),
# but the ORIGINAL string is what code_heads/last_seen_heads key by, so a
# match reports that string back, never the resolved one.
code_roots = []
for _line in (os.environ.get("MC_CODE_ROOTS_TEXT") or "").splitlines():
    _line = _line.strip()
    if not _line:
        continue
    try:
        code_roots.append((Path(_line).resolve(), _line))
    except OSError:
        continue

code_paths = [p for p in (os.environ.get("MC_CODE_PATHS_TEXT") or "").splitlines() if p]

try:
    _unmapped_out = json.loads(os.environ.get("MC_UNMAPPED_JSON") or "{}")
    if not isinstance(_unmapped_out, dict):
        _unmapped_out = {}
except Exception:
    _unmapped_out = {}
# Codex 11 / Grok M6 (fix wave 1 G4): `unmapped` alone is a flat list of
# DISPLAY strings -- each relative to its own best root -- so two sibling
# code roots that happen to share a relative path (both have src/mapped.py,
# say) can produce the identical string from two entirely different
# physical files; membership-testing a display string against that flat
# set (the old `unmapped_set`) could then count -- or miss -- the wrong
# file of the two. `by_path` (memidx.py own addition, keyed by each
# absolute path exactly as this hook fed it into `unmapped PATH...`)
# resolves this unambiguously: this hook already knows the exact absolute
# path of every code_paths entry, so a per-path lookup needs no
# (root, relative_path) reconstruction at all -- no apostrophe anywhere in
# this comment block, since it lives inside a single-quoted bash string.
by_path = _unmapped_out.get("by_path")
if not isinstance(by_path, dict):
    by_path = {}


def _best_root_key(raw_path):
    try:
        resolved = Path(raw_path).resolve()
    except OSError:
        return None, None
    best_resolved = None
    best_key = None
    for _cr_resolved, _cr_key in code_roots:
        try:
            resolved.relative_to(_cr_resolved)
        except ValueError:
            continue
        if best_resolved is None or len(str(_cr_resolved)) > len(str(best_resolved)):
            best_resolved = _cr_resolved
            best_key = _cr_key
    if best_resolved is None:
        return None, None
    try:
        return best_key, str(resolved.relative_to(best_resolved))
    except ValueError:
        return None, None


nudge_out_lines = []
for root, cur in code_heads.items():
    if not cur:
        continue
    prev = last_seen.get(root)
    if prev is None or cur == prev:
        continue
    # root genuinely moved since the last prompt (freshly re-derived here,
    # under the lock -- never trusting the gate own unlocked read above).
    try:
        result = subprocess.run(
            ["git", "-C", root, "log", "-1", "--format=%B"],
            capture_output=True, text=True, timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        # git failed (root deleted, corrupt, timed out, ...) -- skip
        # silently; last_seen_heads does not advance for this root this
        # turn (self-healing: re-examined on the next prompt).
        continue
    if result.returncode != 0:
        continue

    message = result.stdout
    last_seen[root] = cur

    if TOP_RE.search(message):
        continue
    if cur in nudged_set:
        continue

    count = 0
    for raw in code_paths:
        _b, _rel = _best_root_key(raw)
        if _b == root and by_path.get(raw) == "unmapped":
            count += 1
    if count <= 0:
        continue

    short = cur[:7]
    root_name = os.path.basename(os.path.normpath(root)) or root
    # Codex 14 (fix wave 1 G4): `count` is the session ledger own
    # unmapped-file count under this root -- it was never derived from
    # this SPECIFIC commit own diff (no `git show`/`git diff-tree` runs
    # here at all) -- so "of ITS edited file(s)" claimed an attribution
    # this code never actually checked. The shipped positive test proved
    # it: it commits a file named newcommit.py while the count comes from
    # a ledger entry named unmapped.py, a file that commit never touched.
    # Reworded to describe what was actually measured: files edited under
    # this root during the SESSION (the ledger), not this commit own
    # tree -- the no-diff implementation (still no `git show` call) is
    # unchanged, only the claim now matches it. No apostrophe anywhere in
    # this comment block, since it lives inside a single-quoted bash string.
    fact = (
        "Commit " + short + " under " + root_name + " names no decision; "
        + str(count) + " file(s) edited under this root in the session "
        "have no topic — name the decision (TOP-xxxx Ln) in the message, "
        "or write the record."
    )
    nudged.append(cur)
    nudged_set.add(cur)
    nudge_out_lines.append(short + "\t" + root + "\t" + fact)

state["last_seen_heads"] = last_seen
state["nudged_commits"] = nudged[-20:]

try:
    with open(os.environ["MC_NUDGE_OUT"], "w") as f:
        for _l in nudge_out_lines:
            f.write(_l + "\n")
except OSError:
    pass

print(json.dumps(state))
' >>"$MC_LOG" 2>&1

            if [ -s "$NUDGE_TMP" ]; then
                while IFS=$'\t' read -r NUDGE_SHA NUDGE_ROOT NUDGE_FACT; do
                    [ -n "$NUDGE_SHA" ] || continue
                    mc_log "userprompt outcome=commit-nudge sha=$NUDGE_SHA root=$NUDGE_ROOT session=${SESSION_ID:-} $(mc_elapsed_field)"
                    if [ -n "$NUDGE_FACT_TEXT" ]; then
                        NUDGE_FACT_TEXT="$NUDGE_FACT_TEXT
$NUDGE_FACT"
                    else
                        NUDGE_FACT_TEXT="$NUDGE_FACT"
                    fi
                done <"$NUDGE_TMP"
            fi
            rm -f "$NUDGE_TMP" 2>/dev/null
        fi
    fi

    OUTPUT_JSON="$(UNMAPPED_JSON="$UNMAPPED_JSON" CODE_CHANGED="$CODE_CHANGED" STORE_CHANGED="$STORE_CHANGED" \
        MEMCONTINUUM_ROOT="${MEMCONTINUUM_ROOT:-}" MC_NUDGE_FACT_TEXT="$NUDGE_FACT_TEXT" \
        MC_CANDIDATE="${CANDIDATE:-0}" MC_PQ_TEXT="$PQ_TEXT" \
        env PYTHONPATH= "$MC_PY" -c '
import json, os

try:
    unmapped_out = json.loads(os.environ.get("UNMAPPED_JSON") or "{}")
    if not isinstance(unmapped_out, dict):
        unmapped_out = {}
except Exception:
    unmapped_out = {}

unmapped = unmapped_out.get("unmapped") or []
coverage_status = unmapped_out.get("coverage_status", "unknown")
code_changed = os.environ.get("CODE_CHANGED") == "true"
store_changed = os.environ.get("STORE_CHANGED") == "true"
is_candidate = os.environ.get("MC_CANDIDATE") == "1"
# TOP-0133 L2: the prompt-query channel own already-rendered guess (label
# + chain text), or "" on a turn it found nothing/never ran. Merged, never
# re-derived, into whichever branch below actually emits something --
# order is existing block first, then the guess (point 7).
pq_text = os.environ.get("MC_PQ_TEXT") or ""
# The commit nudge (TOP-0122 L1 rule 2a): computed just above, off the SAME
# unmapped call this fact_line already reads -- shares this same turn
# delivery/cooldown/dedupe rather than any nudge logic of its own.
nudge_lines = [l for l in (os.environ.get("MC_NUDGE_FACT_TEXT") or "").splitlines() if l]


def yn(v):
    return "yes" if v else "no"


# Codex 9 (fix wave 1 G4): this block now also runs on a turn that is NOT
# a coverage candidate, when a root moved (MOVED_ROOTS, checked by the
# bash `if` above this whole python invocation is nested in). On such a
# turn, the coverage fact_line/has_evidence/question below are the
# COVERAGE nudge own content -- coverage was never asked to fire this
# turn, so none of it belongs in the output; only nudge_lines (already
# computed, unconditionally, above) may justify emitting anything at all.
if not is_candidate:
    if not nudge_lines:
        raise SystemExit(3)
    nudge_ctx = "\n".join(nudge_lines)
    if pq_text:
        nudge_ctx += "\n\n" + pq_text
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": nudge_ctx,
        }
    }))
    raise SystemExit(0)

if coverage_status != "ok":
    fact_line = (
        "Coverage signal — decision-topic coverage unknown "
        f"(store index {coverage_status}); code HEAD changed: {yn(code_changed)}; "
        f"store HEAD changed: {yn(store_changed)}"
    )
    has_evidence = code_changed or store_changed
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
    has_evidence = (n > 0) or code_changed or store_changed

if not has_evidence and not nudge_lines:
    raise SystemExit(3)

store_root = os.environ.get("MEMCONTINUUM_ROOT") or "<store root not configured>"
question = (
    "Any ruling, incident, or rejected alternative from this session that "
    "the MemContinuum store should hold? Store: " + store_root + " — a ruling "
    "is a new link in topics/<area>/<topic>.md, an incident is a file in "
    "incidents/ (see docs/SCHEMA.md); NOT Claude Code auto-memory. If none, "
    "say so once."
)
ctx = fact_line
for _nl in nudge_lines:
    ctx += "\n" + _nl
ctx += "\n\n" + question
if pq_text:
    ctx += "\n\n" + pq_text
print(json.dumps({
    "hookSpecificOutput": {
        "hookEventName": "UserPromptSubmit",
        "additionalContext": ctx,
    }
}))
' 2>>"$MC_LOG")"
    OUT_RC=$?

    if [ $OUT_RC -eq 0 ] && [ -n "$OUTPUT_JSON" ]; then
        printf '%s\n' "$OUTPUT_JSON"
        # TOP-0133 L2 (fix round 1b): the guess (if any) was already merged
        # into OUTPUT_JSON above -- this printf is what actually delivered
        # it, so mark PQ_EMITTED now (finish() below never re-emits it
        # standalone for THIS turn's own "injected"/"nudge-only" outcome).
        # The search_fallbacks state append itself is deferred to finish()
        # -- it runs AFTER whichever write closes delivery_open for this
        # outcome (the phase-3 write a few lines below on the CANDIDATE=1
        # branch, or finish()'s own close write on the nudge-only branch),
        # never before -- see pq_mark_delivered's own header comment.
        # Serves both this and the nudge-only outcome -- they share this
        # one printf.
        [ -n "$PQ_TEXT" ] && PQ_EMITTED=1

        if [ "${CANDIDATE:-0}" = "1" ]; then
            # Phase 3 (locked): commit the injection bookkeeping now that we
            # know it actually happened. last_inject_ts mirrors
            # last_inject_time (the look-back's shared "since last inject of
            # any kind" clock). Codex 9: this must never run on a nudge-only
            # turn (CANDIDATE=0) -- it is coverage's OWN cooldown/dedupe
            # bookkeeping (last_injected_pairs, last_inject_turn/time/ts), and
            # a nudge-only turn resetting it would let a commit nudge quietly
            # re-arm coverage's cooldown clock without coverage ever actually
            # having fired.
            PAIRS_JSON="$(env PYTHONPATH= "$MC_PY" -c '
import json, sys
with open(sys.argv[1]) as f:
    d = json.load(f)
print(json.dumps(d.get("pairs", [])))
' "$DECIDE_TMP" 2>>"$MC_LOG")"

            export MC_PAIRS_JSON="$PAIRS_JSON"
            export MC_TURN_NUM="$TURN"
            export MC_NOW

            mc_update_state_json "$STATE_FILE" '
import json, os

try:
    pairs = json.loads(os.environ.get("MC_PAIRS_JSON") or "[]")
except Exception:
    pairs = []
try:
    turn = int(os.environ.get("MC_TURN_NUM") or 0)
except Exception:
    turn = state.get("user_turn_count", 0)
try:
    now = float(os.environ.get("MC_NOW") or 0)
except Exception:
    now = 0

state["last_injected_pairs"] = pairs
state["last_inject_turn"] = turn
state["last_inject_time"] = now
state["last_inject_ts"] = now
# Re-gate finding (HIGH, Codex+Grok): a confirmed stdout means this
# delivery is done -- close it so a later same-hash redelivery is a plain
# duplicate, not a retry.
state["delivery_open"] = False

print(json.dumps(state))
' >>"$MC_LOG" 2>&1

            finish "injected"
        else
            # Codex 9: a nudge fired on a turn coverage was never a
            # candidate on -- a genuine, distinct turn outcome (counted as a
            # real user prompt by stats; NOT the same thing as the
            # supplemental per-commit `outcome=commit-nudge` line(s) this
            # turn also already wrote, which stats excludes instead). finish
            # (not in its own "injected"/"lookback-injected" exemption list)
            # closes delivery_open on our behalf, same as any other
            # genuinely-finished turn.
            finish "nudge-only"
        fi
    fi
fi

# Coverage did not inject this turn (either it was never a candidate, or it
# was but classification found no evidence) -- evaluate the T-thin
# look-back. Coverage always wins; this is only ever reached once coverage
# has already declined to speak this turn.
if [ "${LB_ELIGIBLE:-0}" != "1" ]; then
    finish "no-evidence"
fi

# search-fallback look-back mention (TOP-0133 L1): reads STATE, never
# hook.log, and never re-searches. hook.log's own pre-edit-chain.sh
# `search-fallback` lines carry `session=` now (finish() puts it on every
# pre-edit line, `-` when the payload had none), but this block still reads
# STATE rather than scanning hook.log: STATE_FILE already IS this
# session's own file (mc_state_file_for, loaded once above), and both
# fallback-emitting hooks (pre-edit-chain.sh, newfile-nudge.sh) already
# append every hit they show to its `search_fallbacks` list, titled, the
# moment they fire -- so this block only ever READS that list, exactly
# ruling B's "reads state and hook.log, never the prompt" (unchanged: this
# is a state read, not a prompt read). Capped at 8, the same
# `unmapped[:8]` cap coverage's own fact_line above already uses, so one
# noisy session can't blow up the look-back block. This is what lets the
# orchestrator later bind a right guess through the normal record path --
# the hook itself never binds anything, only surfaces the guess again.
FB_LOOKBACK_LINES="$(env PYTHONPATH= "$MC_PY" -c '
import json, sys

try:
    with open(sys.argv[1]) as f:
        state = json.load(f)
    if not isinstance(state, dict):
        state = {}
except Exception:
    state = {}

fallbacks = state.get("search_fallbacks")
if not isinstance(fallbacks, list):
    fallbacks = []

def _one_line(s):
    # An env var cannot carry an embedded NUL at all (the OS environment
    # is NUL-terminated strings), so this join -- unlike every NUL-
    # delimited stdout/stdin transport elsewhere in these hooks -- uses
    # "\n" as the field separator instead, and each field is flattened to
    # one physical line first (a title/file value could in principle
    # embed a literal newline; collapsed to a space so it can never be
    # mistaken for a second entry downstream).
    return " ".join(str(s).split())

lines = []
for h in fallbacks[-8:]:
    if not isinstance(h, dict):
        continue
    title = _one_line(h.get("title", "") or "(untitled)")
    hid = _one_line(h.get("id", "") or "?")
    fpath = _one_line(h.get("file", "") or "?")
    lines.append(f"search surfaced {title} ({hid}) for {fpath}")

sys.stdout.write("\n".join(lines))
' "$STATE_FILE" 2>>"$MC_LOG")"

LB_OUTPUT_JSON="$(SINCE_TURN="${SINCE_TURN:-0}" MEMCONTINUUM_ROOT="${MEMCONTINUUM_ROOT:-}" HOOK_FB_LOOKBACK="$FB_LOOKBACK_LINES" \
    MC_PQ_TEXT="$PQ_TEXT" \
    env PYTHONPATH= "$MC_PY" -c '
import json, os

since = os.environ.get("SINCE_TURN", "0")
Q = chr(39)
store_root = os.environ.get("MEMCONTINUUM_ROOT") or "<store root not configured>"
fact = f"Look-back signal — {since} user turns with no edited-file evidence."
fb_lines = [l for l in os.environ.get("HOOK_FB_LOOKBACK", "").split(chr(10)) if l]
# TOP-0133 L2: same already-rendered guess as the coverage/nudge envelope
# above, merged in the same order (existing block first, then the guess).
pq_text = os.environ.get("MC_PQ_TEXT") or ""
question = (
    "Did the conversation since then establish any ruling, incident, "
    "rejected alternative, priority, wording choice, money decision, or "
    f"{Q}not now{Q} that the MemContinuum store should hold? Store: "
    + store_root + " — a ruling is a new link in topics/<area>/<topic>.md, "
    "an incident is a file in incidents/ (see docs/SCHEMA.md); NOT Claude "
    "Code auto-memory. If none, say so once."
)
ctx = fact
for fb in fb_lines:
    ctx += "\n" + fb
ctx += "\n\n" + question
if pq_text:
    ctx += "\n\n" + pq_text
print(json.dumps({
    "hookSpecificOutput": {
        "hookEventName": "UserPromptSubmit",
        "additionalContext": ctx,
    }
}))
' 2>>"$MC_LOG")"

if [ -z "$LB_OUTPUT_JSON" ]; then
    finish "no-evidence"
fi

printf '%s\n' "$LB_OUTPUT_JSON"

# TOP-0133 L2 (fix round 1b): the guess (if any) was already merged into
# LB_OUTPUT_JSON above -- this printf is what actually delivered it, so
# mark PQ_EMITTED now (finish() below never re-emits it standalone for
# this turn's own "lookback-injected" outcome). The search_fallbacks
# append itself is deferred to finish() -- it runs AFTER the bookkeeping
# write just below closes delivery_open, never before -- see
# pq_mark_delivered's own header comment. Deferring the append past this
# printf (fix round 1) is also what fixes the double-mention Grok
# measured: the old write ran right after the search, long before this
# look-back block even read search_fallbacks to render its own "search
# surfaced ... for prompt" lines a few statements above -- an exit-3
# candidate that was also look-back eligible used to see the SAME hit
# twice in one envelope (the look-back list line AND the guess itself,
# both computed off a state file the fresh hit had already landed in).
# The append landing only once this turn's own guess is already on its
# way out (now later still: after delivery_open closes) keeps that
# property -- the NEXT look-back turn is still the first one to list it.
[ -n "$PQ_TEXT" ] && PQ_EMITTED=1

export MC_TURN_NUM="${TURN:-0}"
export MC_NOW

mc_update_state_json "$STATE_FILE" '
import json, os

try:
    turn = int(os.environ.get("MC_TURN_NUM") or 0)
except Exception:
    turn = state.get("user_turn_count", 0)
try:
    now = float(os.environ.get("MC_NOW") or 0)
except Exception:
    now = 0

state["last_inject_turn"] = turn
# Stamp coverage own cooldown clock too (dual-gate review finding 4) --
# without this, a coverage candidate on the very next turn reads a
# stale/zero last_inject_time and its time-based cooldown OR-clause
# trivially passes, letting coverage fire right through the cooldown that
# is supposed to follow any injection, look-back included.
state["last_inject_time"] = now
state["last_inject_ts"] = now
# Re-gate finding (HIGH, Codex+Grok): a confirmed stdout means this
# delivery is done -- close it so a later same-hash redelivery is a plain
# duplicate, not a retry.
state["delivery_open"] = False
count = state.get("lookback_count", 0) + 1
state["lookback_count"] = count

# Side-channel the incremented count back to bash via the (by now
# already-consumed) decision file, so the log line below can include it
# (dual-gate review finding 7) without spawning another python call.
try:
    with open(os.environ.get("MC_DECIDE_OUT", ""), "w") as f:
        f.write(str(count))
except OSError:
    pass

print(json.dumps(state))
' >>"$MC_LOG" 2>&1

LB_COUNT="$(cat "$DECIDE_TMP" 2>/dev/null)"
# Re-gate finding (NIT, Grok): DECIDE_TMP is reused as this count's
# side-channel -- it still holds the STALE phase-1 decision JSON until the
# bookkeeping transform above overwrites it with a clean digit string. If
# that write never happens (e.g. the state write itself failed before
# reaching its own file-write line), the file keeps that stale JSON, and
# splicing it straight into the log line would leak the whole decision
# blob under `count=`. Validate the shape instead of trusting it.
if ! [[ "$LB_COUNT" =~ ^[0-9]+$ ]]; then
    LB_COUNT="?"
fi

finish "lookback-injected" "turn=${TURN:-0} since=${SINCE_TURN:-0} count=$LB_COUNT"
