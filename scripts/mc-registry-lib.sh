#!/usr/bin/env bash
# mc-registry-lib.sh -- shared, pure-bash helpers for the machine-level
# decision registry. Sourced by memcontinuum-decide.sh, memcontinuum-state.sh,
# hooks/memcontinuum-detect.sh, memcontinuum-update.sh, and repo-init.sh --
# never executed standalone.
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

# mc_physical PATH -- resolves PATH to its physical (symlink-free) form via
# `cd -P && pwd -P`, matching repo-init.sh's abspath()/no-git-default fix
# (Ruling 89: os.path.realpath / `pwd -P`, not os.path.abspath / raw $PWD).
# Moved here from memcontinuum-update.sh (symlink-review round 3, concern 2:
# memcontinuum-decide.sh recorded --store raw, never resolving it, while
# every OTHER recording site -- repo-init.sh's own --store/--code-root,
# memcontinuum-update.sh's migration overrides -- already does; every path
# a --record-decision-shaped write puts into the registry must go through
# the SAME resolution, so this now lives where both `memcontinuum-decide.sh`
# and `memcontinuum-update.sh` already source it, with no new sourcing wired
# up for either -- see those two files' own headers). Falls back to the raw
# PATH when it does not resolve (does not exist yet, etc.) -- a value that
# was never going to be usable anyway, unchanged behavior.
#
# `CDPATH=` (symlink-review round 1, finding 2): with CDPATH set in the
# operator's environment and a RELATIVE PATH that CDPATH resolves, bash's
# `cd` builtin itself prints the matched directory to stdout (POSIX-
# documented CDPATH behavior) BEFORE `pwd -P` runs, so the command
# substitution would capture two newline-joined lines instead of one,
# corrupting the value this function returns. Clearing CDPATH for just
# this `cd` (not globally -- a local assignment on the command itself)
# closes it while changing nothing about the resolution itself. Reproduced
# and verified fixed directly:
#   CDPATH=/tmp/cdpathbase; cd /tmp && (cd sub && pwd -P)       # two lines
#   CDPATH=/tmp/cdpathbase; cd /tmp && (CDPATH= cd sub && pwd -P) # one line
#
# Never call with an empty PATH: `cd ""` is a silent no-op in bash (stays in
# the current directory), so this would return the CALLER's own cwd instead
# of failing -- every caller here guards with `[ -n "$val" ]` first.
mc_physical() {
    local p
    p="$(CDPATH= cd "$1" 2>/dev/null && pwd -P)"
    if [ -n "$p" ]; then
        printf '%s' "$p"
    else
        printf '%s' "$1"
    fi
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

# mc_registry_rewrite_row DECISIONS_FILE KEY [NEW_LINE]
#
# The ONE atomic decisions.tsv rewrite (symlink-review round 2, NEW-1): every
# row is copied through unchanged EXCEPT KEY's own (matched the same way
# mc_registry_lookup matches it -- exact equality on the text before the
# first tab, comments and blank lines pass through untouched since their
# "key" text never equals a real KEY), then NEW_LINE is appended verbatim if
# given -- a full "key<TAB>decision<TAB>date<TAB>note" row, so a
# reversal/correction REPLACES the old row rather than shadowing it.
# Omitting NEW_LINE (memcontinuum-decide.sh's `forget`) drops the row
# entirely -- nothing is appended in its place. DECISIONS_FILE not existing
# yet writes the standard two-line header first (unconditionally, even for
# a NEW_LINE-less `forget` of a repo with no registry at all -- matches this
# function's one prior caller's own pre-existing behavior exactly, not
# changed here). Atomic: a `.tmp.$$` file in the same directory (so the
# rename stays on one filesystem), and REMOVED on any write failure --
# never left behind for the caller to clean up or trip over later.
mc_registry_rewrite_row() {
    local file="$1" key="$2" new_line="${3-}" line k tmp
    tmp="$file.tmp.$$"
    {
        if [ -f "$file" ]; then
            while IFS= read -r line || [ -n "$line" ]; do
                k="${line%%"$MC_TAB"*}"
                [ "$k" = "$key" ] && continue
                printf '%s\n' "$line"
            done < "$file"
        else
            printf '# MemContinuum per-repo decisions -- written only by memcontinuum-decide.sh\n'
            printf '# key\tdecision\tdate\tnote\n'
        fi
        # `if`, not `[ -n "$new_line" ] && printf ...`: this is the LAST
        # statement in the group, and `{ ... } > "$tmp"`'s own exit status
        # is whatever this last statement's was -- `&&` on a FALSE test
        # (the common `forget`/NEW_LINE-omitted case) returns 1 from the
        # test itself, which would then trip the `|| { rm -f ...; return
        # 1; }` below even though the write actually succeeded. `if`
        # returns 0 when its condition is false and there is no `else`,
        # so an omitted NEW_LINE reports success correctly. (Caught by
        # this round's own new failure-branch test going unexpectedly RED
        # on the `forget` path -- not merely reasoned about.)
        if [ -n "$new_line" ]; then
            printf '%s\n' "$new_line"
        fi
    } > "$tmp" && mv "$tmp" "$file" || { rm -f "$tmp"; return 1; }
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

# mc_first_command_matching NEEDLE SETTINGS_FILE...
#
# The first "command" line across the given settings files that contains
# NEEDLE. Deliberately not scoped to MC_HOOK_BASENAMES or to a project: the
# machine layer has exactly ONE entry, identified by the detector script's
# basename appearing in its command (the same bare-needle identity
# memcontinuum-setup.sh's own merge uses), and it belongs to no project.
# Sets MC_WIRED_COMMAND / MC_WIRED_SETTINGS_FILE and returns 0 on a match;
# clears both and returns 1 otherwise.
mc_first_command_matching() {
    local needle="$1" settings line
    shift
    MC_WIRED_COMMAND=""
    MC_WIRED_SETTINGS_FILE=""
    for settings in "$@"; do
        [ -f "$settings" ] || continue
        while IFS= read -r line || [ -n "$line" ]; do
            case "$line" in
                *"$needle"*)
                    MC_WIRED_COMMAND="$line"
                    MC_WIRED_SETTINGS_FILE="$settings"
                    return 0
                    ;;
            esac
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

# mc_is_windows_mounted_checkout CHECKOUT
#
# True iff CHECKOUT (a physical path -- the caller resolves symlinks first,
# same as everywhere else in this file) sits on a Windows-mounted drive
# inside WSL: `/proc/version` names Microsoft's kernel build (case-
# insensitive -- WSL1 and WSL2 spell it differently) AND CHECKOUT resolves
# under `/mnt/<single letter>/` -- the WSL convention for a mounted Windows
# drive (drvfs, or 9P on WSL1). Neither check alone is enough: a plain path
# named `/mnt/x/...` on a non-WSL Linux box (an unrelated real mount) must
# not trigger this, and a WSL box with the checkout on its native ext4 disk
# (not under `/mnt`) must not either -- only the AND is the "a store walk
# here costs seconds, not milliseconds" case this exists to catch (measured;
# TOP-0109 L5).
#
# Two test seams, because a machine running this suite may or may not
# itself be WSL, and even a real WSL box has no actual `/mnt/<letter>`
# checkout inside a throwaway sandbox HOME -- a test cannot otherwise
# exercise every quadrant of the AND deterministically:
#   MEMCONTINUUM_PROC_VERSION_FILE  overrides the file read in place of the
#     real /proc/version (default), so the kernel-name check can be driven
#     with a fixture file instead of the real machine's kernel string.
#   MEMCONTINUUM_TEST_WSL_MOUNT=1   forces this whole predicate true,
#     unconditionally, bypassing both real checks -- the end-to-end seam
#     an installer-level test uses when it needs "a Windows-mounted
#     checkout" but the sandbox checkout itself cannot physically be one.
#     Any other value (or unset) never forces the other direction: there
#     is no "force false" knob, because the ordinary unset case already
#     exercises that path on every machine that is not itself a
#     Windows-mounted WSL checkout.
mc_is_windows_mounted_checkout() {
    local checkout="$1" proc_version_file proc_version=""
    [ "${MEMCONTINUUM_TEST_WSL_MOUNT:-}" = "1" ] && return 0
    proc_version_file="${MEMCONTINUUM_PROC_VERSION_FILE:-/proc/version}"
    # `|| :`, exit status ignored on purpose (mc_rules_identity_marker,
    # above, does the same): a file with no trailing newline on its last
    # line -- exactly a raw `/proc/version` read, and every fixture this
    # function's own tests write -- makes `read` return NON-zero even
    # though it assigned the variable correctly. `[ -n ]` on the RESULT,
    # not the read's own exit code, is what tells "no such file" (stays
    # empty) apart from "read the one line, no trailing newline" (still
    # gets the content).
    #
    # `2>/dev/null` BEFORE the `<` input redirect, not after (whole-branch
    # review NIT-1): redirections apply left to right, so with the
    # stderr-silencer last, a MISSING proc_version_file still prints "No
    # such file or directory" to the real stderr before `read` ever runs --
    # reproduced with MEMCONTINUUM_PROC_VERSION_FILE=/nonexistent/procver on
    # both bash 5 and bash 3.2. Silencing stderr first suppresses that
    # message the same way it already suppresses `read`'s own complaint,
    # while still reading a file with no trailing newline correctly.
    IFS= read -r proc_version 2>/dev/null < "$proc_version_file" || :
    [ -n "$proc_version" ] || return 1
    case "$proc_version" in
        *[Mm][Ii][Cc][Rr][Oo][Ss][Oo][Ff][Tt]*) ;;
        *) return 1 ;;
    esac
    case "$checkout" in
        /mnt/[a-zA-Z]/*|/mnt/[a-zA-Z]) return 0 ;;
        *) return 1 ;;
    esac
}

# mc_store_project_identity STORE
#
# Reads STORE/README.md's FIRST line and, only when it matches exactly the
# shape templates/store-README.md.tmpl renders ("# {{PROJECT}} Rationale
# store"), sets MC_STORE_PROJECT to the captured {{PROJECT}} text and
# returns 0. Returns 1 (MC_STORE_PROJECT cleared) when the file is missing,
# empty, or its first line does not match -- a hand-authored or foreign
# README (mc_is_marked_store's own "*MemContinuum*" match is looser, on
# purpose, than this) tells us nothing about WHICH project, so callers must
# treat that as "identity unknown", never as a mismatch.
mc_store_project_identity() {
    local store="$1" first_line
    MC_STORE_PROJECT=""
    [ -f "$store/README.md" ] || return 1
    IFS= read -r first_line 2>/dev/null < "$store/README.md" || :
    case "$first_line" in
        "# "*" Rationale store")
            first_line="${first_line#\# }"
            MC_STORE_PROJECT="${first_line% Rationale store}"
            [ -n "$MC_STORE_PROJECT" ] && return 0
            ;;
    esac
    MC_STORE_PROJECT=""
    return 1
}

# mc_store_checkout_identity STORE
#
# G5 residual (fix wave 1 G9, whole-branch-review Codex 5 follow-up): reads
# STORE/README.md for the "memcontinuum-checkout: PATH" marker
# scripts/repo-init.sh stamps at store-CREATION time (never rewritten on a
# re-run -- the README render is write-if-absent), and, when found and not
# the "unknown" placeholder (an explicit --store given from a cwd with no
# git checkout of its own), sets MC_STORE_CHECKOUT to PATH and returns 0.
# This is the one signal mc_store_project_identity's own PROJECT-name
# comparison could never carry: an unambiguous physical checkout path, so
# two DIFFERENT checkouts sharing a basename AND a --project value (neither
# ever run through --record-decision, so the registry has nothing to say
# either) still read as distinct, rather than the second one being read as
# "ours" purely because the project name happens to match.
#
# Returns 1 (MC_STORE_CHECKOUT cleared) when the file is missing, carries no
# such marker at all (a hand-authored README, or a store this fix predates),
# or the marker is the "unknown" placeholder -- every one of those is
# "identity unknown", not a mismatch, so callers must fall back to
# mc_store_project_identity rather than treat an absent marker as foreign.
mc_store_checkout_identity() {
    local store="$1" line
    MC_STORE_CHECKOUT=""
    [ -f "$store/README.md" ] || return 1
    while IFS= read -r line || [ -n "$line" ]; do
        case "$line" in
            "<!-- memcontinuum-checkout: "*" -->")
                line="${line#<!-- memcontinuum-checkout: }"
                line="${line% -->}"
                [ -n "$line" ] && [ "$line" != "unknown" ] || return 1
                MC_STORE_CHECKOUT="$line"
                return 0
                ;;
        esac
    done < "$store/README.md"
    return 1
}

# mc_registry_owner_of_store DECISIONS_FILE STORE_PHYSICAL SELF_KEY
#
# Reverse lookup decisions.tsv (forward lookup, by KEY, already exists as
# mc_registry_lookup above -- this instead asks "who owns this STORE path")
# for a "wired" row whose note's store= field (mc_note_field) resolves
# (mc_physical) to STORE_PHYSICAL, which the caller has already resolved the
# same way. Three outcomes, because the registry is authoritative WHEN IT
# SPEAKS and silent otherwise:
#   * a row for SELF_KEY itself already names STORE_PHYSICAL -- this is our
#     own store from a prior --record-decision run (a re-run, possibly
#     under a renamed --project); sets MC_REGISTRY_STORE_OWNER="self" and
#     returns 0 immediately, before any other row is even considered, so a
#     same-checkout re-run is never second-guessed by a stale README
#     project name check.
#   * a row for a DIFFERENT key names STORE_PHYSICAL -- another checkout
#     already owns it; sets MC_REGISTRY_STORE_OWNER to that row's KEY and
#     returns 0.
#   * no row anywhere names STORE_PHYSICAL (including a missing decisions
#     file, or no --record-decision ever run) -- the registry has nothing
#     to say; sets MC_REGISTRY_STORE_OWNER="" and returns 1, telling the
#     caller to fall back to the README check.
mc_registry_owner_of_store() {
    local file="$1" store_phys="$2" self_key="$3"
    local line k rest decision when note store_val self_row_found=0
    MC_REGISTRY_STORE_OWNER=""
    [ -f "$file" ] || return 1
    while IFS= read -r line || [ -n "$line" ]; do
        case "$line" in
            \#*|"") continue ;;
        esac
        k="${line%%"$MC_TAB"*}"
        rest="${line#*"$MC_TAB"}"
        IFS="$MC_TAB" read -r decision when note <<<"$rest"
        mc_note_field "$note" "store"
        store_val="$MC_NOTE_FIELD"
        [ -n "$store_val" ] || continue
        [ "$(mc_physical "$store_val")" = "$store_phys" ] || continue
        if [ "$k" = "$self_key" ]; then
            MC_REGISTRY_STORE_OWNER="self"
            return 0
        fi
        # A foreign row matched -- keep scanning only long enough to make
        # sure a LATER row for SELF_KEY (decisions.tsv is append-only per
        # key via mc_registry_rewrite_row, so at most one row per key
        # exists at a time, but nothing here assumes that) does not also
        # claim this path; remember it and continue.
        MC_REGISTRY_STORE_OWNER="$k"
        self_row_found=1
    done < "$file"
    [ "$self_row_found" -eq 1 ] && return 0
    MC_REGISTRY_STORE_OWNER=""
    return 1
}

# mc_store_belongs_elsewhere CANDIDATE CHECKOUT PROJECT
#
# True iff CANDIDATE already exists as a marked store (mc_is_marked_store)
# belonging to a checkout or project OTHER than CHECKOUT/PROJECT -- the
# guard mc_default_store_for below needs before it can silently hand out a
# path shared by unrelated checkouts. Three signals, most authoritative
# first:
#   1. decisions.tsv's own store= field (mc_registry_owner_of_store), keyed
#      by CHECKOUT's own repo identity (mc_repo_key) -- authoritative when a
#      row exists for this exact store path, self or foreign, and only then.
#   2. Only when the registry has nothing to say: the rendered store
#      README's own checkout marker (mc_store_checkout_identity, fix wave 1
#      G9) compared PHYSICALLY against CHECKOUT -- an unambiguous path
#      comparison, unlike PROJECT, so two checkouts sharing a basename AND a
#      --project value still read as distinct (the G5 residual this closes).
#   3. Only when NEITHER of the above has anything to say (an older store
#      that predates the checkout marker, or a hand-authored README): the
#      rendered PROJECT name (mc_store_project_identity) compared against
#      PROJECT -- the weakest signal, since two checkouts CAN legitimately
#      share a --project value by coincidence.
# Returns 1 (does not belong elsewhere) when CANDIDATE does not exist yet,
# is not a marked store, PROJECT is empty (identity unknowable -- an older
# caller, or a direct unit test, that never had a project to compare), or
# no signal disagrees with us. Never fails open into a false positive: an
# unknown identity is treated as "not elsewhere", same as before this
# function existed.
mc_store_belongs_elsewhere() {
    local candidate="$1" checkout="$2" project="$3" phys decisions self_key checkout_phys
    [ -e "$candidate" ] || return 1
    mc_is_marked_store "$candidate" || return 1
    phys="$(mc_physical "$candidate")"
    mc_resolve_home
    decisions="$MEMCONTINUUM_HOME/decisions.tsv"
    self_key=""
    mc_repo_key "$checkout" && self_key="$MC_REPO_KEY"
    if mc_registry_owner_of_store "$decisions" "$phys" "$self_key"; then
        [ "$MC_REGISTRY_STORE_OWNER" = "self" ] && return 1
        return 0
    fi
    if mc_store_checkout_identity "$candidate"; then
        checkout_phys="$(mc_physical "$checkout")"
        # Fix round 2 R5 (CI's macOS job, reproduced on the Mac mini at
        # 77e52d1): the stamp used to be compared VERBATIM against
        # mc_physical(CHECKOUT) -- correct only when the stamped path and
        # the live checkout already resolve to the identical string. On
        # macOS a checkout under $TMPDIR is /var/folders/... logically and
        # /private/var/folders/... physically; CWD_TOPLEVEL (what repo-
        # init.sh stamps at store-creation time, below) returns the
        # logical form, so a store never matched its own checkout there.
        # mc_physical falls back to its input unchanged when the path does
        # not (or no longer) exist, so a stamp whose directory is gone
        # still stays a verbatim comparison -- still a mismatch, same as
        # before this fix.
        [ "$(mc_physical "$MC_STORE_CHECKOUT")" = "$checkout_phys" ] && return 1
        return 0
    fi
    [ -n "$project" ] || return 1
    if mc_store_project_identity "$candidate"; then
        [ "$MC_STORE_PROJECT" != "$project" ] && return 0
    fi
    return 1
}

# mc_default_store_for CHECKOUT [PROJECT]
#
# The default --store scripts/repo-init.sh applies when a git checkout's cwd
# gives it none: ordinarily the marked SIBLING name,
# "<dirname>/<basename>-MemContinuum-Store" (owner convention, 2026-08-31:
# never a generic "memory/", never a bare "MemContinuum"). But when CHECKOUT
# sits on a Windows-mounted drive under WSL (mc_is_windows_mounted_checkout
# above), a store there costs SECONDS per walk (drvfs/9P latency), not
# milliseconds -- so the default instead lands on the WSL-native disk:
# "$HOME/dev/<basename>-MemContinuum-Store" when "$HOME/dev" is a directory
# (this machine's convention for where checkouts live), else
# "$HOME/<basename>-MemContinuum-Store" (TOP-0109 L5).
#
# The sibling rule can never collide (a directory cannot hold two entries
# named alike), but the WSL-disk rule keys ONLY on CHECKOUT's basename --
# two different checkouts sharing one (client-a/app and client-b/app) used
# to collapse onto the identical default and silently share one store
# (whole-branch-review Codex 5). Now, only on the WSL branch: the plain name
# is tried first, UNCHANGED, for the first checkout ever to want it (never
# preemptively disambiguated); when it is already a DIFFERENT checkout's or
# project's store (mc_store_belongs_elsewhere), the checkout's own PARENT
# directory name disambiguates it ("<parent>-<basename>-MemContinuum-
# Store"); when even THAT is already a different checkout's or project's
# store, this refuses outright (MC_DEFAULT_STORE cleared,
# MC_DEFAULT_STORE_REFUSED_WHY set, returns 1) rather than guess a third
# name or silently share -- the installer asks for --store instead. A
# checkout re-running against its OWN already-registered or
# already-same-project store never disambiguates or refuses; it lands on
# the plain name exactly as before this fix.
#
# PROJECT is optional (mc_store_belongs_elsewhere treats an empty PROJECT as
# "identity unknowable", never as a mismatch) so every existing direct
# caller of this function that passes only CHECKOUT keeps its prior
# behaviour unchanged.
#
# CHECKOUT is expected already physical (git rev-parse --show-toplevel's own
# output, which the one caller here already is) -- this function does no
# resolution of its own.
#
# Sets MC_DEFAULT_STORE (the computed path, or "" on refusal) and
# MC_DEFAULT_STORE_WHY -- empty for the ordinary sibling rule, or a one-line
# explanation the installer prints when the WSL rule (plain or
# disambiguated) fired. MC_DEFAULT_STORE_REFUSED_WHY is set only when this
# returns 1. Deliberately the ONE place either rule is computed:
# scripts/memcontinuum-decide.sh and hooks/memcontinuum-detect.sh never
# compute or print a default store of their own (verified by reading both
# -- decide.sh only ever records a --store it is explicitly given, and
# detect.sh only ever reports whether a decision exists, never a proposed
# path), so this function currently has exactly one caller. Kept here
# anyway, alongside every other shared predicate in this file, rather than
# inlined into repo-init.sh, so a second caller never has to duplicate it
# to agree.
mc_default_store_for() {
    local checkout="$1" project="${2:-}" name base_dir plain candidate parent
    name="$(basename "$checkout")"
    MC_DEFAULT_STORE_WHY=""
    MC_DEFAULT_STORE_REFUSED_WHY=""
    if mc_is_windows_mounted_checkout "$checkout"; then
        if [ -d "$HOME/dev" ]; then
            base_dir="$HOME/dev"
        else
            base_dir="$HOME"
        fi
        plain="$base_dir/$name-MemContinuum-Store"
        if mc_store_belongs_elsewhere "$plain" "$checkout" "$project"; then
            parent="$(basename "$(dirname "$checkout")")"
            candidate="$base_dir/$parent-$name-MemContinuum-Store"
            if mc_store_belongs_elsewhere "$candidate" "$checkout" "$project"; then
                MC_DEFAULT_STORE=""
                MC_DEFAULT_STORE_REFUSED_WHY="both $plain and $candidate already belong to a different checkout or project"
                return 1
            fi
            MC_DEFAULT_STORE="$candidate"
            MC_DEFAULT_STORE_WHY="store defaults to $MC_DEFAULT_STORE: the checkout is on a Windows-mounted drive, and the plain name $plain already belongs to a different checkout or project, so the parent directory name disambiguates"
        else
            MC_DEFAULT_STORE="$plain"
            MC_DEFAULT_STORE_WHY="store defaults to $MC_DEFAULT_STORE: the checkout is on a Windows-mounted drive, where a store walk costs seconds"
        fi
    else
        MC_DEFAULT_STORE="$(dirname "$checkout")/$name-MemContinuum-Store"
    fi
    return 0
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

# mc_rules_identity_marker ENGINE_ROOT
#
# Reads the identity marker for a rendered <claude-dir>/rules/memcontinuum.md
# out of the template that defines it: line 1 of
# templates/memcontinuum-rules.md. Sets MC_RULES_MARKER and returns 0; sets it
# empty and returns 1 when the template is not readable.
#
# There is exactly one place this string is written down, and it is the
# template. It used to be copied into the installer, the re-render walk, and
# the tests: five copies that had to be edited in lockstep, and any template
# whose first line moved on would have made every file the installer renders
# read as "foreign" -- refusing to overwrite its own output.
mc_rules_identity_marker() {
    local tmpl="$1/templates/memcontinuum-rules.md"
    MC_RULES_MARKER=""
    [ -f "$tmpl" ] || return 1
    IFS= read -r MC_RULES_MARKER < "$tmpl" || :
    [ -n "$MC_RULES_MARKER" ]
}

# mc_skill_identity_marker ENGINE_ROOT
#
# Reads the identity marker for an installed memory-search SKILL.md copy out
# of the template that defines it: the `name: ...` line inside
# skills/memory-search/SKILL.md's OWN frontmatter (bounded by its own
# closing `---` -- the same boundary rule mc_skill_copy_is_ours uses on the
# installed copy, so a `name:` line appearing in the template's BODY is
# never picked up). Sets MC_SKILL_MARKER (e.g. `name: memory-search`) and
# returns 0; sets it empty and returns 1 when the template is not readable
# or its frontmatter carries no `name:` line.
#
# Symmetric with mc_rules_identity_marker, and for the same reason: there is
# exactly one place this string is written down, and it is the template --
# never a literal hardcoded here or in a caller. A skill rename then moves
# the marker everywhere that reads it, rather than making every
# previously-installed copy read as foreign the moment the template changes
# out from under a hardcoded copy of its old name.
mc_skill_identity_marker() {
    local tmpl="$1/skills/memory-search/SKILL.md" fm_end
    MC_SKILL_MARKER=""
    [ -f "$tmpl" ] || return 1
    fm_end="$(grep -n '^---$' "$tmpl" | sed -n '2p' | cut -d: -f1)"
    [ -n "$fm_end" ] || return 1
    MC_SKILL_MARKER="$(sed -n "1,${fm_end}p" "$tmpl" | grep '^name:' | head -n 1)"
    [ -n "$MC_SKILL_MARKER" ]
}

# mc_skill_copy_is_ours DEST MARKER
#
# True iff DEST (a path to an installed memory-search SKILL.md copy) carries
# MARKER (the `name: ...` line mc_skill_identity_marker read from the
# template, at runtime -- never a literal hardcoded here) inside its own
# frontmatter. The same test repo-init.sh and memcontinuum-update.sh both
# need, and used to each hardcode their own copy of. Unlike the rules file,
# the skill copy's identity cannot be a fixed first line: the opening `---`
# has to stay byte 0 for the skill loader (repo-init stamps right after the
# frontmatter's CLOSING `---` instead), so identity here is "the frontmatter
# carries MARKER" -- scanned only up to that closing `---`, never the whole
# file, so a hand-authored file whose BODY happens to mention MARKER after
# its own frontmatter does not read as ours.
#
# Sets MC_SKILL_FM_END to the 1-based line number of the closing `---` when
# found (empty otherwise) -- the stamp comment sits on the line right after
# it, and callers that need the stamp read it from there instead of
# re-finding the boundary themselves. Returns 0 when DEST is ours, 1
# otherwise (a missing file, no MARKER given, a file with no
# two-`---`-line frontmatter, or one whose frontmatter names something
# else).
mc_skill_copy_is_ours() {
    local dest="$1" marker="$2"
    MC_SKILL_FM_END=""
    [ -f "$dest" ] || return 1
    [ -n "$marker" ] || return 1
    MC_SKILL_FM_END="$(grep -n '^---$' "$dest" | sed -n '2p' | cut -d: -f1)"
    [ -n "$MC_SKILL_FM_END" ] || return 1
    sed -n "1,${MC_SKILL_FM_END}p" "$dest" | grep -Fqx -- "$marker"
}

# mc_installer_wrapper_shape HOOKPATH HOOKS_DIR SCRIPT
#
# True (rc 0) only when HOOKPATH is EXACTLY the shape
# scripts/repo-init.sh's install_store_hook_wrapper generates for SCRIPT
# (found under HOOKS_DIR): five lines, no more and no fewer -- a shebang, the
# three MEMCONTINUUM_* exports (values may be stale from an earlier install
# with a different store/project/python -- only the KEYS are checked, so a
# stale-but-ours wrapper still reads as ours), and an exec line naming
# HOOKS_DIR/SCRIPT verbatim.
#
# Moved here (I2, updater-coverage workstream) from a private function of
# the same shape inside scripts/repo-init.sh so scripts/memcontinuum-update.sh
# can ask the identical "is this ours" question for its own store-hooks
# health column -- the same reasoning mc_skill_copy_is_ours above already
# follows: the installer's own refusal and the walker's own report must
# agree, by construction, on what "ours" means, rather than each carrying a
# separate copy of the check that can drift apart. repo-init.sh calls this
# with its own $HOOKS_DIR; nothing else about its behavior changed.
mc_installer_wrapper_shape() {
    local hookpath="$1" hooks_dir="$2" script="$3"
    local expected_exec line n=0
    local -a lines=()
    expected_exec="exec bash $(printf '%q' "$hooks_dir/$script")"
    while IFS= read -r line || [ -n "$line" ]; do
        lines[$n]="$line"
        n=$((n + 1))
    done < "$hookpath"
    [ "$n" -eq 5 ] || return 1
    [ "${lines[0]}" = "#!/usr/bin/env bash" ] || return 1
    case "${lines[1]}" in
        "export MEMCONTINUUM_ROOT="*) ;;
        *) return 1 ;;
    esac
    case "${lines[2]}" in
        "export MEMCONTINUUM_PROJECT="*) ;;
        *) return 1 ;;
    esac
    case "${lines[3]}" in
        "export MEMCONTINUUM_PYTHON="*) ;;
        *) return 1 ;;
    esac
    [ "${lines[4]}" = "$expected_exec" ] || return 1
    return 0
}

# mc_store_hook_wrapper_state HOOKPATH HOOKS_DIR SCRIPT STORE PROJECT PYTHON_BIN
#
# Sets MC_STORE_HOOK_STATE to one of missing/foreign/stale/ok for the store
# git hook wrapper at HOOKPATH (STORE's post-commit or pre-commit). There is
# no render stamp on these wrappers the way there is on a hook line, the
# rules file or the skill copy -- they are three raw values (MEMCONTINUUM_
# ROOT/PROJECT/PYTHON), not a template with a fingerprint comment -- so
# currency can only be judged by re-deriving the exact bytes
# scripts/repo-init.sh's install_store_hook_wrapper would write right now for
# these STORE/PROJECT/PYTHON_BIN and comparing byte-for-byte. "ok" only on an
# exact match; a shape match (mc_installer_wrapper_shape -- ours, by
# construction the same identity repo-init.sh's own refusal uses) with
# different bytes is "stale"; no shape match at all is "foreign"; no file at
# all is "missing".
mc_store_hook_wrapper_state() {
    local hookpath="$1" hooks_dir="$2" script="$3" store="$4" project="$5" python_bin="$6"
    local expected
    if [ ! -f "$hookpath" ]; then
        MC_STORE_HOOK_STATE="missing"
        return 0
    fi
    if ! mc_installer_wrapper_shape "$hookpath" "$hooks_dir" "$script"; then
        MC_STORE_HOOK_STATE="foreign"
        return 0
    fi
    expected="$(
        printf '#!/usr/bin/env bash\n'
        printf 'export MEMCONTINUUM_ROOT=%s\n' "$(printf '%q' "$store")"
        printf 'export MEMCONTINUUM_PROJECT=%s\n' "$(printf '%q' "$project")"
        printf 'export MEMCONTINUUM_PYTHON=%s\n' "$(printf '%q' "$python_bin")"
        printf 'exec bash %s\n' "$(printf '%q' "$hooks_dir/$script")"
    )"
    if [ "$expected" = "$(cat "$hookpath")" ]; then
        MC_STORE_HOOK_STATE="ok"
    else
        MC_STORE_HOOK_STATE="stale"
    fi
    return 0
}

# mc_store_hooks_dir STORE
#
# Resolves the git hooks directory git actually consults for commits made in
# STORE (`git -C STORE rev-parse --git-path hooks`, the same resolver
# scripts/repo-init.sh's own git_hooks_dir_for uses), refusing (return 1)
# when that resolves OUTSIDE STORE's own `--git-common-dir` -- a shared or
# global core.hooksPath, the exact condition scripts/repo-init.sh itself
# refuses to install a wrapper into (the append-only guard must never run
# for repositories other than its own store). Sets MC_STORE_HOOKS_DIR on
# success; MC_STORE_HOOKS_DIR_WHY (never STORE_HOOKS_DIR) on failure -- STORE
# is not a git repository at all, or the resolved dir is outside it. A
# caller that cannot resolve this dir has no path to check the wrappers
# against at all, which is exactly scripts/memcontinuum-update.sh's
# store-hooks "not-checked" case: honestly saying it cannot look, rather
# than guessing or silently reporting nothing.
#
# Deliberately a fresh, independent resolution rather than repo-init.sh's
# own git_hooks_dir_for moved here: that function's callers also carry
# install-specific status bookkeeping (POST_COMMIT_STATUS/skipped-foreign
# messages) this read-only lookup has no business touching, and the git
# calls themselves are the entire function -- the same "deliberately
# duplicated, not sourced" reasoning scripts/memcontinuum-update.sh's own
# mc_update_resolve_python already documents for repo-init.sh's
# resolve_python().
mc_store_hooks_dir() {
    local store="$1" raw resolved store_git_dir
    MC_STORE_HOOKS_DIR=""
    MC_STORE_HOOKS_DIR_WHY=""
    raw="$(git -C "$store" rev-parse --git-path hooks 2>/dev/null)" || {
        MC_STORE_HOOKS_DIR_WHY="not a git repository"
        return 1
    }
    case "$raw" in
        /*) resolved="$raw" ;;
        *) resolved="$store/$raw" ;;
    esac
    store_git_dir="$(git -C "$store" rev-parse --git-common-dir 2>/dev/null)" || {
        MC_STORE_HOOKS_DIR_WHY="could not resolve the store's git directory"
        return 1
    }
    case "$store_git_dir" in
        /*) ;;
        *) store_git_dir="$store/$store_git_dir" ;;
    esac
    resolved="$(mc_physical "$resolved")"
    store_git_dir="$(mc_physical "$store_git_dir")"
    case "$resolved" in
        "$store_git_dir"/*)
            MC_STORE_HOOKS_DIR="$resolved"
            return 0
            ;;
        *)
            MC_STORE_HOOKS_DIR_WHY="core.hooksPath resolves outside the store's own .git ($resolved is outside $store_git_dir)"
            return 1
            ;;
    esac
}

# mc_render_fingerprint SCOPE ENGINE_ROOT   (SCOPE: repo | machine)
#
# The stamp every rendered artifact carries: 12 hex characters of a sha256
# over this checkout's RENDER INPUTS. Sets MC_RENDER_FINGERPRINT and returns
# 0; sets it to the literal "unknown" and returns 1 when no sha256 tool is
# available or the checkout is missing the inputs (which the callers treat as
# "cannot tell -- re-render to find out", never as an error).
#
# Why not the engine's HEAD commit, which is what this used to be. Two kinds
# of change reach a wired repository in completely different ways:
#
#   a fix to a SCRIPT (a hook, memidx.py, the walker) reaches every wired repo
#   the moment the checkout is pulled -- every rendered hook line runs that
#   script from this checkout by absolute path. Nothing needs re-rendering.
#
#   a change to what gets RENDERED (a template, the installer's own rendering,
#   the settings merge, a skill that gets copied into a repo) reaches nobody
#   until the installer is re-run there.
#
# A HEAD sha moves on both, so it marked every wired repo on the machine
# stale after any commit to anything -- the distinction the update command
# exists to draw, drawn wrong. Hashing the inputs draws it exactly: a
# scripts-only commit leaves every repo `ok`; a template or installer change
# flips them `stale`.
#
# Hook SCRIPTS are deliberately NOT inputs -- they are executed by path, and
# pulling updates them live. That includes hooks/memcontinuum-detect.sh: the
# machine layer renders a hook LINE naming it, not a copy of it.
#
# TWO SCOPES, because the two layers are re-rendered by different commands and
# a repository has no way to act on the other one's drift. A change to
# memcontinuum-setup.sh or the machine-level skill affects only what lives in
# ~/.claude; with one combined fingerprint it marked every PER-REPO row
# `stale`, and `--apply` then re-rendered all of them to no effect while
# leaving the drift that actually existed untouched.
#
#   repo     what scripts/repo-init.sh renders into a project's claude-dir
#     scripts/repo-init.sh           -- does the rendering
#     scripts/mc_settings_merge.py   -- decides how rendered blocks land
#     templates/*                    -- everything rendered from a template
#     skills/memory-search/SKILL.md  -- the skill copied into a project
#
#   machine  what memcontinuum-setup.sh renders into ~/.claude
#     memcontinuum-setup.sh          -- renders the detector hook line and
#                                       config.sh (both templated inside it)
#     scripts/mc_settings_merge.py   -- lands the detector entry too
#     skills/memcontinuum/SKILL.md   -- the skill copied to the user level
#
# mc_settings_merge.py is in both on purpose: it is what actually writes the
# rendered blocks, per repo and machine-wide alike.
#
# Each input is preceded by its path relative to the checkout, so adding,
# removing or renaming one changes the fingerprint even when the bytes
# elsewhere are identical.
mc_render_fingerprint() {
    local scope="$1" root="$2" f hasher out
    local -a files=()
    MC_RENDER_FINGERPRINT="unknown"
    case "$scope" in
        repo|machine) ;;
        *) return 1 ;;
    esac
    if command -v sha256sum >/dev/null 2>&1; then
        hasher="sha256sum"
    elif command -v shasum >/dev/null 2>&1; then
        hasher="shasum -a 256"
    else
        return 1
    fi
    # LC_ALL=C so glob expansion is byte-ordered, and therefore the same on
    # every machine -- a locale-dependent order would give the same checkout
    # two different fingerprints.
    local saved_lc="${LC_ALL:-__mc_unset__}"
    LC_ALL=C
    if [ "$scope" = "repo" ]; then
        for f in "$root/scripts/repo-init.sh" "$root/scripts/mc_settings_merge.py"; do
            [ -f "$f" ] && files[${#files[@]}]="$f"
        done
        for f in "$root"/templates/*; do
            [ -f "$f" ] && files[${#files[@]}]="$f"
        done
        [ -f "$root/skills/memory-search/SKILL.md" ] && \
            files[${#files[@]}]="$root/skills/memory-search/SKILL.md"
    else
        for f in "$root/memcontinuum-setup.sh" "$root/scripts/mc_settings_merge.py" \
                 "$root/skills/memcontinuum/SKILL.md"; do
            [ -f "$f" ] && files[${#files[@]}]="$f"
        done
    fi
    if [ "$saved_lc" = "__mc_unset__" ]; then unset LC_ALL; else LC_ALL="$saved_lc"; fi
    # An incomplete checkout cannot be fingerprinted honestly.
    [ "${#files[@]}" -ge 3 ] || return 1
    out="$(
        for f in "${files[@]}"; do
            printf '%s\n' "${f#"$root"/}"
            cat "$f"
        done | $hasher
    )" || return 1
    out="${out%% *}"
    case "$out" in
        [0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]*) ;;
        *) return 1 ;;
    esac
    MC_RENDER_FINGERPRINT="${out:0:12}"
    return 0
}

# mc_fingerprint_match RENDERED ENGINE
#
# True iff the two render fingerprints are the same KNOWN value. The ONE
# comparison every caller uses -- the table's stamp column, the rules file's
# own stamp line, the machine layer, and state.sh's drift hint.
#
# `unknown` is what mc_render_fingerprint returns when it could not compute a
# fingerprint at all: no sha256 tool on the machine, or a checkout missing its
# render inputs. It is the ABSENCE of an answer, not an answer, and two
# absences are not an agreement -- `[ "$a" = "$b" ]` on them reports the
# artifact as current, which is precisely the claim nobody was able to check.
# The empty string is the same thing arriving by a different route (a hook
# line with no MEMCONTINUUM_RENDERED on it at all).
#
# So: unknown or empty on either side means no match, and a no-match means
# `stale` -- re-render and find out. Re-rendering something already current is
# a no-op; calling something current that nobody verified is not.
mc_fingerprint_match() {
    case "${1:-}" in ""|unknown) return 1 ;; esac
    case "${2:-}" in ""|unknown) return 1 ;; esac
    [ "$1" = "$2" ]
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

# mc_config_managed_python CONFIG_FILE
#
# Read MEMCONTINUUM_PYTHON and MEMCONTINUUM_VENV_MANAGED off a config.sh AS
# THEY STAND ON DISK, ignoring whatever the calling environment already has
# under those names. Sets MC_CONFIG_PYTHON (empty when the file records
# none) and MC_CONFIG_MANAGED ("0" or "1"); returns 0 when the file was
# read, 1 when it does not exist, with both cleared.
#
# The `unset` inside each subshell is the whole point, and it is not
# optional. config.sh writes its python line conditionally --
# `if [ -z "${MEMCONTINUUM_PYTHON:-}" ]; then MEMCONTINUUM_PYTHON=...; fi`,
# deliberately, so an explicit env override still wins for ordinary
# resolution -- and a subshell inherits every variable this process has,
# exported or not. Left unguarded, a caller that already has
# MEMCONTINUUM_PYTHON set reads back its OWN value and calls it the disk
# truth, while MEMCONTINUUM_VENV_MANAGED (written unconditionally) reads the
# real file. That mismatched pair is how a foreign python acquires a
# managed=1 flag it never earned.
#
# Two decisions need this specific read, and both are decisions ABOUT the
# recorded install rather than about which python to run now:
# memcontinuum-setup.sh's sticky managed-venv determination, and
# memcontinuum-update.sh --machine's choice of whether it may reinstall
# requirements.lock into that python. Ordinary "which python do I run"
# resolution stays env-first and does not come through here.
mc_config_managed_python() {
    local config="${1:-}"
    MC_CONFIG_PYTHON=""
    MC_CONFIG_MANAGED="0"
    [ -n "$config" ] && [ -f "$config" ] || return 1
    MC_CONFIG_PYTHON="$(
        unset MEMCONTINUUM_PYTHON MEMCONTINUUM_VENV_MANAGED
        # shellcheck source=/dev/null
        . "$config" >/dev/null 2>&1
        printf '%s' "${MEMCONTINUUM_PYTHON:-}"
    )"
    MC_CONFIG_MANAGED="$(
        unset MEMCONTINUUM_PYTHON MEMCONTINUUM_VENV_MANAGED
        # shellcheck source=/dev/null
        . "$config" >/dev/null 2>&1
        printf '%s' "${MEMCONTINUUM_VENV_MANAGED:-0}"
    )"
    return 0
}
