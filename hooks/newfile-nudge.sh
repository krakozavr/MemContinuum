#!/usr/bin/env bash
# PreToolUse hook (matcher: Write ONLY -- never Edit): the deterministic
# new-file nudge. pre-edit-chain.sh only ever fires on a path that
# ALREADY EXISTS (`for-path` looks up governance for an existing file) --
# a brand-new file has no existing-file governance to trip, so nothing
# reminds the agent to check the code index before writing it. This hook
# is the mirror-image trigger: fires ONLY when tool_input.file_path does
# NOT exist yet (and isn't a symlink), sits under a configured code root,
# and has an indexed source extension -- and injects one line pointing at
# the same check memory-search's SKILL.md already asks for, at the one
# moment it's most likely to be skipped.
#
# Deliberately separate from pre-edit-chain.sh, and deliberately minimal:
# no vector, no index read, no memidx.py call of any kind (unlike every
# other hook in this repo). Whether the code index is actually
# initialized/current is left to the agent to confirm via `code-search`
# itself -- this hook cannot know that without paying for a python+sqlite
# read on every single Write, for a question code-search's own `state`
# field (finding 1) already answers on demand.
#
# Contract:
#   - reads the PreToolUse JSON payload on stdin, extracts tool_input.file_path
#   - fires ONLY when: the path does not exist AND is not a symlink (a
#     broken symlink is `! -e` but IS `-L` -- must stay silent, not be
#     treated as new), is under MEMCONTINUUM_CODE_ROOT, and ends in an
#     indexed source extension
#   - any other case, or any failure -> exit 0, no stdout (never blocks
#     the write)
#   - always runs under hooks/mc-watchdog.sh's shared wall-clock watchdog
#     (the same guard block every write-side hook uses) even though this
#     hook's own logic never calls python for real work -- one shared
#     mechanism, not a second bespoke timeout story for the one hook that
#     happens to be fast
#   - logs exactly one outcome line per invocation to
#     $MEMCONTINUUM_HOME/hook.log; never writes anything else, anywhere
#
# Env:
#   MEMCONTINUUM_CODE_ROOT   the code root this hook watches for new files.
#                            Required for this hook to ever fire (no code
#                            root configured -> always silent).
#   MEMCONTINUUM_HOME        base dir for hook.log. Defaults to
#                            ~/.memcontinuum (memidx.py's own default).
#   MEMCONTINUUM_PYTHON      absolute path to the venv python, used ONLY
#                            for the shared watchdog launcher and (when jq
#                            isn't on PATH) JSON handling -- never to run
#                            memidx.py.
#   MEMCONTINUUM_LANG_EXTS   space-separated glob list of engine-wired
#                            source extensions (e.g. "*.swift *.py"),
#                            rendered at hook-line render time (Task 10).
#                            UNSET -> legacy `*.swift`-only fallback, for
#                            wiring not yet re-rendered. Explicitly EMPTY
#                            (MEMCONTINUUM_LANG_EXTS='', rendered on
#                            purpose for language-less wiring -- Ruling 6)
#                            -> matches NOTHING, no fallback -- `${VAR-x}`
#                            (no colon) is used below specifically so
#                            "unset" and "set but empty" stay distinct.
#   MEMCONTINUUM_KNOWN_EXTS  space-separated glob list of ALL engine-
#                            supported extensions, wired or not. A file
#                            matching KNOWN but not WIRED logs
#                            outcome=language-available-not-wired instead
#                            of the plain not-indexed-extension. Unset ->
#                            falls back to MEMCONTINUUM_LANG_EXTS (no
#                            language-available-not-wired distinction).
#                            newlang-nudge (N1/N2): this case ALSO surfaces
#                            a one-line user-visible nudge -- naming the
#                            language, saying this project has not wired
#                            it, and pointing at the memcontinuum skill for
#                            the (unrestated) re-wiring procedure -- unless
#                            WIRED_EXTS is empty (language-less wiring,
#                            Ruling 6 above: there is no complete wired set
#                            to name yet, so nagging about every known
#                            extension would be noise, not help). Deduped
#                            once per language per SESSION, never
#                            permanently, via the same per-session state
#                            file every other write-side hook already
#                            shares (mc_state_file_for/mc_update_state_json,
#                            hooks/memlib.sh) -- reused rather than a new
#                            persistence layer because it is already
#                            session-scoped (no cleanup story needed) and a
#                            permanent "already told you" marker would mean
#                            a user who missed the line once never hears it
#                            again. memlib.sh is sourced LAZILY, only once
#                            this branch is already committed to (a brand
#                            new file in a known-but-unwired language,
#                            inherently rare) -- the common already-wired
#                            path below never pays for it, and never gains
#                            a subprocess it didn't have before.
#   MEMCONTINUUM_NEVER_EXTS  space-separated glob list of extensions the
#                            human answered "never" to at install time.
#                            Checked BEFORE the wired gate: a match
#                            finishes silently with
#                            outcome=never-extension, even for an
#                            otherwise-wired extension. Unset/empty ->
#                            matches nothing (no behavior change).

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"

