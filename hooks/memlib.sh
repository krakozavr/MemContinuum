#!/usr/bin/env bash
# memlib.sh -- shared helpers for MemContinuum's write-side reminder hooks
# (ledger-post-edit.sh, precompact-persist.sh, sessionstart-remind.sh,
# userprompt-remind.sh, sessionend-stamp.sh). Source this from each hook
# script; it is never executed standalone.
#
# Contract mirrors pre-edit-chain.sh (docs/DESIGN.md SS8 / docs/DESIGN.md
# ruling F): hard-clear PYTHONPATH, absolute venv python, MEMCONTINUUM_* env,
# a single hook.log, fail-open on every path. Every hook that sources this is
# still individually responsible for its own final `exit 0` -- memlib.sh never
# exits or traps on the caller's behalf.
#
# macOS port (docs/DESIGN.md SS8 port note, 2026-08-30): stock macOS bash is
# 3.2 and ships neither `flock` nor `timeout`. This file used to shell out to
# both; it no longer uses either anywhere. Locking now lives inside the one
# python process mc_update_state_json already spawns (real fcntl.flock, a 2s
# non-blocking-retry deadline, atomic tmp+rename write, see below). The
# overall per-call deadline that `timeout 2` used to provide is now the job
# of each CALLING hook script's own watchdog guard (a tiny python launcher
# that runs the whole script in its own process group and kills the group on
# a 2s budget -- see the top of each hooks/*.sh file) -- so no helper in this
# file wraps its own subprocess calls in any per-call timeout any more; the
# caller's watchdog bounds the entire run, including every helper call made
# along the way.
#
# Env (project-agnostic; concrete values belong only in project wiring, e.g.
# .claude/settings.json or a *.json.example next to it -- never in this repo):
#   MEMCONTINUUM_HOME        base dir for the index db, hook.log, and session
#                        state ($MEMCONTINUUM_HOME/sessions/<project>/<id>.json).
#                        Defaults to ~/.memcontinuum (memidx.py's own default).
#   MEMCONTINUUM_PROJECT     project namespace passed to memidx.py --project.
#                        Defaults to $(basename "$MEMCONTINUUM_ROOT"), else
#                        "default" (memidx.py's own DEFAULT_PROJECT).
#   MEMCONTINUUM_ROOT        store markdown root (the decision-chain repo).
#   MEMCONTINUUM_CODE_ROOT   code root these hooks watch edits under (single-
#                        root fallback; see MEMCONTINUUM_CODE_ROOTS below).
#   MEMCONTINUUM_CODE_ROOTS  JSON array of every configured code root's
#                        physical path (design R5, audit MC-P1-05, TOP-0123
#                        L5). mc_code_roots() reads this first and falls
#                        back to the single MEMCONTINUUM_CODE_ROOT above
#                        when unset, so a project with one root never needs
#                        to set both.
#   MEMCONTINUUM_PYTHON      absolute path to the venv python. Falls back to
#                        <engine>/.venv/bin/python (see scripts/repo-init.sh
#                        --bootstrap-venv) when unset.
#
# WRITE-LOCK (ruling E): these scripts' only writable surface is
# $MEMCONTINUUM_HOME/sessions/**/*.json[.lock] and $MEMCONTINUUM_HOME/hook.log.
# Never write anything under MEMCONTINUUM_ROOT (the store) or any code root
# named by MEMCONTINUUM_CODE_ROOT or MEMCONTINUUM_CODE_ROOTS from any
# function in this file or any script that sources it.

export PYTHONPATH=

MC_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"

# mc_path_under_root lives in its own side-effect-free file (symlink-paths
# review round 1, finding 3) so hooks/newfile-nudge.sh can reach it WITHOUT
# paying everything below this line's cost -- see mc-path-lib.sh's own
# header. Sourcing it here costs every OTHER caller of this file nothing
# beyond defining one more function (no I/O, no side effects of its own).
# shellcheck source=mc-path-lib.sh
. "$MC_LIB_DIR/mc-path-lib.sh"

MC_MEMIDX="$MC_LIB_DIR/../memidx.py"
# Python resolution order (README.md "Requirements" / memcontinuum-setup.sh):
#   $MEMCONTINUUM_PYTHON -> $MEMCONTINUUM_HOME/config.sh -> <engine>/.venv/bin/python
#   -> (left unresolved; every caller here fails open, so a missing python
#   surfaces as a logged outcome, never a blocked hook -- see mc_log below and
#   each script's own finish()).
#
# The config.sh step exists because a venv does not have to live at
# <engine>/.venv: point --venv anywhere, or hand memcontinuum-setup.sh an existing
# --python, and the last fallback is wrong. Without this step every hook line
# in every project has to carry MEMCONTINUUM_PYTHON by hand, and the one that
# forgets fails silently -- observed in the field, hooks dead for two days
# behind a "no python resolved" line nobody was reading. config.sh is shell
# rather than JSON precisely so this costs a `.` and no interpreter.
# R2/R3 fix, round 4: a custom-HOME install also writes a minimal POINTER
# config.sh at the fixed default path (memcontinuum-setup.sh "3. config")
# that records only the real MEMCONTINUUM_HOME. Source the default/env
# path first; if that just redefined MEMCONTINUUM_HOME to a DIFFERENT
# directory than the file we sourced, it was a pointer -- follow through
# and source the REAL config.sh too, so MEMCONTINUUM_PYTHON actually
# resolves there instead of silently falling back to the engine venv (and
# so this script's own MC_LOG/MC_DB_PATH below land under the real HOME,
# not the default one -- R3). Unconditional on MEMCONTINUUM_PYTHON already
# being set: config.sh's own `if [ -z "${MEMCONTINUUM_PYTHON:-}" ]` guard
# keeps env/baked precedence for PYTHON either way. Both sources are
# fail-open (`|| true`): a missing/corrupt config.sh only costs sourcing
# time, never blocks the hook.
MEMCONTINUUM_HOME="${MEMCONTINUUM_HOME:-$HOME/.memcontinuum}"
MC_HOME_CONFIG_1="$MEMCONTINUUM_HOME/config.sh"
if [ -f "$MC_HOME_CONFIG_1" ]; then
    # shellcheck source=/dev/null
    . "$MC_HOME_CONFIG_1" 2>/dev/null || true
