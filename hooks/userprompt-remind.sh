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
# This hook NEVER reads transcript_path, user_input, prompt, or
# last_assistant_message from the payload (ruling B; the addendum extends
# the not-read invariant to `prompt`, the alternate payload key Claude Code
# may use for the same field) -- only session_id, agent_id, agent_type, and
# prompt_id. prompt_id itself is never extracted, stored, or logged: only
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
# agent_id/agent_type presence, a prompt_id fingerprint) and the sorted
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
# passed, via one `memidx.py unmapped` call (self-healing) over the ledger's
# code-root paths, so the (comparatively expensive) classification only runs
# on turns that were already going to inject something.
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
finish() {
    local outcome="$1"
    local extra="${2:-}"
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
    # budget the existing latency test enforces.
    if [ "${DELIVERY_OPEN:-0}" = "1" ] && [ -z "$DELIVERY_CLOSE_DONE" ] \
        && [ "$outcome" != "injected" ] && [ "$outcome" != "lookback-injected" ]; then
        DELIVERY_CLOSE_DONE=1
        mc_update_state_json "$STATE_FILE" '
state["delivery_open"] = False
print(json.dumps(state))
' >>"$MC_LOG" 2>&1
    fi
    [ -n "$DECIDE_TMP" ] && rm -f "$DECIDE_TMP" 2>/dev/null
    if [ -n "$extra" ]; then
        mc_log "userprompt outcome=$outcome $extra session=${SESSION_ID:-}"
    else
        mc_log "userprompt outcome=$outcome session=${SESSION_ID:-}"
    fi
    exit 0
}

PAYLOAD="$(cat)"
[ -z "$PAYLOAD" ] && finish "empty-payload"

eval "$(mc_extract_fields "$PAYLOAD" session_id agent_id agent_type _prompt_hash _top_keys_csv)" 2>/dev/null
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
        "lookback_eligible": lookback_eligible,
        "since_turn": since_turn,
    })

decision["delivery_open"] = bool(state.get("delivery_open", False))

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
# delivery_open) --
# kept to a single call so the happy path doesn't add spawns on top of the
# 2s outer budget above.
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
}
for k, v in out.items():
    print(f"{k}={shlex.quote(v)}")
' "$DECIDE_TMP" 2>>"$MC_LOG")"

if [ "${DUPLICATE:-0}" = "1" ]; then
    finish "duplicate-delivery"
fi

if [ "${CANDIDATE:-0}" = "1" ]; then
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
' "$DECIDE_TMP" 2>>"$MC_LOG")

    # Phase 2 (outside the lock): the real, possibly-heavier classification call.
    UNMAPPED_JSON="{}"
    if [ "${#CODE_PATHS[@]}" -gt 0 ] && [ -n "${MEMCONTINUUM_ROOT:-}" ]; then
        ARGS=(unmapped)
        ARGS+=("${CODE_PATHS[@]}")
        ARGS+=(--root "$MEMCONTINUUM_ROOT" --project "$MC_PROJECT" --db "$MC_DB_PATH" --json)
        [ -n "${MEMCONTINUUM_CODE_ROOT:-}" ] && ARGS+=(--code-root "$MEMCONTINUUM_CODE_ROOT")
        RAW="$(env PYTHONPATH= "$MC_PY" "$MC_MEMIDX" "${ARGS[@]}" 2>>"$MC_LOG")"
        RC=$?
        # F1 (ruling 68): `unmapped` now exits 1 (not just 0) on a genuine
        # coverage_status still worth reading -- uninitialized/upgrade-
        # required/index-error all print a real JSON body on stderr-free
        # stdout, just with an empty unmapped list. Accept RC 0 or 1, then
        # log a distinct outcome per coverage_status so a still-degraded
        # index is visible in hook.log, not silently folded into whatever
        # the existing coverage_status != "ok" branch below already does.
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
                uninitialized)      mc_log "userprompt outcome=index-uninitialized session=${SESSION_ID:-}" ;;
                upgrade-required)   mc_log "userprompt outcome=index-upgrade-required session=${SESSION_ID:-}" ;;
                index-error)        mc_log "userprompt outcome=index-error session=${SESSION_ID:-}" ;;
                quarantined)        mc_log "userprompt outcome=index-quarantined session=${SESSION_ID:-}" ;;
            esac
            # Design R7 (audit MC-P2-03, TOP-0123 L7): a typed internal
            # error collapses coverage_status to "unknown" like any other
            # read failure (no new case arm there -- see 2.3 of the map),
            # but carries its own `degraded` object naming the reason. One
            # more hook.log token, same case-arm style as index-error
            # above, so `stats` can count it separately from a plain
            # unknown.
            DEGRADED_REASON="$(printf '%s' "$RAW" | env PYTHONPATH= "$MC_PY" -c '