# Watchdog guard (same pattern as every other hook here -- see
# userprompt-remind.sh's own guard-block comment for the full rationale):
# must be the literal first thing this script does after resolving
# SCRIPT_DIR and sourcing mc-watchdog.sh (see its own header for what
# running it costs).
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

export PYTHONPATH=
MEMCONTINUUM_HOME="${MEMCONTINUUM_HOME:-$HOME/.memcontinuum}"
# Python resolution order (matches hooks/memlib.sh; F6 fix, round 4):
#   $MEMCONTINUUM_PYTHON -> $MEMCONTINUUM_HOME/config.sh -> <engine>/.venv/bin/python
# Only used for JSON handling when jq isn't on PATH (see the header comment
# above) -- never to run memidx.py. The config.sh step is sourced (never
# sed/grep'd), swallowed by `|| true` so a missing/corrupt config.sh can
# only cost sourcing time, never block this hook. mc-watchdog.sh above
# already resolved MEMCONTINUUM_PYTHON via this same order for MC_GUARD_PY,
# so this second lookup is a no-op whenever that one already found one.
if [ -z "${MEMCONTINUUM_PYTHON:-}" ] && [ -f "$MEMCONTINUUM_HOME/config.sh" ]; then
    # shellcheck source=/dev/null
    . "$MEMCONTINUUM_HOME/config.sh" 2>/dev/null || true
fi
PY="${MEMCONTINUUM_PYTHON:-$SCRIPT_DIR/../.venv/bin/python}"
LOG="$MEMCONTINUUM_HOME/hook.log"
mkdir -p "$MEMCONTINUUM_HOME" 2>/dev/null || true

# Project resolution (liveness metric fix: memidx.py stats groups hook.log
# by project; matches memlib.sh's MC_PROJECT / pre-edit-chain.sh's own PROJECT
# resolution -- MEMCONTINUUM_PROJECT, else basename(MEMCONTINUUM_ROOT), else
# "default"). This hook resolves its own copy of PY/LOG/PROJECT rather than
# sourcing memlib.sh for them on the common path -- see hooks/mc-path-lib.sh
# below for the one function it needs unconditionally. newlang-nudge (N1):
# the one exception is the known-but-not-wired-language branch further
# down, which lazily sources memlib.sh (only once it is already on that
# rare branch) to reuse mc_state_file_for/mc_update_state_json for the
# nudge's per-session dedupe -- never on this common path, so the cost
# described above is unchanged for every already-wired write.
PROJECT="${MEMCONTINUUM_PROJECT:-}"
if [ -z "$PROJECT" ]; then
    if [ -n "${MEMCONTINUUM_ROOT:-}" ]; then
        PROJECT="$(basename "$MEMCONTINUUM_ROOT")"
    else
        PROJECT="default"
    fi
fi

FILE_PATH=""

log() {
    # never let logging itself fail the hook
    printf '%s\n' "$1" >>"$LOG" 2>/dev/null || true
}

finish() {
    # $1 = one-word outcome for the log line; $2 = optional extra
    # "key=value" token (sessionstart-remind.sh's own finish() established
    # this convention first -- mirrored here, not invented, for
    # newlang-nudge's nudge=shown/nudge=suppressed field). Placed between
    # outcome= and the trailing project=/file= fields, never after: file=
    # is last on purpose (a path can contain spaces), and appending
    # anything past it would land inside what a reader takes for the path.
    local extra="${2:-}"
    if [ -n "$extra" ]; then
        log "$(date -Iseconds 2>/dev/null || date) newfile-nudge outcome=$1 $extra project=${PROJECT:-} file=${FILE_PATH:-}"
    else
        log "$(date -Iseconds 2>/dev/null || date) newfile-nudge outcome=$1 project=${PROJECT:-} file=${FILE_PATH:-}"
    fi
    exit 0
}

