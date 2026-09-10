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
# moment it's most likely to be skipped. (A different, KNOWN-but-not-WIRED
# extension gets a different, multi-line decision point instead -- see
# MEMCONTINUUM_KNOWN_EXTS in the Env section below, not this one line.)
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
#   - a VISIBLE nudge (this hook's stdout/additionalContext) fires ONLY
#     when: the path does not exist AND is not a symlink (a broken
#     symlink is `! -e` but IS `-L` -- must stay silent, not be treated
#     as new), is under MEMCONTINUUM_CODE_ROOT, and ends in a source
#     extension this engine knows about -- WIRED for the retrieval
#     reminder, KNOWN-but-not-WIRED for the newlang-nudge decision point
#     (N2 fix round: both nudges share this same containment gate now;
#     before this round the newlang nudge's visible text ignored
#     containment even though its header always claimed the hook fires
#     only under the code root). The log-only DETECTION outcome
#     (outcome=language-available-not-wired) is NOT gated on containment
#     -- unchanged, pre-existing behavior memidx.py stats already counts
#     regardless of code root; only the VISIBLE nudge text is new here.
#   - any other case, or any failure -> exit 0, no stdout (never blocks
#     the write)
#   - always runs under hooks/mc-watchdog.sh's shared wall-clock watchdog
#     (the same guard block every write-side hook uses) even though this
#     hook's own logic never calls python for real work -- one shared
#     mechanism, not a second bespoke timeout story for the one hook that
#     happens to be fast
#   - logs exactly one outcome line per invocation to
#     $MEMCONTINUUM_HOME/hook.log. The known-but-unwired-language branch
#     also reads/writes its own per-session dedupe state file under
#     $MEMCONTINUUM_HOME/sessions/<project>/<session>.json (see
#     MEMCONTINUUM_KNOWN_EXTS below) -- no other write, anywhere, ever;
#     in particular never under the code root or the store.
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
#                            newlang-nudge (N1 fix round, TOP-0128): this
#                            case ALSO surfaces a structured, numbered
#                            DECISION POINT -- not a one-line notice with
#                            no way to answer -- naming the language and
#                            offering exactly the shape
#                            skills/memcontinuum/SKILL.md section 2 uses
#                            for every other human decision this tool
#                            asks: (1) wire it now, (2) never mention it
#                            here again -- recorded permanently through
#                            repo-init.sh's existing --never-ext mechanism
#                            (this hook already honours
#                            MEMCONTINUUM_NEVER_EXTS above; nothing new is
#                            built here), (3) not now -- nothing recorded,
#                            asked again next session. Neither option is
#                            restated as a literal command here (INC-0117:
#                            a restated computed procedure is a copy that
#                            can drift) -- both point at "the memcontinuum
#                            skill", whose section 6 is where an agent
#                            acting on the human's answer finds what to
#                            actually run. Gated silent (no message, no
#                            state touched) when WIRED_EXTS is empty
#                            (language-less wiring, Ruling 6 above: there
#                            is no complete wired set to name yet, so
#                            nagging about every known extension would be
#                            noise, not help) or when the write is not
#                            under MEMCONTINUUM_CODE_ROOT (N2 fix round --
#                            same containment gate the wired-file reminder
#                            already used; the log-only DETECTION outcome
#                            still fires either way, see the file header).
#                            Deduped once per language per SESSION, never
#                            permanently, via the same per-session state
#                            file every other write-side hook already
#                            shares (mc_state_file_for/mc_update_state_json,
#                            hooks/memlib.sh) -- reused rather than a new
#                            persistence layer because it is already
#                            session-scoped (no cleanup story needed) and a
#                            permanent "already told you" marker would mean
#                            a user who missed the line once never hears it
#                            again. The per-session mark only happens AFTER
#                            the message's JSON envelope has already been
#                            built successfully (N3 fix round: mark-then-
#                            build could lose a nudge to a build failure or
#                            a watchdog kill mid-build, leaving the
#                            language marked as told while nothing was
#                            ever shown -- build-then-mark cannot lose one
#                            that way, since a failed build never marks).
#                            memlib.sh is sourced LAZILY, only once this
#                            branch is already committed to (a brand new
#                            file in a known-but-unwired language,
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