fi
# Re-default after every source: a damaged-but-sourceable config may have
# `unset MEMCONTINUUM_HOME`, and under `set -u` a bare expansion would
# kill the hook (regate round 2).
MEMCONTINUUM_HOME="${MEMCONTINUUM_HOME:-$HOME/.memcontinuum}"
if [ "$MEMCONTINUUM_HOME/config.sh" != "$MC_HOME_CONFIG_1" ] && [ -f "$MEMCONTINUUM_HOME/config.sh" ]; then
    # shellcheck source=/dev/null
    . "$MEMCONTINUUM_HOME/config.sh" 2>/dev/null || true
    MEMCONTINUUM_HOME="${MEMCONTINUUM_HOME:-$HOME/.memcontinuum}"
fi
unset MC_HOME_CONFIG_1
if [ -n "${MEMCONTINUUM_PYTHON:-}" ]; then
    MC_PY="$MEMCONTINUUM_PYTHON"
else
    MC_PY="$MC_LIB_DIR/../.venv/bin/python"
fi
MC_LOG="$MEMCONTINUUM_HOME/hook.log"
mkdir -p "$MEMCONTINUUM_HOME" 2>/dev/null || true

# Project resolution moved ABOVE the "no python resolved" check (liveness
# metric fix: memidx.py stats needs `project=` on every hook.log line,
# including this fail-open one -- MC_PROJECT costs nothing to compute this
# early, it is env/basename-only, no python involved).
MC_PROJECT="${MEMCONTINUUM_PROJECT:-}"
if [ -z "$MC_PROJECT" ]; then
    if [ -n "${MEMCONTINUUM_ROOT:-}" ]; then
        MC_PROJECT="$(basename "$MEMCONTINUUM_ROOT")"
    else
        MC_PROJECT="default"
    fi
fi

if [ ! -x "$MC_PY" ]; then
    printf '%s memlib: no python resolved (checked MEMCONTINUUM_PYTHON, %s, %s) -- run memcontinuum-setup.sh project=%s\n' \
        "$(date -Iseconds 2>/dev/null || date)" "$MEMCONTINUUM_HOME/config.sh" \
        "$MC_LIB_DIR/../.venv/bin/python" "$MC_PROJECT" >>"$MC_LOG" 2>/dev/null || true
fi

MC_DB_PATH="$MEMCONTINUUM_HOME/$MC_PROJECT.sqlite"

# mc_log MESSAGE -- append one timestamped line to hook.log, with
# `project=$MC_PROJECT` always appended at the end (liveness metric:
# memidx.py stats groups hook.log by project; every line from this shared
# path must carry one, matching pre-edit-chain.sh's own independent logger,
# which already stamps project= -- see that file's finish()). Never fails
# the calling hook (logging failure is swallowed, not propagated).
mc_log() {
    printf '%s %s project=%s\n' "$(date -Iseconds 2>/dev/null || date)" "$1" "$MC_PROJECT" >>"$MC_LOG" 2>/dev/null || true
}

# mc_cwd_token CWD -- prints the `cwd=<token>` hook.log field (INC-0130): the
# first 512 BYTES of CWD percent-encoded, keeping only [A-Za-z0-9/._~:!-]
# (every other byte, `%` and control characters included, becomes %XX in
# uppercase hex), `cwd=-` when empty. One whitespace-free, `=`-free token, so a
# path can neither forge a `key=value` field nor split the line. memidx.py's
# _log_path_token is the same encoder in Python (a test runs both on one
# input). Byte-wise under LC_ALL=C; bash 3.2-safe (no ${var@Q}, no assoc arrays); `& 255` undoes 3.2's sign-extended high bytes.
mc_cwd_token() {
    local s="${1:-}" out="" i n c v h had_lc="${LC_ALL+x}" old_lc="${LC_ALL-}"
    LC_ALL=C
    s="${s:0:512}"
    n=${#s}
    i=0
    while [ "$i" -lt "$n" ]; do
        c="${s:$i:1}"
        case "$c" in
            [A-Za-z0-9/._~:!-]) out="$out$c" ;;
            *) printf -v v '%d' "'$c"; printf -v h '%%%02X' $((v & 255)); out="$out$h" ;;
        esac
        i=$((i + 1))
    done
    if [ -n "$had_lc" ]; then LC_ALL="$old_lc"; else unset LC_ALL; fi
    printf 'cwd=%s' "${out:--}"
}