# _emit_additional_context MESSAGE -- builds and prints the
# hookSpecificOutput/additionalContext JSON envelope both of this hook's
# nudges need (the wired-file reminder further down, and newlang-nudge's
# known-but-unwired-language nudge) -- one JSON-building implementation,
# not two. Prints the JSON and returns 0 on success; prints nothing and
# returns 1 on failure, leaving the caller to pick which outcome to log.
_emit_additional_context() {
    export HOOK_MESSAGE="$1"
    local output_json
    output_json="$(PYTHONPATH= "$PY" -c '
import json, os

print(json.dumps({
    "hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "additionalContext": os.environ["HOOK_MESSAGE"],
    }
}))
' 2>>"$LOG")"
    if [ -z "$output_json" ]; then
        return 1
    fi
    printf '%s\n' "$output_json"
    return 0
}

# --- read + parse the payload -------------------------------------------
PAYLOAD="$(cat)"

if [ -n "$PAYLOAD" ]; then
    if command -v jq >/dev/null 2>&1; then
        FILE_PATH="$(printf '%s' "$PAYLOAD" | jq -r '.tool_input.file_path // empty' 2>/dev/null)"
    else
        FILE_PATH="$(printf '%s' "$PAYLOAD" | "$PY" -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(1)
print(d.get("tool_input", {}).get("file_path", "") or "")
' 2>/dev/null)"
    fi
fi

if [ -z "$FILE_PATH" ]; then
    finish "no-file-path"
fi

# `-e` is false for a broken symlink too, so `-L` must be checked
# separately -- a symlink (broken or not) is never "a new file".
if [ -e "$FILE_PATH" ] || [ -L "$FILE_PATH" ]; then
    finish "existing-or-symlink"
fi

# Extension gate is render-time env, not a hardcoded list: Task 10 renders
# MEMCONTINUUM_LANG_EXTS (space-separated glob list of engine-wired
# extensions, e.g. "*.swift *.py") and MEMCONTINUUM_KNOWN_EXTS (all
# engine-supported globs, wired or not) into this hook's environment.
# UNSET MEMCONTINUUM_LANG_EXTS means legacy un-re-rendered wiring -- fall
# back to the original hardcoded `*.swift` so behavior stays byte-identical
# for a hook line Task 10 has not re-rendered yet. An explicitly EMPTY
# MEMCONTINUUM_LANG_EXTS (Ruling 6, Task 10 fix round: `MEMCONTINUUM_LANG_EXTS=''`
# rendered on the line, not omitted) is a DIFFERENT, deliberate state --
# language-less wiring, matching nothing -- and must NOT fall back to
# `*.swift`: `${VAR-default}` (dash, no colon) substitutes the default only
# when VAR is UNSET, leaving a set-but-empty VAR empty. This is why the
# expansion below is `${MEMCONTINUUM_LANG_EXTS-*.swift}`, not
# `${MEMCONTINUUM_LANG_EXTS:-*.swift}` (colon = "unset OR empty", which
# could never distinguish the two states Task 10 needs distinguished).
#
# _ext_matches loops over $2 deliberately UNQUOTED to split the
# space-separated glob list on IFS (bash-3.2 has no arrays-of-globs
# alternative) -- `set -f` (noglob) brackets the loop so that unquoted
# expansion never lets the shell glob-expand a pattern like *.swift
# against files in cwd; `case "$1" in $_pat)` itself is safe unquoted
# too (case patterns match, they never expand against the filesystem). An
# empty $2 makes the `for` loop iterate zero times (word-splitting an
# empty string yields no words), so `_ext_matches path ""` correctly
# returns 1 (no match) -- matches nothing, exactly what language-less
# wiring's explicit empty MEMCONTINUUM_LANG_EXTS needs.
_ext_matches() {  # $1=path  $2=space-separated glob list
    set -f
    for _pat in $2; do
        case "$1" in $_pat) set +f; return 0 ;; esac
    done
    set +f
    return 1
}

