#!/usr/bin/env bash
# mc-registry-lib.sh -- shared, pure-bash helpers for the machine-level
# decision registry. Sourced by memcontinuum-decide.sh, memcontinuum-state.sh,
# and hooks/memcontinuum-detect.sh -- never executed standalone.
#
# Kills the fragment that used to be triplicated three ways (repo-key
# derivation, decisions.tsv parsing, the wired-hooks scan) and that drifted
# out of sync between the three files (fix-round-4 finding F5/F9): one
# definition, sourced everywhere, so "wired"/"decided" mean the same thing to
# the detector, `state.sh`, and `decide.sh` by construction, not by comment
# discipline.
#
# Constraints, because detect.sh sources this on EVERY session start in EVERY
# git repo on the machine, including ones that have nothing to do with this
# tool:
#   * Pure bash + git. No python, no hooks/memlib.sh -- either would give
#     detect.sh a dependency (and a cost) it deliberately does not have.
#   * Side-effect-free at SOURCE time: this file only defines functions and
#     two constants. It runs nothing, reads nothing, writes nothing until one
#     of its functions is actually called. Safe to `.` under `set -u`.
#   * bash 3.2 compatible: no associative arrays, no `${var,,}`, no
#     `readarray`. `$'\t'` and `<<<` are both fine (bash 2.x+).
#
# Functions return 0/1 like a normal test and hand back results through a few
# plain globals (an `MC_`-prefixed "out parameter" convention) rather than
# stdout capture or `local -n` namerefs -- bash 3.2 has neither nameref nor
# `local -n`, and forking a subshell per lookup is exactly the per-call cost
# detect.sh exists to avoid.

# TAB constant, hoisted once. The three former call sites each re-forked
# `$(printf '\t')` per loop iteration to get an IFS value; that fork is gone
# now that decisions.tsv reading lives in mc_registry_lookup below.
MC_TAB=$'\t'

# The five ALWAYS-wired write-side hooks, matched on "command" lines only.
# The two PreToolUse hooks (pre-edit-chain.sh, newfile-nudge.sh) are
# deliberately excluded: a rationale-only install omits them on purpose, so
# their absence must never make a repo read as unwired.
MC_HOOK_BASENAMES="ledger-post-edit.sh precompact-persist.sh sessionstart-remind.sh userprompt-remind.sh sessionend-stamp.sh"

# mc_repo_key TARGET
#
# The one repo-identity mapping all three consumers must agree on: TARGET's
# git toplevel -> that repo's `origin` remote URL when it has one, else the
# toplevel path itself -> tab/newline-sanitized (a key containing either
# could otherwise fake a TSV column or an extra row). On success, sets
# MC_REPO (the toplevel path, unsanitized -- callers that need the plain repo
# path use this instead of a second `git rev-parse`) and MC_REPO_KEY (the
# sanitized key) and returns 0. When TARGET is not inside a git working tree
# (or git itself is unavailable), clears both and returns 1.
mc_repo_key() {
    local target="$1" remote
    MC_REPO=""
    MC_REPO_KEY=""
    command -v git >/dev/null 2>&1 || return 1
    MC_REPO="$(git -C "$target" rev-parse --show-toplevel 2>/dev/null)"
    [ -n "$MC_REPO" ] || return 1
    remote="$(git -C "$MC_REPO" config --get remote.origin.url 2>/dev/null)"
    if [ -n "$remote" ]; then
        MC_REPO_KEY="$remote"
    else
        MC_REPO_KEY="$MC_REPO"
    fi
    MC_REPO_KEY="$(printf '%s' "$MC_REPO_KEY" | tr '\t\n' '__')"
    return 0
}

# mc_registry_lookup DECISIONS_FILE KEY
#
# Reads DECISIONS_FILE (key<TAB>decision<TAB>iso-date<TAB>note, `#`-comment
# and blank lines skipped) looking for KEY's row. The
# `|| [ -n "$line" ]` clause reads a final line with no trailing newline too
# (F9: dropped silently without it -- and the dropped row is always the
# MOST RECENTLY written one, since decide.sh appends). On a match, sets
# MC_LOOKUP_DECISION / MC_LOOKUP_WHEN / MC_LOOKUP_NOTE and returns 0; on no
# match, including a missing file, clears all three and returns 1.
mc_registry_lookup() {
    local file="$1" key="$2" line k rest
    MC_LOOKUP_DECISION=""
    MC_LOOKUP_WHEN=""
    MC_LOOKUP_NOTE=""
    [ -f "$file" ] || return 1
    while IFS= read -r line || [ -n "$line" ]; do
        case "$line" in
            \#*|"") continue ;;
        esac
        k="${line%%"$MC_TAB"*}"
        [ "$k" = "$key" ] || continue
        rest="${line#*"$MC_TAB"}"
        IFS="$MC_TAB" read -r MC_LOOKUP_DECISION MC_LOOKUP_WHEN MC_LOOKUP_NOTE <<<"$rest"
        return 0
    done < "$file"
    return 1
}

