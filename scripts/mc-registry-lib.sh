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