# _lang_name_for -- newlang-nudge (N1): a human-readable language name for
# the nudge's "name the language" requirement, e.g. ".ts" -> "typescript".
# Deliberately a bash `case` table, not a memidx.py/chunkers call: this
# hook never calls memidx.py for real work (see the file header), and this
# branch is already the rare one -- but a python subprocess just to look
# up a display name is still one more thing to fail, and this hook's own
# design principle is to stay simple enough to reason about without one.
# Mirrors chunkers.LANGUAGE_TABLE (chunkers/__init__.py) row-for-row, since
# the extension is not always a substring of the language name (.ts is
# "typescript", .rs is "rust", four JS extensions are all "javascript") --
# tests/test_write_hooks.py::
# test_language_name_matches_the_engine_registry_for_every_known_language
# runs the real hook against every LANGUAGE_TABLE row and fails the moment
# a new row (or a new extension on an existing row) has no arm here, so
# this table cannot drift unnoticed the way INC-0117's copy did. An
# extension with no arm (this engine version's KNOWN_EXTS should never
# produce one, but render-time env is not proof) falls back to the raw
# extension text -- correct but unhelpfully terse is better than wrong.
_lang_name_for() {  # $1=path  $2=space-separated glob list (KNOWN_EXTS)
    set -f
    for _pat in $2; do
        case "$1" in
            $_pat)
                set +f
                case "$_pat" in
                    "*.swift") printf 'swift' ;;
                    "*.py") printf 'python' ;;
                    "*.js"|"*.jsx"|"*.mjs"|"*.cjs") printf 'javascript' ;;
                    "*.ts") printf 'typescript' ;;
                    "*.tsx") printf 'tsx' ;;
                    "*.java") printf 'java' ;;
                    "*.php") printf 'php' ;;
                    "*.rs") printf 'rust' ;;
                    "*.lua") printf 'lua' ;;
                    *) printf '%s' "${_pat#\*.}" ;;
                esac
                return 0
                ;;
        esac
    done
    set +f
    printf '%s' "${1##*.}"
    return 1
}
WIRED_EXTS="${MEMCONTINUUM_LANG_EXTS-*.swift}"
KNOWN_EXTS="${MEMCONTINUUM_KNOWN_EXTS:-$WIRED_EXTS}"

# MEMCONTINUUM_NEVER_EXTS is checked FIRST, before the wired gate (B4,
# Anatomy M1 fix wave): the install dialogue's "never for one extension"
# answer means "stop mentioning this one", and it has to win even over an
# extension that is otherwise wired -- otherwise the answer would only
# work for extensions the hook was already silent about, which is no
# answer at all. Empty or unset matches nothing (an empty $2 makes
# _ext_matches' `for` loop iterate zero times), so wiring that never asked
# the question behaves exactly as before. This is render-time persistence:
# the value lives on the hook's own command line, refreshed by every
# install; there is no registry behind it, so it holds for this wiring only.
NEVER_EXTS="${MEMCONTINUUM_NEVER_EXTS:-}"
if [ -n "$NEVER_EXTS" ] && _ext_matches "$FILE_PATH" "$NEVER_EXTS"; then
    finish "never-extension"
fi

if ! _ext_matches "$FILE_PATH" "$WIRED_EXTS"; then
    if _ext_matches "$FILE_PATH" "$KNOWN_EXTS"; then
        # newlang-nudge (N1/N2): the DETECTION outcome below is unchanged
        # from before this feature existed (literal string pinned by
        # memidx.py's stats and tests/test_stats.py) -- it still fires on
        # every single occurrence, dup or not. Whether the visible nudge
        # actually SHOWS is a separate question, answered below and
        # recorded in the extra nudge= field, never by changing this
        # outcome literal.
        #
        # Language-less wiring (WIRED_EXTS explicitly empty, Ruling 6): a
        # project that has deliberately wired NO language has no complete
        # wired set to name in the message at all, and every known
        # extension would otherwise nag on every single write -- silent,
        # exactly as before this feature, same as any other unwired
        # extension.
        if [ -z "$WIRED_EXTS" ]; then
            finish "language-available-not-wired"
        fi

        LANG_NAME="$(_lang_name_for "$FILE_PATH" "$KNOWN_EXTS")"

        # Dedupe: once per language per SESSION (coordinator's decision),
        # not once per project forever -- reusing mc_state_file_for's
        # existing per-session state file (hooks/memlib.sh), never a new
        # persistence layer. It is already session-scoped, so there is no
        # cleanup story to invent; a PERMANENT "already told you" marker
        # would mean a user who missed the line once never hears it
        # again; one line per language per session is self-limiting while
        # still reminding someone who has not acted. session_id is parsed
        # from $PAYLOAD here, lazily -- nowhere else in this hook needs
        # it, and this whole branch only runs for a brand-new file in a
        # known-but-unwired language, inherently rare.
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

        NUDGE_DECISION="shown"
        if [ -n "$SESSION_ID" ]; then
            # shellcheck source=memlib.sh
            source "$SCRIPT_DIR/memlib.sh"
            STATE_FILE="$(mc_state_file_for "$PROJECT" "$SESSION_ID")"
            export MC_NUDGE_LANG="$LANG_NAME"
            mc_update_state_json "$STATE_FILE" '