# mc_wiring_scan SETTINGS_FILE...
#
# Classifies wiring against MC_HOOK_BASENAMES with ONE grep pass per settings
# file (not the 5-basenames x 2-files = 10 greps this replaces): every
# "command" line across the given files is collected once, then each
# basename is matched against that in-memory text. Missing files are simply
# skipped -- pass whichever of settings.local.json / settings.json exist.
# Sets MC_WIRING to "full" (all five present), "none" (none present), or
# "partial" (some but not all), and MC_WIRING_MISSING to a space-separated
# list of the basenames NOT found (empty when full).
mc_wiring_scan() {
    local settings base cmds="" missing="" found_any=0
    for settings in "$@"; do
        [ -f "$settings" ] || continue
        cmds="$cmds
$(grep '"command"' "$settings" 2>/dev/null)"
    done
    for base in $MC_HOOK_BASENAMES; do
        case "$cmds" in
            *"$base"*) found_any=1 ;;
            *) missing="$missing$base " ;;
        esac
    done
    MC_WIRING_MISSING="${missing% }"
    if [ -z "$missing" ]; then
        MC_WIRING="full"
    elif [ "$found_any" -eq 0 ]; then
        MC_WIRING="none"
    else
        MC_WIRING="partial"
    fi
}

# mc_first_wired_command SETTINGS_FILE...
#
# Prints (via globals, not stdout) the first raw "command" line across the
# given settings files that contains one of MC_HOOK_BASENAMES, and which file
# it came from. Used to pull MEMCONTINUUM_ROOT/MEMCONTINUUM_PROJECT from a
# single, coherent hook entry -- independently sed-ing the whole settings
# blob for each field separately can pair one project's store with a
# DIFFERENT project's name when a repo's .claude carries more than one
# project's wiring (the two-projects-one-claude-dir topology). Sets
# MC_WIRED_COMMAND and MC_WIRED_SETTINGS_FILE and returns 0 on a match;
# clears both and returns 1 when none of the files has one.
mc_first_wired_command() {
    local settings base line
    MC_WIRED_COMMAND=""
    MC_WIRED_SETTINGS_FILE=""
    for settings in "$@"; do
        [ -f "$settings" ] || continue
        while IFS= read -r line || [ -n "$line" ]; do
            for base in $MC_HOOK_BASENAMES; do
                case "$line" in
                    *"$base"*)
                        MC_WIRED_COMMAND="$line"
                        MC_WIRED_SETTINGS_FILE="$settings"
                        return 0
                        ;;
                esac
            done
        done < <(grep '"command"' "$settings" 2>/dev/null)
    done
    return 1
}

# mc_wired_command_for_project PROJECT SETTINGS_FILE...
#
# Like mc_first_wired_command, but scoped to entries carrying an
# MEMCONTINUUM_PROJECT=PROJECT marker at an env-assignment position (start-
# of-command or preceded by whitespace, followed by whitespace or end-of-
# command; bare or single-quoted -- same anchoring scripts/mc_settings_merge.py
# uses, R6/R7 fix, round 4). Used when a repo's .claude wires MORE THAN ONE
# project (two-projects-one-claude-dir topology): memcontinuum-state.sh
# must report the store/project the REGISTRY ROW actually names, not
# whichever project's hook entry happens to appear first in the settings
# file. Sets MC_WIRED_COMMAND and MC_WIRED_SETTINGS_FILE and returns 0 on a
# match; clears both and returns 1 when none of the files has one.
mc_wired_command_for_project() {
    local project="$1" settings base line padded
    shift
    MC_WIRED_COMMAND=""
    MC_WIRED_SETTINGS_FILE=""
    for settings in "$@"; do
        [ -f "$settings" ] || continue
        while IFS= read -r line || [ -n "$line" ]; do
            padded=" $line "
            for base in $MC_HOOK_BASENAMES; do
                case "$line" in
                    *"$base"*)
                        case "$padded" in
                            *" MEMCONTINUUM_PROJECT=$project "*|*" MEMCONTINUUM_PROJECT='$project' "*)
                                MC_WIRED_COMMAND="$line"
                                MC_WIRED_SETTINGS_FILE="$settings"
                                return 0
                                ;;
                        esac
                        ;;
                esac
            done
        done < <(grep '"command"' "$settings" 2>/dev/null)
    done
    return 1
}