import json, sys
try:
    d = (json.load(sys.stdin) or {}).get("degraded")
except Exception:
    d = None
print(d.get("reason_code", "") if isinstance(d, dict) else "")
' 2>/dev/null)"
            if [ -n "$DEGRADED_REASON" ]; then
                mc_log "userprompt outcome=index-degraded reason=${DEGRADED_REASON} session=${SESSION_ID:-}"
            fi
        fi
    fi

    CUR_CODE_SHA="$(mc_git_head "${MEMCONTINUUM_CODE_ROOT:-}")"
    CUR_STORE_SHA="$(mc_git_head "${MEMCONTINUUM_ROOT:-}")"
    START_CODE_SHA="$(env PYTHONPATH= "$MC_PY" -c '
import json, sys
try:
    with open(sys.argv[1]) as f:
        state = json.load(f)
except Exception:
    state = {}
print(state.get("start_code_sha") or "")
' "$STATE_FILE" 2>>"$MC_LOG")"
    START_STORE_SHA="$(env PYTHONPATH= "$MC_PY" -c '
import json, sys
try:
    with open(sys.argv[1]) as f:
        state = json.load(f)
except Exception:
    state = {}
print(state.get("start_store_sha") or "")
' "$STATE_FILE" 2>>"$MC_LOG")"

    if [ -n "$CUR_CODE_SHA" ] && [ "$CUR_CODE_SHA" != "$START_CODE_SHA" ]; then
        CODE_CHANGED="true"
    else
        CODE_CHANGED="false"
    fi
    if [ -n "$CUR_STORE_SHA" ] && [ "$CUR_STORE_SHA" != "$START_STORE_SHA" ]; then
        STORE_CHANGED="true"
    else
        STORE_CHANGED="false"
    fi

    OUTPUT_JSON="$(UNMAPPED_JSON="$UNMAPPED_JSON" CODE_CHANGED="$CODE_CHANGED" STORE_CHANGED="$STORE_CHANGED" \
        MEMCONTINUUM_ROOT="${MEMCONTINUUM_ROOT:-}" \
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


def yn(v):
    return "yes" if v else "no"


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

if not has_evidence:
    raise SystemExit(3)

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
        "hookEventName": "UserPromptSubmit",
        "additionalContext": ctx,
    }
}))
' 2>>"$MC_LOG")"
    OUT_RC=$?

    if [ $OUT_RC -eq 0 ] && [ -n "$OUTPUT_JSON" ]; then
        printf '%s\n' "$OUTPUT_JSON"

        # Phase 3 (locked): commit the injection bookkeeping now that we know
        # it actually happened. last_inject_ts mirrors last_inject_time (the
        # look-back's shared "since last inject of any kind" clock).
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
    fi
fi

# Coverage did not inject this turn (either it was never a candidate, or it
# was but classification found no evidence) -- evaluate the T-thin
# look-back. Coverage always wins; this is only ever reached once coverage
# has already declined to speak this turn.
if [ "${LB_ELIGIBLE:-0}" != "1" ]; then
    finish "no-evidence"
fi

LB_OUTPUT_JSON="$(SINCE_TURN="${SINCE_TURN:-0}" MEMCONTINUUM_ROOT="${MEMCONTINUUM_ROOT:-}" \
    env PYTHONPATH= "$MC_PY" -c '
import json, os

since = os.environ.get("SINCE_TURN", "0")
Q = chr(39)
store_root = os.environ.get("MEMCONTINUUM_ROOT") or "<store root not configured>"
fact = f"Look-back signal — {since} user turns with no edited-file evidence."
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
        "hookEventName": "UserPromptSubmit",
        "additionalContext": ctx,
    }
}))
' 2>>"$MC_LOG")"

if [ -z "$LB_OUTPUT_JSON" ]; then
    finish "no-evidence"
fi

printf '%s\n' "$LB_OUTPUT_JSON"

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