# mc_rotate_orphans_oldest_first -- lists every hook.log.rotating.* file
# next to $MC_LOG, oldest mtime first. Portable across GNU and BSD/macOS
# without arrays or a GNU-only/BSD-only reversal tool: `ls -t` sorts
# newest-first on both, and the awk one-liner just reverses that list (no
# `tac`, GNU-only; no `tail -r`, BSD-only). A missing directory or zero
# matches prints nothing, never an error -- the glob simply fails to
# expand and `ls`'s own stderr is discarded.
mc_rotate_orphans_oldest_first() {
    ls -1t "$MC_LOG".rotating.* 2>/dev/null | awk '{a[NR]=$0} END{for (i = NR; i >= 1; i--) print a[i]}'
}

# mc_rotate_shift_and_land SRC KEEP -- the rotation primitive shared by the
# normal path (SRC=the content just claimed from hook.log) and interrupted-
# shift recovery (SRC=a recovered dead-pid orphan, see mc_rotate_hook_log
# below): shifts .1..KEEP-1 up to .2..KEEP (dropping whatever sat at .KEEP,
# highest N first so a shift never overwrites a file before that file
# itself has been shifted along), then lands SRC at the now-free .1. Every
# shift step is independently fail-open (`|| true`) -- a single missing/
# unmovable link in the middle of the chain must never abort the ones
# after it; only the final `mv "$SRC" .1` result is ever surfaced to the
# caller.
#
# The shift only ever runs when .1 currently exists (round-2 review
# finding, MINOR -- Grok's worked example): if .1 is already missing, this
# is either the ordinary first-ever-rotation case (every slot below KEEP is
# empty too, so the shift would be an all-no-op regardless) OR a PRIOR call
# already shifted this exact chain to completion and was interrupted before
# landing its own SRC -- the shift loop's last step (n=1) is the one that
# vacates .1, so .1 missing is precisely the signal that "nothing is left
# to shift, only the land step remains". Running the shift AGAIN in that
# second case would shift the ALREADY-shifted chain a second time and drop
# one extra in-window file that should have survived (exactly the bug the
# finding describes: KEEP=3, .1/.2/.3 = A/B/C, a claim's shift completes
# in full -- B lands at .3 dropping C, A lands at .2, .1 now empty -- then
# the process is killed before landing its own SRC at .1; if the next
# rotation blindly re-shifted, it would move .2's A to .3, dropping B,
# which had every right to survive). When .1 IS present, the shift below
# is always safe to run unconditionally even over a PARTIALLY completed
# prior shift, because every already-vacated slot's own `[ -f ... ]` check
# makes that step a harmless no-op the second time through. KEEP=1 makes
# the loop itself a no-op (n starts at 0) regardless of this guard, so the
# final mv lands directly on .1 exactly as the original two-file-total code
# always did.
mc_rotate_shift_and_land() {
    local src="$1" keep="$2" n
    if [ -f "$MC_LOG.1" ]; then
        n=$((keep - 1))
        while [ "$n" -ge 1 ]; do
            if [ -f "$MC_LOG.$n" ]; then
                mv -f "$MC_LOG.$n" "$MC_LOG.$((n + 1))" 2>/dev/null || true
            fi
            n=$((n - 1))
        done
    fi
    mv -f "$src" "$MC_LOG.1" 2>/dev/null
}