# mc_wired_commands_for_project PROJECT SETTINGS_FILE...
#
# Like mc_wired_command_for_project, but returns EVERY matching command line
# (one per stdout line), not just the first -- the updater workstream needs
# every one of a project's rendered hook lines (to check each carries the
# current stamp, not just whichever sorts first), where the detector/state.sh
# only ever needed one representative line. Kept as a separate function
# rather than changing mc_wired_command_for_project's contract: that
# function's single-match/out-parameter shape is relied on by
# memcontinuum-state.sh today. Prints nothing and returns 1 when no line
# matches; prints N lines and returns 0 otherwise. Command lines never
# contain a literal newline (they are single JSON string values), so one
# line of stdout per match is safe to split on.
mc_wired_commands_for_project() {
    local project="$1" settings base line padded found=1
    shift
    for settings in "$@"; do
        [ -f "$settings" ] || continue
        while IFS= read -r line || [ -n "$line" ]; do
            padded=" $line "
            for base in $MC_HOOK_BASENAMES; do
                case "$line" in
                    *"$base"*)
                        case "$padded" in
                            *" MEMCONTINUUM_PROJECT=$project "*|*" MEMCONTINUUM_PROJECT='$project' "*)
                                printf '%s\n' "$line"
                                found=0
                                ;;
                        esac
                        ;;
                esac
            done
        done < <(grep '"command"' "$settings" 2>/dev/null)
    done
    return "$found"
}

# mc_command_env_value CMD VAR
#
# Pulls one VAR=value token out of a rendered hook COMMAND line. Generalizes
# the VAR-specific extraction memcontinuum-state.sh used to inline twice
# (MEMCONTINUUM_ROOT, MEMCONTINUUM_PROJECT) so the updater does not
# reimplement it a third time. repo-init.sh emits shlex-quoted values
# (VAR='a b/c') when the value could contain a shell-special character;
# hand-wired or charset-restricted values (PROJECT, a plain sha) often
# aren't quoted -- the quoted form is used when the assignment is followed by
# a quote, else the bare word (up to the next space or the closing JSON
# quote). Sets MC_ENV_VALUE (empty string if VAR is absent from CMD) and
# always returns 0 -- an absent var is not an error, just "this line doesn't
# carry it" (pre-stamp lines lack MEMCONTINUUM_RENDERED, for instance).
#
# Pure parameter expansion, no sed fork, and -- the reason it was rewritten --
# an EXPLICITLY EMPTY value is read as empty. The old two-sed version tried
# the quoted form first and treated its empty result as "not found", falling
# through to the bare-word pattern, which then captured the two literal quote
# characters: `MEMCONTINUUM_NEVER_EXTS=''` came back as `''`, and that
# two-character string went on to be recorded in a registry row and rendered
# back onto a hook line as the nonsense glob `*.''`.
#
# The match is anchored on an assignment boundary (start of string, or a
# preceding character that cannot be part of an identifier -- a space, or the
# JSON string's opening quote for the first token on the line) so a VAR that
# is a suffix of a longer variable name can never be read out of it.
# MC_ENV_PRESENT is set alongside MC_ENV_VALUE: 1 when the line actually
# carries the assignment, 0 when it does not. The two are NOT the same
# question, and the difference decides a migration. An explicitly empty
# `MEMCONTINUUM_LANG_EXTS=''` is a RECORDED ANSWER -- language-less wiring,
# chosen on purpose. The variable being absent means the wiring predates the
# language set ever being written down: unknown, not "none". Migrating the
# second case as though it were the first would silently turn a project's
# code indexing off.
mc_command_env_value() {
    local cmd="$1" var="$2" head tail
    MC_ENV_VALUE=""
    MC_ENV_PRESENT=0
    tail="$cmd"
    while :; do
        case "$tail" in
            *"$var="*) ;;
            *) return 0 ;;
        esac
        head="${tail%%"$var="*}"
        tail="${tail#*"$var="}"
        case "$head" in
            ""|*[!A-Za-z0-9_]) ;;
            *) continue ;;
        esac
        case "$tail" in
            "'"*)
                tail="${tail#\'}"
                MC_ENV_VALUE="${tail%%\'*}"
                ;;
            *)
                MC_ENV_VALUE="${tail%%[ \"]*}"
                ;;
        esac
        MC_ENV_PRESENT=1
        return 0
    done
}