# _build_additional_context MESSAGE -- builds the hookSpecificOutput/
# additionalContext JSON envelope both of this hook's nudges need (the
# wired-file reminder further down, and newlang-nudge's known-but-
# unwired-language nudge) -- one JSON-building implementation, not two
# (N3/NIT3 fix round: this comment used to claim that already while the
# wired path still ran its own separate inline copy -- both now actually
# call this).
#
# Named "build", not "emit": it prints the JSON to ITS OWN stdout and
# returns 0 on success, prints nothing and returns 1 on failure -- but it
# never writes to the hook's real stdout itself. The caller captures this
# function's output via command substitution and decides separately
# whether/when to printf it for real. newlang-nudge's build-then-mark-
# then-print ordering (N3 fix round) depends on this: the JSON has to be
# built and validated BEFORE the per-session dedupe mark, so a build
# failure (or a watchdog kill mid-build) never marks a language as told
# when nothing was actually shown.
_build_additional_context() {
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

# _case_name_for_pattern -- newlang-nudge: maps ONE known extension glob
# (e.g. "*.ts") to its engine language name (e.g. "typescript").
# Deliberately a bash `case` table, not a memidx.py/chunkers call: this
# hook never calls memidx.py for real work (see the file header), and the
# branches that need this are already the rare ones -- but a python
# subprocess just to look up a display name is still one more thing to
# fail, and this hook's own design principle is to stay simple enough to
# reason about without one. Mirrors chunkers.LANGUAGE_TABLE
# (chunkers/__init__.py) row-for-row, since the extension is not always a
# substring of the language name (.ts is "typescript", .rs is "rust",
# four JS extensions are all "javascript") -- tests/test_write_hooks.py::
# test_language_name_matches_the_engine_registry_for_every_known_language
# runs the real hook against every LANGUAGE_TABLE row and fails the moment
# a new row (or a new extension on an existing row) has no arm here, so
# this table cannot drift unnoticed the way INC-0117's copy did. An
# extension with no arm (this engine version's KNOWN_EXTS should never
# produce one, but render-time env is not proof) falls back to the raw
# extension text -- correct but unhelpfully terse is better than wrong.
#
# The ONE table both _lang_name_for (path -> name) and _lang_exts_for
# (name -> every known extension mapping to it, N1 fix round) build on,
# so the extension<->name mapping is never duplicated between "what do I
# call this file" and "what do I need to list to decline this language".
_case_name_for_pattern() {  # $1=one glob (e.g. "*.ts")
    case "$1" in
        "*.swift") printf 'swift' ;;
        "*.py") printf 'python' ;;
        "*.js"|"*.jsx"|"*.mjs"|"*.cjs") printf 'javascript' ;;
        "*.ts") printf 'typescript' ;;
        "*.tsx") printf 'tsx' ;;
        "*.java") printf 'java' ;;
        "*.php") printf 'php' ;;
        "*.rs") printf 'rust' ;;
        "*.lua") printf 'lua' ;;
        *) printf '%s' "${1#\*.}" ;;
    esac
}

_lang_name_for() {  # $1=path  $2=space-separated glob list (KNOWN_EXTS)
    set -f
    for _pat in $2; do
        case "$1" in
            $_pat)
                set +f
                _case_name_for_pattern "$_pat"
                return 0
                ;;
        esac
    done
    set +f
    printf '%s' "${1##*.}"
    return 1
}