# mc_rotate_hook_log -- eval-topic-logging section 5 (owner-approved
# add-on): nothing truncates or prunes hook.log on its own
# (mc_prune_old_state only ever clears session-state JSON) -- measured on a
# real machine, ~177 KB/day, so it grows unbounded without this, and every
# `memidx.py stats` run reads the whole file cold. If $MC_LOG is larger
# than MEMCONTINUUM_LOG_MAX_BYTES (default 5242880 = 5 MiB), it is rotated:
# the chain hook.log.1 (newest) .. hook.log.$MEMCONTINUUM_LOG_KEEP (oldest)
# is shifted up by one (.N -> .N+1, oldest dropped), then $MC_LOG's current
# content becomes the new hook.log.1 and a fresh, empty $MC_LOG is created.
# MEMCONTINUUM_LOG_KEEP (default 12) bounds how many rotated files are ever
# kept -- no file beyond .$MEMCONTINUUM_LOG_KEEP, no dated archive, no
# compression; data older than the oldest retained file is gone by design
# (memidx.py stats reads every hook.log.N it finds, N=1..KEEP, alongside
# hook.log itself). Lowering MEMCONTINUUM_LOG_KEEP is honored on the very
# next rotation: anything now beyond the new, smaller bound is pruned (see
# below), not merely left unreferenced. MEMCONTINUUM_LOG_KEEP=1 reproduces
# the pre-this-feature, hook.log.1-only policy exactly (hook.log.1 always
# replaced, never a .2). Sizing rationale: a real store's live hook.log
# measured ~5 MB of growth in 21 days (~240 KB/day), so the default
# 12 x 5 MiB bound gives roughly 8-9 months of retained history at that
# rate -- bounded and documented, not "archive forever" (an external
# review ruled out unbounded retention; this is the sized alternative).
#
# Called ONLY from sessionstart-remind.sh's own startup/resume/clear
# branch, once per session -- NEVER from mc_log above, or from
# pre-edit-chain.sh's own independent logger, both of which are hot append
# paths that must not gain a stat() call for this.
#
# Fail-open, like every other path in this file: a missing/unwritable
# $MEMCONTINUUM_HOME, a size that can't be read, or a concurrent session
# racing this same check all leave the log alone and return 0 -- this is
# best-effort housekeeping, never a correctness guarantee, and must never
# raise or block the calling hook. The rename-to-a-pid-unique-temp-name
# step below (rather than a direct `mv "$MC_LOG" "$MC_LOG.1"`) means at
# most ONE of two sessions racing this same rotation ever wins: the
# loser's own `mv "$MC_LOG" ...` simply fails (the winner already moved
# it) and returns cleanly -- only the winner ever reaches the shift logic
# below, so there is no shift-vs-shift race to guard against either.
mc_rotate_hook_log() {
    [ -f "$MC_LOG" ] || return 0

    local max_bytes="${MEMCONTINUUM_LOG_MAX_BYTES:-}"
    case "$max_bytes" in
        ''|*[!0-9]*) max_bytes=5242880 ;;
    esac

    local size
    size="$(wc -c <"$MC_LOG" 2>/dev/null)"
    size="${size//[[:space:]]/}"
    case "$size" in
        ''|*[!0-9]*) return 0 ;;
    esac
    [ "$size" -gt "$max_bytes" ] || return 0

    # KEEP validation (round-2 review finding, MAJOR): reject not just
    # empty/non-digit input but also a LEADING ZERO -- bash's own
    # arithmetic expansion ($(( ))) in mc_rotate_shift_and_land treats a
    # leading-zero operand as octal, so KEEP=08 aborts with an "invalid
    # octal digit" arithmetic error and KEEP=012 silently means 10, not 12.
    # By this point in the function $MC_LOG has NOT yet been claimed (the
    # claim is below), but an arithmetic error inside the shift-and-land
    # helper still must never happen: this repo's fail-open rule has no
    # trap/set -e safety net around it, and a raised arithmetic error would
    # abandon whatever `case`/`while` was running and skip every step after
    # it, including `touch "$MC_LOG"`. Reject any value outside [1, 1000]
    # too -- an unbounded KEEP (e.g. 999999999) would make the shift loop
    # in mc_rotate_shift_and_land spin roughly that many `[ -f ... ]`
    # iterations, an effectively infinite SessionStart hang under the
    # caller's own watchdog; 1000 is far beyond this feature's sizing
    # rationale (12) and still trivially cheap.
    local keep="${MEMCONTINUUM_LOG_KEEP:-}"
    case "$keep" in
        ''|0*|*[!0-9]*) keep=12 ;;
    esac
    if ! [ "$keep" -ge 1 ] 2>/dev/null || ! [ "$keep" -le 1000 ] 2>/dev/null; then
        keep=12
    fi

    local tmp="$MC_LOG.rotating.$$"
    mv "$MC_LOG" "$tmp" 2>/dev/null || return 0

    # Interrupted-shift recovery (round-2 review finding, MINOR):
    # sessionstart-remind.sh's own 2s watchdog can kill this function mid-
    # shift, after the claim just above has already renamed hook.log away
    # but before mc_rotate_shift_and_land finished landing it at .1 -- the
    # claimed content is then stranded forever as hook.log.rotating.<pid>,
    # invisible to every hook.log reader (_rotated_hook_log_paths in
    # memidx.py only ever looks at pure-digit suffixes) unless recovered
    # here (see mc_rotate_shift_and_land's own comment for how a naive
    # re-shift would additionally drop an in-window file that should have
    # survived, and why checking .1 there is enough to avoid it).
    #
    # Fix: before doing the normal shift-and-land for $tmp, sweep every
    # hook.log.rotating.<pid> belonging to a DEAD pid (`kill -0` failing --
    # a LIVE pid means another session's claim is genuinely in flight right
    # now this instant, never touch that one) and land each recovered
    # window through the exact same mc_rotate_shift_and_land primitive,
    # oldest mtime first. Ordering property this depends on, in the common
    # case: every such orphan is newer than whatever already sits at .1
    # (nothing ever lands at .1 except through this same claim-and-shift
    # path, and a dead orphan's claim ordinarily predates the claim just
    # made above) and older than $tmp (claimed an instant ago) -- so
    # processing oldest-orphan .. newest-orphan .. $tmp, each through its
    # own single shift-and-land call, reproduces exactly the chain an
    # uninterrupted sequence of individual rotations would have produced.
    # This is best-effort ordering, not a strict guarantee (round-2 review
    # NIT): if an earlier sweep skipped an orphan because its pid had been
    # recycled by an unrelated live process (a live-looking pid that isn't
    # really the original claim's), that orphan is left for a LATER
    # rotation to recover, and it can then land ahead of windows that were
    # actually claimed after it -- eviction order at a full retention
    # window can end up slightly off in that narrow case. Nothing is ever
    # lost and no stats row is miscounted either way (each line still
    # carries and is scanned by its own timestamp, independent of which
    # numbered file it physically sits in) -- only which specific window
    # gets evicted first at the KEEP boundary can be affected.
    local orphan pid
    # `while read` over a heredoc, never `for orphan in $(...)` (round-2
    # review finding, MINOR): the bare `for ... in $(...)` form is subject
    # to the shell's own word-splitting AND glob expansion of the command
    # substitution's output -- the only such unquoted expansion anywhere
    # in hooks/*.sh. A MEMCONTINUUM_HOME containing a space (reproduced:
    # ".../c7 with space") splits one real orphan path into two bogus
    # words, neither of which passes `[ -f "$orphan" ]`, so the orphan is
    # silently skipped and recovery no-ops with rc=0 -- exactly the kind
    # of silent failure this repo's fail-open rule is not supposed to
    # excuse. `read -r` takes each line whole, no splitting, no globbing;
    # the heredoc (not `<(...)` process substitution, not a pipe) keeps
    # this loop running in the CURRENT shell rather than a subshell, and
    # needs nothing beyond bash 3.2.
    while IFS= read -r orphan; do
        [ -f "$orphan" ] || continue
        [ "$orphan" = "$tmp" ] && continue
        pid="${orphan##*.rotating.}"
        case "$pid" in
            ''|*[!0-9]*) continue ;;
        esac
        kill -0 "$pid" 2>/dev/null && continue
        mc_rotate_shift_and_land "$orphan" "$keep"
    done <<EOF