# mc_is_git_repo DIR -- true iff DIR is a git working tree, root OR linked
# worktree. A linked worktree (`git worktree add`) has a .git FILE (a
# "gitdir: <path>" pointer), not a directory, so `[ -d "$DIR/.git" ]` misreads
# one as "not a git repo at all". `[ -e ]` accepts either shape;
# `rev-parse --is-inside-work-tree` confirms it is actually a working tree
# (not some unrelated directory that merely happens to contain a file or dir
# named .git) before this counts as a real answer.
mc_is_git_repo() {
    [ -e "$1/.git" ] || return 1
    git -C "$1" rev-parse --is-inside-work-tree >/dev/null 2>&1
}

# mc_is_marked_store DIR -- true iff DIR is an existing decision store: a git
# working tree carrying this tool's markers (a topics/incidents/concepts
# directory, or a README naming the tool). The ONE definition of "this path
# still holds a store", shared by the installer (which refuses to seed store
# directories and a replacement post-commit hook into an unrelated repo) and
# by the re-render walk (whose row may name a store that has since been
# renamed or deleted -- re-running the installer against a missing --store
# would seed a fresh one there, which is the line neither command crosses).
mc_is_marked_store() {
    local dir="$1" d
    mc_is_git_repo "$dir" || return 1
    for d in topics incidents concepts; do
        [ -d "$dir/$d" ] && return 0
    done
    if [ -f "$dir/README.md" ]; then
        case "$(cat "$dir/README.md" 2>/dev/null)" in
            *MemContinuum*) return 0 ;;
        esac
    fi
    return 1
}

# mc_note_encode VALUE / mc_note_decode VALUE
#
# The registry row's NOTE column is space-separated `key=value` fields with
# `;` between the elements of a list field. A value containing a space would
# therefore end its own field early and turn the rest of itself into garbage
# fields -- and real stores do live under paths with spaces in them. So every
# value is percent-encoded on the way in and decoded on the way out: `%`
# becomes `%25` first, then ` ` becomes `%20`.
#
# Decoding undoes that in the opposite order (`%20` before `%25`), which is
# what makes a value that literally contains the text "%20" survive: it is
# stored as `%2520`, where no `%20` occurs, and only the `%25` step turns it
# back into a percent sign.
#
# `;`, tab and newline are NOT encoded -- they are refused at the point of
# writing instead (memcontinuum-decide.sh). A `;` that came back decoded
# would arrive AFTER the field had already been split on `;`, so encoding it
# would only move the corruption somewhere harder to see.
#
# Accepted edge: a row written before this encoding existed, holding a path
# with a literal `%` in it, decodes wrongly if that `%` happens to be
# followed by `20` or `25`. Such a path could never have been stored
# correctly anyway (it would have had no space to break on, but nothing
# guaranteed round-tripping either); the fix is to rewrite the row.
mc_note_encode() {
    local v="$1"
    v="${v//%/%25}"
    v="${v// /%20}"
    printf '%s' "$v"
}

mc_note_decode() {
    local v="$1"
    v="${v//%20/ }"
    v="${v//%25/%}"
    printf '%s' "$v"
}

# mc_note_field NOTE KEY
#
# Pulls one "key=value" field out of a registry row's NOTE column (the shape
# decide.sh writes: "store=... project=... claude-dirs=a;b code-roots=c;d
# langs=python;swift never=.cs;.h" -- semicolon-joined for the list-valued
# fields, space-separated between fields). The value is percent-DECODED
# before it is handed back, so a caller always sees the real path. Sets
# MC_NOTE_FIELD to the value, or "" when KEY is absent -- absence is normal
# (every row written before those four fields existed lacks them), not an
# error -- and always returns 0.
mc_note_field() {
    local note="$1" key="$2" tok
    local -a toks=()
    MC_NOTE_FIELD=""
    # `read -ra`, not `for tok in $note`: an unquoted expansion also runs
    # pathname expansion, so a field value that happens to look like a glob
    # would be replaced by whatever files sit in the caller's directory.
    read -ra toks <<<"$note"
    for tok in "${toks[@]:-}"; do
        case "$tok" in
            "$key="*) MC_NOTE_FIELD="$(mc_note_decode "${tok#"$key"=}")"; return 0 ;;
        esac
    done
    return 0
}