# _lang_exts_for LANG KNOWN_EXTS -- newlang-nudge decline option (N1 fix
# round): every extension among KNOWN_EXTS (bare ".ext" form, comma-
# joined) whose engine name is LANG. --never-ext (repo-init.sh /
# memcontinuum-update.sh) takes EXTENSIONS, not language names, and a
# multi-extension language (javascript: .js/.jsx/.mjs/.cjs) needs every
# one of them named to actually go silent for that language -- the
# decline option has to say so, not just name the one extension that
# happened to trigger this particular nudge.
_lang_exts_for() {  # $1=lang name  $2=space-separated glob list
    local _out=""
    set -f
    for _pat in $2; do
        if [ "$(_case_name_for_pattern "$_pat")" = "$1" ]; then
            _out="${_out:+$_out,}${_pat#\*}"
        fi
    done
    set +f
    printf '%s' "$_out"
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
        # newlang-nudge: the DETECTION outcome below is unchanged from
        # before this feature existed (literal string pinned by
        # memidx.py's stats and tests/test_stats.py) -- it still fires on
        # every single occurrence, dup or not, regardless of code-root
        # containment. Whether the VISIBLE decision point actually shows
        # is a separate question, gated below (language-less wiring,
        # then containment, then per-session dedupe) and recorded in the
        # extra nudge= field, never by changing this outcome literal.
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

        # N2 fix round (Grok 1, LOW): a write outside the code root (or a
        # code_root/../outside traversal) used to still show the visible
        # nudge even though a wired extension on the same path stays
        # silent -- the two disagreed, and the file header's own
        # "fires only under the code root" contract was wrong for this
        # branch. Checked here, before any dedupe state is touched and
        # before the message is even built, so an out-of-root write costs
        # nothing beyond this one check and never consumes the session's
        # one "shown" slot for a language that may get a real, in-root
        # occurrence later. Collapses every mc_path_under_root failure
        # mode (traversal, unresolvable root, no existing ancestor,
        # symlink escape) into one nudge= value -- this branch only needs
        # yes/no, not why, the same precedent ledger-post-edit.sh already
        # set for its own out-of-scope outcome.
        CODE_ROOT="${MEMCONTINUUM_CODE_ROOT:-}"
        if [ -z "$CODE_ROOT" ]; then
            finish "language-available-not-wired" "nudge=outside-code-root"
        fi
        # shellcheck source=mc-path-lib.sh
        source "$SCRIPT_DIR/mc-path-lib.sh"
        if ! mc_path_under_root "$FILE_PATH" "$CODE_ROOT"; then
            finish "language-available-not-wired" "nudge=outside-code-root"
        fi

        LANG_NAME="$(_lang_name_for "$FILE_PATH" "$KNOWN_EXTS")"
        LANG_EXTS="$(_lang_exts_for "$LANG_NAME" "$KNOWN_EXTS")"

        # N1 (owner ruling, TOP-0128): the notice becomes a DECISION
        # POINT -- a structured numbered-choice prompt, the same shape
        # skills/memcontinuum/SKILL.md section 2 uses for every other
        # human decision this tool asks, not a one-line notice with no
        # way to answer. Neither option restates the wiring procedure's
        # actual flags (INC-0117: a restated computed procedure is a copy
        # that can drift) -- both point at "the memcontinuum skill" by
        # name; section 6 there is where an agent acting on the answer
        # finds what to actually run, including which of option 2's two
        # possible costs applies to this repo.
        LANG_MESSAGE="New file type: ${LANG_NAME} is supported by this engine but not wired for this project."
        LANG_MESSAGE="${LANG_MESSAGE}
1. Wire it now — re-run repo-init.sh with the complete current parameter set plus ${LANG_NAME}, not a one-flag add; see the memcontinuum skill for the procedure
2. Never mention ${LANG_EXTS} here (every ${LANG_NAME} extension) — recorded permanently via --never-ext: one command when this project already records its wiring, otherwise the same full re-run as option 1 with these added to the never-list; see the memcontinuum skill for which applies
3. Not now — nothing recorded; asked again next session"

        # N3 fix round (Grok 2, LOW): build the JSON envelope BEFORE the
        # per-session mark, not after. Building it is the one step that
        # can fail (a broken PY) or get cut short (a watchdog kill
        # mid-build) -- doing it first means either of those leaves
        # nothing marked, so the next occurrence in this session retries
        # instead of being silently suppressed by a mark nothing was ever
        # shown for. The mark step right after this is still the SAME
        # atomic check-and-append mc_update_state_json call as before --
        # the concurrent-first-hits guarantee (one shown, the rest
        # suppressed under lock) is unchanged, since every concurrent
        # loser still discards its own already-built JSON the instant its
        # own mark attempt comes back "already told" (rc=3, below).
        BUILT_JSON="$(_build_additional_context "$LANG_MESSAGE")"
        if [ -z "$BUILT_JSON" ]; then
            finish "language-available-not-wired" "nudge=build-failed"
        fi

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
langs = state.get("nudged_langs")
# A JSON list is the only shape production ever writes here (this same
# transform, a few lines below). Anything else -- corrupted or
# hand-edited state, e.g. a bare string -- must NOT feed `in`: on a str,
# `in` is a SUBSTRING test, so {"nudged_langs": "typescript"} would
# silently suppress the "typescript" nudge (and "typescriptfoo" would
# suppress it too). Treat a malformed field the same way the caller
# above already treats a wholly garbage state file (state reset, not a
# second fail-open path): reset just this field to an empty list and
# keep going, so the nudge still fires and the next write leaves
# nudged_langs well-formed.
if not isinstance(langs, list) or not all(isinstance(x, str) for x in langs):
    langs = []
if lang in langs:
    sys.exit(3)
langs.append(lang)
state["nudged_langs"] = langs
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

        printf '%s\n' "$BUILT_JSON"
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

# NIT 3 fix round (Grok, duplicate JSON envelope): this used to keep its
# own inline copy of the JSON-building python instead of calling
# _build_additional_context, even though that helper's own comment
# already claimed "one JSON-building implementation, not two." Now true.
OUTPUT_JSON="$(_build_additional_context "$MESSAGE")"
if [ -z "$OUTPUT_JSON" ]; then
    finish "output-build-failed"
fi

printf '%s\n' "$OUTPUT_JSON"
finish "nudged"