$(mc_rotate_orphans_oldest_first)
EOF

    mc_rotate_shift_and_land "$tmp" "$keep" || return 0

    # Prune (round-2 review finding, MINOR): lowering MEMCONTINUUM_LOG_KEEP
    # after files beyond the new bound already exist must actually enforce
    # the new, smaller bound going forward -- the shift-and-land calls
    # above only ever touch .1..$keep (they have no reason to know about a
    # stale .5 left over from when KEEP was higher), so without this, a
    # lowered KEEP would silently leave the old, larger retention in place
    # forever.
    local n=$((keep + 1))
    while [ -f "$MC_LOG.$n" ]; do
        rm -f "$MC_LOG.$n" 2>/dev/null || true
        n=$((n + 1))
    done

    # `touch`, never `: >`/`>`: a concurrent writer (a second session's
    # mc_log/pre-edit-chain.sh append) can create a brand-new hook.log via
    # its own `>>` in the gap between the mv above and this line -- a bare
    # truncating redirect here would silently destroy that line the instant
    # it lands. `touch` creates the file when it's genuinely still missing
    # and is a no-op (never truncates) when it already exists.
    touch "$MC_LOG" 2>/dev/null || true
    return 0
}

# mc_state_dir_for PROJECT
mc_state_dir_for() {
    printf '%s/sessions/%s' "$MEMCONTINUUM_HOME" "$1"
}

# mc_state_file_for PROJECT SESSION_ID
mc_state_file_for() {
    printf '%s/%s.json' "$(mc_state_dir_for "$1")" "$2"
}