# mc_split_semi LIST
#
# Splits a ';'-joined registry list field into the MC_SPLIT array, dropping
# empty elements. The ONE way this codebase turns a stored list back into
# arguments: the hand-rolled alternatives it replaces were
# `for x in $(printf '%s' "$list" | tr ';' ' ')`, which both word-splits on
# spaces (so a store or claude-dir under a path containing one arrives as two
# arguments) and glob-expands against the current directory. `read -ra` does
# neither. Bash-3.2 safe.
mc_split_semi() {
    MC_SPLIT=()
    [ -n "${1:-}" ] || return 0
    local part
    local -a raw=()
    IFS=';' read -ra raw <<<"$1"
    for part in "${raw[@]:-}"; do
        [ -n "$part" ] && MC_SPLIT[${#MC_SPLIT[@]}]="$part"
    done
    return 0
}

# mc_build_wiring_args CODE_ROOTS_SEMI LANGS_COMMA NEVER_COMMA
#
# Builds the tail every "re-run the wiring with this row's parameters" command
# line shares -- `--code-root DIR` per recorded code root, then `--langs` and
# `--never-ext` when non-empty -- into the MC_BUILT_ARGS array. Both
# memcontinuum-decide.sh and scripts/repo-init.sh take exactly these three
# options with exactly these spellings, which is why one builder can serve
# both.
#
# It replaces four near-identical hand-rolled blocks (the installer's
# record-the-decision arguments, and the re-render command's targeted-mode,
# migration, and per-claude-dir blocks) that had already drifted: some split
# the semicolon lists with an IFS assignment, others with `tr ';' ' '` and an
# unquoted expansion that word-split every path containing a space and
# glob-expanded the rest.
#
# Claude-dirs are deliberately NOT built here: `decide.sh wired` takes the
# whole set at once while an installer run takes exactly one, so the caller
# decides. Results come back through an array global because bash 3.2 has no
# namerefs, and because an array is the only way to pass a path with a space
# through without re-quoting it.
mc_build_wiring_args() {
    local code_roots_semi="$1" langs_comma="$2" never_comma="$3" root
    MC_BUILT_ARGS=()
    mc_split_semi "$code_roots_semi"
    for root in "${MC_SPLIT[@]:-}"; do
        [ -n "$root" ] || continue
        MC_BUILT_ARGS[${#MC_BUILT_ARGS[@]}]="--code-root"
        MC_BUILT_ARGS[${#MC_BUILT_ARGS[@]}]="$root"
    done
    if [ -n "$langs_comma" ]; then
        MC_BUILT_ARGS[${#MC_BUILT_ARGS[@]}]="--langs"
        MC_BUILT_ARGS[${#MC_BUILT_ARGS[@]}]="$langs_comma"
    fi
    if [ -n "$never_comma" ]; then
        MC_BUILT_ARGS[${#MC_BUILT_ARGS[@]}]="--never-ext"
        MC_BUILT_ARGS[${#MC_BUILT_ARGS[@]}]="$never_comma"
    fi
    return 0
}

# mc_resolve_home
#
# F7: two registries used to exist under a non-default MEMCONTINUUM_HOME --
# the detector's hook line baked setup-time HOME literally while decide.sh
# and state.sh defaulted to $HOME/.memcontinuum, so a decline never silenced
# the ask. Resolution order, identical for all three consumers:
#   1. MEMCONTINUUM_HOME already set in the environment -- used as-is.
#   2. The FIXED default path's config.sh ($HOME/.memcontinuum/config.sh),
#      IF it records a different MEMCONTINUUM_HOME -- a "pointer" written by
#      memcontinuum-setup.sh when it was run with a non-default HOME.
#   3. The fixed default itself, $HOME/.memcontinuum.
# Sourced in a subshell so a pointer config.sh's OTHER variables (engine,
# python) can't leak into our environment before HOME is decided. Always
# succeeds; leaves MEMCONTINUUM_HOME set on return.
mc_resolve_home() {
    local default_home="$HOME/.memcontinuum" pointer
    if [ -n "${MEMCONTINUUM_HOME:-}" ]; then
        return 0
    fi
    pointer="$default_home/config.sh"
    if [ -f "$pointer" ]; then
        MEMCONTINUUM_HOME="$(
            # shellcheck source=/dev/null
            . "$pointer" 2>/dev/null
            printf '%s' "${MEMCONTINUUM_HOME:-}"
        )"
    fi
    [ -n "${MEMCONTINUUM_HOME:-}" ] || MEMCONTINUUM_HOME="$default_home"
    return 0
}