import os

lang = os.environ.get("MC_NUDGE_LANG", "")
langs = state.setdefault("nudged_langs", [])
if lang in langs:
    sys.exit(3)
langs.append(lang)
print(json.dumps(state))
' >>"$MC_LOG" 2>&1
            MC_UPDATE_RC=$?
            # 3 = the transform's own "already told this session" signal
            # (never a state write, matches nothing that was printed).
            # Any OTHER non-zero code is a lock failure (97/98, mapped to
            # 1 by mc_update_state_json itself, already logged under its
            # own outcome=lock-timeout/lock-open-failed line) -- fail
            # OPEN toward SHOWING the nudge rather than silently losing
            # the one thing this feature exists to do; an occasional
            # extra line under lock contention is a minor annoyance, a
            # silently-never-told user is the bug being fixed.
            if [ "$MC_UPDATE_RC" -eq 3 ]; then
                NUDGE_DECISION="suppressed"
            fi
        fi

        if [ "$NUDGE_DECISION" = "suppressed" ]; then
            finish "language-available-not-wired" "nudge=suppressed"
        fi

        LANG_MESSAGE="New file type: ${LANG_NAME} is supported by this engine but not wired for this project — wiring it means re-running repo-init.sh with the complete current parameter set plus ${LANG_NAME}, not a one-flag add; see the memcontinuum skill for the procedure."
        if ! _emit_additional_context "$LANG_MESSAGE"; then
            finish "language-available-not-wired" "nudge=build-failed"
        fi
        finish "language-available-not-wired" "nudge=shown"
    fi
    finish "not-indexed-extension"
fi

CODE_ROOT="${MEMCONTINUUM_CODE_ROOT:-}"
if [ -z "$CODE_ROOT" ]; then
    finish "no-code-root-configured"
fi

# A plain lexical `case "$FILE_PATH" in "$CODE_ROOT"/*` prefix match (the
# original check here) is not enough to prove containment: it is fooled
# both by a literal `/../` traversal segment (which is textually "under"
# the root string while resolving to a sibling of it) and by a symlinked
# ancestor directory (every path segment textually under the root, but
# the real directory it names lives elsewhere). MEDIUM (2026-08-31
# review), fixed bash-3.2-safe with no external binaries beyond what this
# hook already uses -- mc_path_under_root (hooks/mc-path-lib.sh) now
# shared with hooks/ledger-post-edit.sh, so this is the one
# implementation, not two. mc-path-lib.sh is a separate, side-effect-free
# file (symlink-paths review round 1, finding 3) specifically so this
# hook never has to source all of memlib.sh (and pay its mkdir/config.sh/
# MC_PY cost) just to reach this one pure function -- sourced lazily here
# regardless (not at the top of this file), so every earlier finish()
# above still short-circuits before paying even mc-path-lib.sh's own
# (much smaller) sourcing cost.
# shellcheck source=mc-path-lib.sh
source "$SCRIPT_DIR/mc-path-lib.sh"
mc_path_under_root "$FILE_PATH" "$CODE_ROOT"
case $? in
    0) ;;
    2) finish "path-traversal" ;;
    3) finish "code-root-unresolvable" ;;
    4) finish "no-existing-ancestor" ;;
    5) finish "ancestor-unresolvable" ;;
    *) finish "outside-code-root" ;;
esac

MESSAGE="New source file under ${CODE_ROOT} — confirm the code index is initialized and not stale, then run code-search; name relevant hits or say none."

export HOOK_MESSAGE="$MESSAGE"
OUTPUT_JSON="$(PYTHONPATH= "$PY" -c '
import json, os

print(json.dumps({
    "hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "additionalContext": os.environ["HOOK_MESSAGE"],
    }
}))
' 2>>"$LOG")"

if [ -z "$OUTPUT_JSON" ]; then
    finish "output-build-failed"
fi

printf '%s\n' "$OUTPUT_JSON"
finish "nudged"