# mc_extract_fields PAYLOAD_JSON FIELD...
# Reads PAYLOAD_JSON on stdin (never argv, never an env var -- see dual-gate
# review finding 1 below) and prints one shlex-quoted `NAME=value` line per
# requested field,
# suitable for `eval "$(mc_extract_fields ...)"`. Supported field tokens:
# any top-level payload key (uppercased for the shell var name), plus the
# special "tool_input.file_path" -> FILE_PATH, "_prompt_hash" -> PROMPT_HASH
# (sha256(payload["prompt_id"])[:16] -- the raw prompt_id itself is never
# extracted or emitted, dual-gate review finding 2), "_top_keys_csv" ->
# TOP_KEYS_CSV (sorted top-level KEY NAMES only, comma-joined, never
# values -- the payload-shape capture addendum), and "_prompt_terms" ->
# PROMPT_TERMS plus a second line, PROMPT_SOURCE (TOP-0133 L2/L3, the
# prompt-query channel's own amendment to ruling B -- see below). Never
# reads transcript_path, user_input, prompt,
# or last_assistant_message beyond that one explicitly-requested,
# terms-only derivation on purpose -- a caller must not ask for the other
# tokens above (docs/DESIGN.md ruling B). The payload is
# piped to this one python's stdin only -- never placed in an env var or
# another process's argv (dual-gate review finding 1). No per-call timeout
# here (see the file header): the calling hook's own watchdog bounds this.
#
# "_prompt_terms" (TOP-0133 L2, ruling B amendment): reads
# payload["prompt"] first, payload["user_input"] as a fallback (whichever
# is a non-empty string -- Addendum A: real UserPromptSubmit payloads on
# this machine carry "prompt", never "user_input"). Before tokenizing
# anything (TOP-0133 L3), the lstripped text is checked against
# mc_text._MACHINE_PROMPT_PREFIXES -- Claude Code delivers subagent
# hand-back reports, background-task notifications, cross-session
# messages, and harness-injected system framing through this SAME event,
# framed with a fixed prefix (`<task-notification>`, `Another Claude
# session sent a message` (with or without a trailing colon),
# `A peer session sent a message`, `<agent-message`,
# `<cross-session-message`, `<system-reminder>`, `[SYSTEM NOTIFICATION`);
# a prefix match (never substring) there sets PROMPT_SOURCE=machine and skips tokenizing
# entirely (PROMPT_TERMS stays empty). Otherwise PROMPT_SOURCE=human and
# the text is tokenized with the SAME content-term vocabulary memidx.py's
# own `_content_terms` uses
# (mc_text.py, imported via a `sys.path.insert` onto MC_ENGINE_ROOT -- an
# env var scoped to THIS one subprocess call, never exported globally),
# lowercases, drops stopwords AND conversational filler (mc_text.py's own
# `_PROMPT_FILLER` -- fix round 1, Grok's judgement: applied here, and
# ONLY here, never inside `_content_terms` itself), drops tokens under 3
# characters, dedupes (first occurrence wins), and caps at 12. Prints an
# EMPTY value when
# fewer than 4 terms survive -- the "at least four content words" gate
# lives HERE, so a caller downstream of this function never sees a
# partial term list to second-guess. PROMPT_SOURCE stays "" when there is
# no prompt/user_input string at all. The prompt TEXT itself never leaves
# this one python process: it is read, classified, tokenized (when
# human-framed), and immediately discarded -- only PROMPT_TERMS and
# PROMPT_SOURCE (never argv, env, a file, state, or hook.log) are the
# return value. A caller must request this field only
# when the prompt-query channel is confirmed ON (see
# userprompt-remind.sh) -- requesting it unconditionally would defeat the
# whole point of an opt-in channel (a caller that never asks for
# "_prompt_terms" never triggers this branch at all, so the payload's
# prompt/user_input keys are never even looked at).
mc_extract_fields() {
    local payload="$1"
    shift
    printf '%s' "$payload" | env PYTHONPATH= MC_ENGINE_ROOT="$MC_LIB_DIR/.." "$MC_PY" -c '
import hashlib, json, os, sys, shlex
fields = sys.argv[1:]
try:
    d = json.load(sys.stdin)
except Exception:
    d = {}
if not isinstance(d, dict):
    d = {}
for f in fields:
    if f == "tool_input.file_path":
        v = (d.get("tool_input") or {}).get("file_path") or ""
        name = "FILE_PATH"
    elif f == "tool_input.notebook_path":
        # R5/R6 (audit MC-P1-05/MC-P1-04, TOP-0123 L5/T8): a NotebookEdit
        # payload carries notebook_path, not file_path -- added here so
        # Task 8 does not need to touch memlib.sh itself.
        v = (d.get("tool_input") or {}).get("notebook_path") or ""
        name = "NOTEBOOK_PATH"
    elif f == "_prompt_hash":
        pid = d.get("prompt_id") or ""
        v = hashlib.sha256(pid.encode()).hexdigest()[:16] if pid else ""
        name = "PROMPT_HASH"
    elif f == "_top_keys_csv":
        v = ",".join(sorted(d.keys()))
        name = "TOP_KEYS_CSV"
    elif f == "_prompt_terms":
        name = "PROMPT_TERMS"
        v = ""
        # PROMPT_SOURCE (TOP-0133 L3): "human" / "machine" / "" (no prompt
        # string at all). Printed explicitly right here -- one extra line,
        # from the SAME one process that already reads the prompt -- rather
        # than adding a second field token, so a caller never triggers a
        # second read of payload["prompt"]/["user_input"]. The eval this
        # feeds already handles multiple output lines per call.
        _source = ""
        _txt = d.get("prompt")
        if not (isinstance(_txt, str) and _txt):
            _txt = d.get("user_input")
        if isinstance(_txt, str) and _txt:
            try:
                _root = os.environ.get("MC_ENGINE_ROOT") or ""
                if _root:
                    sys.path.insert(0, _root)
                import mc_text
                if _txt.lstrip().startswith(mc_text._MACHINE_PROMPT_PREFIXES):
                    _source = "machine"
                    # Machine-framed text is never tokenized -- v stays "".
                else:
                    _source = "human"
                    _seen = set()
                    _toks = []
                    for _w in mc_text._CONTENT_TOKEN_RE.findall(_txt.lower()):
                        if (
                            _w in mc_text._STOPWORDS
                            or _w in mc_text._PROMPT_FILLER
                            or len(_w) < 3
                            or _w in _seen
                        ):
                            continue
                        _seen.add(_w)
                        _toks.append(_w)
                        if len(_toks) >= 12:
                            break
                    if len(_toks) >= 4:
                        v = " ".join(_toks)
            except Exception:
                v = ""
                _source = ""
        print(f"PROMPT_SOURCE={shlex.quote(_source)}")
        del _txt
    else:
        v = d.get(f)
        if v is None:
            v = ""
        name = f.upper()
    print(f"{name}={shlex.quote(str(v))}")
' "$@" 2>>"$MC_LOG"
}

# mc_code_roots -- prints one PHYSICAL code root per line, read by callers
# via the existing bash-3.2-safe idiom:
#   while IFS= read -r root; do ... ; done < <(mc_code_roots)
# (process substitution, never a pipe -- a pipe would run the loop in a
# subshell and drop any variable assignments made inside it). Design R5
# (audit MC-P1-05, TOP-0123 L5): parses MEMCONTINUUM_CODE_ROOTS (a JSON
# list, repo-init.sh's own esc_cmd(json.dumps(code_roots))) via ONE python
# call -- the same one-python-call discipline mc_extract_fields already
# uses, never a second process per root. Falls back to the single
# MEMCONTINUUM_CODE_ROOT when the list variable is unset (old-shape
# wiring rendered before this task, or a hand-written config) -- the list,
# when present, IS the complete set; the single var is a strict subset/
# legacy alias of it, never additional information, so this never reads
# both.
mc_code_roots() {
    if [ -n "${MEMCONTINUUM_CODE_ROOTS:-}" ]; then
        printf '%s' "$MEMCONTINUUM_CODE_ROOTS" | env PYTHONPATH= "$MC_PY" -c '
import json, sys
try:
    roots = json.load(sys.stdin)
except Exception:
    roots = []
if isinstance(roots, list):
    for r in roots:
        if isinstance(r, str) and r:
            print(r)
' 2>>"$MC_LOG"
    elif [ -n "${MEMCONTINUUM_CODE_ROOT:-}" ]; then
        printf '%s\n' "$MEMCONTINUUM_CODE_ROOT"
    fi
}

# mc_git_head DIR -- read-only; empty string if DIR is missing or not a repo.
# Never mutates DIR. No per-call timeout (see the file header).
mc_git_head() {
    local dir="$1"
    [ -n "$dir" ] && [ -d "$dir" ] || { printf ''; return; }
    git -C "$dir" rev-parse HEAD 2>>"$MC_LOG" || printf ''
}

# mc_code_heads_from CODE_ROOTS_TEXT -- prints "root<TAB>head\n" for every
# non-empty line of CODE_ROOTS_TEXT (mc_code_roots's own newline-separated
# output), via mc_git_head (no python -- git only). LOW-3 (task-7-review.md):
# shared by sessionstart-remind.sh, userprompt-remind.sh, and
# precompact-persist.sh, collapsing the per-root HEAD-reading loop that used
# to be duplicated (byte-for-byte in two of the three) across all three.
# Never spawns python itself -- the caller already paid for the ONE
# mc_code_roots call that produced CODE_ROOTS_TEXT, so folding this in adds
# no new spawn.
mc_code_heads_from() {
    local roots_text="$1"
    local cr
    while IFS= read -r cr; do
        [ -n "$cr" ] || continue
        printf '%s\t%s\n' "$cr" "$(mc_git_head "$cr")"
    done <<<"$roots_text"
}

# mc_head_changed STATE_FILE CODE_HEADS CUR_STORE_SHA FIRST_ROOT
#
# Prints "CODE_CHANGED=true|false", "STORE_CHANGED=true|false", and
# "MOVED_ROOTS=<root>\t<head>\n..." (shlex-quoted, suitable for
# `eval "$(...)"`) -- the shared "did any configured root's HEAD move"
# comparisons. LOW-3 (task-7-review.md): this ~30-line python heredoc used
# to be duplicated byte-for-byte in userprompt-remind.sh and
# precompact-persist.sh; now lives here once. CODE_HEADS is
# mc_code_heads_from's own output ("root<TAB>head\n" lines); STATE_FILE's
# `start_code_shas` ({root: sha}) is the per-root map sessionstart-remind.sh
# writes; `start_code_sha` (singular) is the pre-multi-root legacy value,
# recorded only for the FIRST configured root (repo-init.sh always renders
# MEMCONTINUUM_CODE_ROOT as code_roots[0] whenever any code root is
# configured, so FIRST_ROOT == that value identifies the one root the
# legacy key was ever measuring).
#
# LOW-4 fix (task-7-review.md): a root OTHER than FIRST_ROOT that is
# missing from `start_code_shas` (the transitional window before a resume
# repopulates the map -- see sessionstart-remind.sh's own header comment)
# is treated as UNKNOWN and skipped, never compared against a DIFFERENT
# root's start sha -- the pre-fix fallback compared every such root's
# current HEAD against the first root's own start sha (two unrelated git
# repositories), which could only ever read as a false "changed: yes".
#
# MOVED_ROOTS (TOP-0122 L1 rule 2a, the commit nudge): a SEPARATE, per-
# PROMPT comparison against `last_seen_heads` ({root: sha}, distinct from
# the per-SESSION `start_code_shas` above) -- folded into this same read
# (one state-file load, one CODE_HEADS scan) purely so the common "nothing
# moved" turn costs userprompt-remind.sh no extra python spawn at all. This
# is a CHEAP GATE only, not the authoritative decision: a root missing from
# `last_seen_heads` is treated as unknown and never reported moved (mirrors
# the LOW-4 policy above), and the caller re-derives the real comparison
# (and does the actual bookkeeping write) from a freshly-loaded state
# inside its own locked transform before acting on it -- this avoids ever
# trusting a value read outside a lock as the basis for a write.
mc_head_changed() {
    local state_file="$1"
    local code_heads="$2"
    local cur_store_sha="$3"
    local first_root="$4"
    CODE_HEADS="$code_heads" CUR_STORE_SHA="$cur_store_sha" MC_FIRST_ROOT="$first_root" \
        env PYTHONPATH= "$MC_PY" -c '
import json, os, shlex, sys

try:
    with open(sys.argv[1]) as f:
        state = json.load(f)
    if not isinstance(state, dict):
        state = {}
except Exception:
    state = {}

starts = state.get("start_code_shas")
if not isinstance(starts, dict):
    starts = {}
legacy_start = state.get("start_code_sha") or ""
last_seen = state.get("last_seen_heads")
if not isinstance(last_seen, dict):
    last_seen = {}
first_root = os.environ.get("MC_FIRST_ROOT") or ""
heads = os.environ.get("CODE_HEADS") or ""
code_changed = False
moved_lines = []
for line in heads.splitlines():
    if not line or "\t" not in line:
        continue
    root, cur = line.split("\t", 1)
    if root in starts:
        start = starts[root]
    elif root == first_root:
        start = legacy_start
    else:
        start = None
    if start is not None and cur and cur != start:
        code_changed = True

    prev = last_seen.get(root)
    if prev is not None and cur and cur != prev:
        moved_lines.append(root + "\t" + cur)

cur_store = os.environ.get("CUR_STORE_SHA") or ""
start_store = state.get("start_store_sha") or ""
store_changed = bool(cur_store) and cur_store != start_store

print("CODE_CHANGED=" + shlex.quote("true" if code_changed else "false"))
print("STORE_CHANGED=" + shlex.quote("true" if store_changed else "false"))
print("MOVED_ROOTS=" + shlex.quote("\n".join(moved_lines)))
' "$state_file" 2>>"$MC_LOG"
}

# mc_update_state_json STATE_FILE PY_TRANSFORM [DEADLINE_SECONDS]
#
# The one shared "lock + atomic-rename JSON update" primitive. Everything --
# acquiring the lock, loading the existing state, running the transform, and
# the atomic write -- happens inside ONE python process (macOS port: no
# `flock`/`timeout` binary involved anywhere). That process:
#   1. opens STATE_FILE.lock and takes a real fcntl.flock(LOCK_EX), retried
#      non-blocking every ~20ms up to DEADLINE_SECONDS (default 2.0, every
#      pre-existing caller's own unchanged behavior) -- exits 97 (mapped to
#      outcome=lock-timeout below) if the deadline passes without the lock.
#      Re-gate round 3, MAJOR 1: a caller whose OWN total budget is tight
#      (the search fallback's titling write, running under the SAME 2s
#      watchdog its query already spent most of) passes a short
#      DEADLINE_SECONDS instead of eating the full 2.0s default -- see
#      hooks/mc-fallback-lib.sh's own mc_fallback_write_state, the ONE
#      caller that does this today.
#   2. loads STATE_FILE (or {} if missing/corrupt/unreadable) into `state`.
#   3. runs PY_TRANSFORM (spliced in verbatim at column 0, exactly as
#      before) against it -- it may read any MC_*-prefixed env var the
#      caller exported beforehand, and must end by printing the new,
#      complete state object via `print(json.dumps(state))` (or print
#      nothing / exit non-zero to make this a no-op write).
#   4. via an atexit hook registered before the transform runs (so it fires
#      whether the transform falls through normally, calls sys.exit(N), or
#      raises), writes whatever was printed to a tmp file and os.replace()s
#      it onto STATE_FILE -- but only if something was actually printed --
#      then releases the lock.
# The transform never sees the existing state through an exported env var or
# argv (only via the `state` dict already loaded into that same process) --
# so a legacy or otherwise-sensitive key already sitting in a caller's
# persisted state (e.g. a pre-fingerprint-scheme raw prompt_id) is never
# inherited by any subprocess that python spawns (re-gate finding, HIGH; the
# same class of bug as dual-gate review finding 1, but for the EXISTING
# state rather than the incoming payload).
#
# Returns the transform's exit code (0 on a normal, evaluated write; a lock
# failure returns 1 after logging outcome=lock-timeout / outcome=lock-open-
# failed). A caller that also needs a *value* out of the transform (not just
# the persisted state) should have the transform write that value to a
# side-channel file of its own choosing (e.g. one named by an MC_*_OUT env
# var) and read it back itself afterward -- this function's own stdout/
# return value carries no such value.
mc_update_state_json() {
    local state_file="$1"
    local py_transform="$2"
    local deadline_s="${3:-2.0}"
    local state_dir
    state_dir="$(dirname "$state_file")"
    mkdir -p "$state_dir" 2>/dev/null || { mc_log "outcome=state-dir-failed dir=$state_dir"; return 1; }

    local rc
    env PYTHONPATH= MC_DEADLINE_S="$deadline_s" "$MC_PY" -c "
import atexit, fcntl, io, json, os, sys, time

state_file = sys.argv[1]
lockfile = state_file + '.lock'

try:
    lock_fd = os.open(lockfile, os.O_CREAT | os.O_RDWR, 0o644)
except OSError:
    sys.exit(98)

try:
    _deadline_s = float(os.environ.get('MC_DEADLINE_S', '2.0') or '2.0')
except ValueError:
    _deadline_s = 2.0
_deadline = time.time() + _deadline_s
_locked = False
while True:
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _locked = True
        break
    except OSError:
        if time.time() >= _deadline:
            break
        time.sleep(0.02)
if not _locked:
    os.close(lock_fd)
    sys.exit(97)

_buf = io.StringIO()
_real_stdout = sys.stdout


def _finalize():
    sys.stdout = _real_stdout
    new_json = _buf.getvalue()
    if new_json.strip():
        tmp = state_file + '.tmp.' + str(os.getpid())
        try:
            with open(tmp, 'w') as f:
                f.write(new_json)
            os.replace(tmp, state_file)
        except OSError:
            pass
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
    except OSError:
        pass
    os.close(lock_fd)


atexit.register(_finalize)

existing = '{}'
if os.path.isfile(state_file):
    try:
        with open(state_file) as f:
            existing = f.read() or '{}'
    except OSError:
        existing = '{}'
try:
    state = json.loads(existing)
    if not isinstance(state, dict):
        state = {}
except Exception:
    state = {}

sys.stdout = _buf
$py_transform
" "$state_file" 2>>"$MC_LOG"
    rc=$?

    case $rc in
        97) mc_log "outcome=lock-timeout file=$state_file.lock"; return 1 ;;
        98) mc_log "outcome=lock-open-failed file=$state_file.lock"; return 1 ;;
        *) return $rc ;;
    esac
}

# mc_prune_old_state PROJECT MINUTES -- deletes *.json state files older than
# MINUTES under $MEMCONTINUUM_HOME/sessions/PROJECT (mtime-based; a state
# file's mtime is its last write, i.e. its last activity). Never touches any
# other project's directory, and never touches MEMCONTINUUM_ROOT/CODE_ROOT.
mc_prune_old_state() {
    local project="$1"
    local minutes="$2"
    local dir
    dir="$(mc_state_dir_for "$project")"
    [ -d "$dir" ] || return 0
    find "$dir" -maxdepth 1 -type f -name '*.json' -mmin "+$minutes" -exec rm -f {} + 2>/dev/null || true
}

# mc_path_under_root now lives in mc-path-lib.sh (sourced near the top of
# this file) -- see that file's own header/doc comment.
