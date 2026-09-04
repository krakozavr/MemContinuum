#!/usr/bin/env bash
# usage: memcontinuum-decide.sh <wired|declined|never-ask|ask-again|forget>
#                               --repo PATH [--store DIR] [--project NAME]
#                               [--claude-dir DIR ...] [--code-root DIR ...]
#                               [--langs LIST] [--never-ext LIST]
#
# Record a human's answer about one repository, so the SessionStart detector
# never asks again. Called by the `memcontinuum` skill AFTER a human has
# answered -- never by a hook, and never to guess.
#
#   wired      they said yes AND the hooks are actually wired -- refused when
#              settings under the repo's .claude (or every --claude-dir given)
#              don't reference all five always-wired write-side hooks,
#              because a wired row silences the detector forever whether or
#              not the install ever succeeded
#   declined   they said no
#   forget     drop this repo's row, returning it to "undecided"
#   never-ask  machine-wide: stop asking in every repo
#   ask-again  undo never-ask
#
# --repo is REQUIRED for wired/declined/forget: each of these three silences
# or unsilences one specific repo permanently, and there is no safe $PWD
# default for a write like that -- a shell sitting in the engine checkout
# would record the answer against the ENGINE's key while the repo actually
# meant stayed undecided forever. Name the repo explicitly.
# never-ask/ask-again are machine-wide and take no repo at all.
#
# --claude-dir DIR   (wired only, repeatable) one claude-dir this project's
#                    wiring lives in. A project can have more than one (a
#                    working-dir .claude beside a bare code checkout, or two
#                    separate session homes pointed at the same store) --
#                    give it once per claude-dir; every one given must be
#                    fully wired, or the whole command is refused. Omit for
#                    the common case (a single --repo/.claude) -- defaults to
#                    that one directory, matching the pre-existing behavior.
# --code-root DIR    (wired only, repeatable) a code checkout this project's
#                    wiring watches. Recorded so a later re-render (see
#                    scripts/memcontinuum-update.sh) never has to re-derive
#                    it from settings.
# --langs LIST       (wired only) comma-separated language set this wiring's
#                    code index was enabled for.
# --never-ext LIST   (wired only) comma-separated extensions the new-file
#                    reminder was told to never mention again for this
#                    wiring.
#
# None of --claude-dir/--code-root/--langs/--never-ext changes what "wired"
# means or what gets verified -- they are recorded so a later re-render (a
# rendered-artifact fix in the engine, or `--add-lang`/`--never-ext`) can
# reproduce this exact install without re-asking. This command still never
# writes a consent nobody gave: recording these alongside "wired" only ever
# happens because the human already said yes to wiring this repo.
#
# The registry is a TSV at $MEMCONTINUUM_HOME/decisions.tsv:
#   key <TAB> decision <TAB> iso-date <TAB> note
# Key = origin remote URL when there is one, else the working tree path --
# remote-keyed so a repo that moves on disk keeps its decision.
# note (wired rows) = "store=S project=P claude-dirs=a;b code-roots=c;d
# langs=python;swift never=.cs;.h" -- store=/project= are unconditional
# (blank when not given, matching the pre-existing behavior); the four
# newer fields are OMITTED entirely (not written blank) when never given, so
# an older row and one written with no --claude-dir/--code-root/--langs/
# --never-ext at all look identical, and mc_note_field (mc-registry-lib.sh)
# reads either shape the same way -- "" for a field that was never recorded.
# --MC-USAGE-END--
#
# Known path-key staleness (accepted, reviewer finding 2026-08-31): a NEW repo
# created at a path whose previous occupant left a path-keyed row inherits
# that answer and is not asked -- the recycled-scratch-directory case. The fix
# is `forget` from inside that directory. Detecting recycling automatically
# would mean fingerprinting repo identity beyond the path, which this registry
# deliberately does not do.

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
# shellcheck source=./mc-registry-lib.sh
. "$SCRIPT_DIR/mc-registry-lib.sh" || { echo "missing $SCRIPT_DIR/mc-registry-lib.sh -- incomplete checkout" >&2; exit 1; }

mc_resolve_home
DECISIONS="$MEMCONTINUUM_HOME/decisions.tsv"

# scan to the explicit end marker above rather than a hardcoded line count --
# a hardcoded `sed -n '2,Np'` silently truncates or overruns usage() every
# time this header is edited (fix-round-4 finding).
usage() {
    sed -n '2,/^# --MC-USAGE-END--$/p' "$0" | grep -v '^# --MC-USAGE-END--$' | sed 's/^# \{0,1\}//'
    exit "${1:-1}"
}

[ $# -ge 1 ] || usage 1
# --help before the action dispatch: asking for help is not an unknown action.
case "$1" in -h|--help) usage 0 ;; esac
ACTION="$1"; shift

STORE=""; PROJECT=""; REPO_ARG=""; LANGS=""; NEVER_EXT=""
declare -a CLAUDE_DIRS=()
declare -a CODE_ROOTS=()
# A two-argument option with no value must ERROR, not loop: `shift 2` on a
# one-element argv fails and leaves the argument in place, which spins this
# loop forever (round-3 reviewer finding, reproduced).
need_value() { [ $# -ge 2 ] || { echo "missing value for $1" >&2; exit 2; }; }
while [ $# -gt 0 ]; do
    case "$1" in
        --store) need_value "$@"; STORE="$2"; shift 2 ;;
        --project) need_value "$@"; PROJECT="$2"; shift 2 ;;
        --repo) need_value "$@"; REPO_ARG="$2"; shift 2 ;;
        --claude-dir) need_value "$@"; CLAUDE_DIRS+=("$2"); shift 2 ;;
        --code-root) need_value "$@"; CODE_ROOTS+=("$2"); shift 2 ;;
        --langs) need_value "$@"; LANGS="$2"; shift 2 ;;
        --never-ext) need_value "$@"; NEVER_EXT="$2"; shift 2 ;;
        -h|--help) usage 0 ;;
        *) echo "unknown argument: $1" >&2; usage 1 ;;
    esac
done

# join_semi ARR... -- prints array elements joined by ';', "" for a
# zero-element array, each element percent-encoded for the note column
# (mc_note_encode, scripts/mc-registry-lib.sh). Bash-3.2-safe (no
# `${arr[*]/%/;}` tricks, no external tr/paste dependency): plain iteration.
join_semi() {
    local out="" first=1 a
    for a in "$@"; do
        a="$(mc_note_encode "$a")"
        if [ "$first" -eq 1 ]; then out="$a"; first=0; else out="$out;$a"; fi
    done
    printf '%s' "$out"
}

# check_storable LABEL VALUE -- refuses the three characters the note column
# cannot represent. A space is fine (it is encoded); `;` separates the
# elements of a list field, and a tab or newline would fake a TSV column or a
# whole extra row. Storing any of them by guessing at a split is worse than
# saying so.
check_storable() {
    case "$2" in
        *";"*)  echo "REFUSED: $1 contains ';', which separates the elements of a registry list field: $2" >&2; exit 2 ;;
        *"$MC_TAB"*) echo "REFUSED: $1 contains a tab, which separates registry columns: $2" >&2; exit 2 ;;
        *"
"*) echo "REFUSED: $1 contains a newline, which separates registry rows: $2" >&2; exit 2 ;;
    esac
}

mkdir -p "$MEMCONTINUUM_HOME" || { echo "cannot create $MEMCONTINUUM_HOME" >&2; exit 1; }

case "$ACTION" in
    never-ask)
        : > "$MEMCONTINUUM_HOME/no-ask"
        echo "recorded: never ask about MemContinuum in any repo on this machine"
        echo "undo with: $0 ask-again"
        exit 0 ;;
    ask-again)
        rm -f "$MEMCONTINUUM_HOME/no-ask"
        echo "recorded: asking re-enabled machine-wide"
        exit 0 ;;
    wired|declined|forget) ;;
    *) echo "unknown action: $ACTION" >&2; usage 1 ;;
esac

# F2: no $PWD fallback for a write that silences a repo permanently -- see
# the header note above.
[ -n "$REPO_ARG" ] || {
    echo "the '$ACTION' action requires an explicit repo: pass --repo PATH" >&2
    exit 2
}

if ! mc_repo_key "$REPO_ARG"; then
    echo "not a git repository: $REPO_ARG" >&2
    exit 1
fi
REPO="$MC_REPO"
KEY="$MC_REPO_KEY"

# Resolve every path argument PHYSICALLY before it is validated or written
# (symlink-review round 3, concern 2): every OTHER recording site --
# repo-init.sh's own --store/--code-root (abspath(), Ruling 89), and
# memcontinuum-update.sh's own migration overrides -- already resolves
# through this same mc_physical (scripts/mc-registry-lib.sh) before the
# value reaches the registry; this script was the one gap left, recording
# --store (and --claude-dir/--code-root) raw. A row written here with a
# symlinked path would otherwise disagree with what repo-init.sh itself
# renders for the identical value, the exact registry-vs-rendered
# divergence class this whole task exists to close -- and since a human
# can type any of these three flags directly into this script (bypassing
# repo-init.sh's own resolution entirely, "wired"'s normal path when a
# --record-decision-driven install did not just run it), this is the ONE
# place that class of divergence can still originate. --repo does NOT need
# this: it is never written into the note directly, only used to derive
# REPO/KEY via mc_repo_key above, which is already physical (git resolves
# via getcwd(), never the shell's logical $PWD). Guarded on non-empty:
# mc_physical("") would silently return this PROCESS's own cwd (`cd ""` is
# a no-op in bash), never an error -- worse than leaving an empty value
# empty for check_storable/the usage checks below to catch as they always
# have.
#
# The `wired` DEFAULT claude-dir is assigned HERE, above the loop, rather
# than beside the wiring scan that consumes it (symlink-review round 4): an
# omitted --claude-dir is the skill's own documented `wired` command
# (skills/memcontinuum/SKILL.md), so a default assigned below this block is
# the one path that reaches the note unresolved -- a repo whose .claude is
# itself a symlink recorded raw while repo-init.sh renders into the physical
# target. Resolving here rather than at the note-building line keeps one
# choke point AND gives check_storable and the wiring scan the same value
# the row records: a physical target can carry a `;` its symlink's own name
# does not, and the scan reports the directory the row names. Every value
# the note can hold is assigned above this block; nothing below it assigns a
# path. Guarded on `wired` because that is the only action the default
# belongs to -- a `declined`/`forget` row names no claude-dir.
if [ "$ACTION" = "wired" ] && [ "${#CLAUDE_DIRS[@]}" -eq 0 ]; then
    CLAUDE_DIRS=("$REPO/.claude")
fi
[ -n "$STORE" ] && STORE="$(mc_physical "$STORE")"
for i in "${!CLAUDE_DIRS[@]}"; do
    [ -n "${CLAUDE_DIRS[$i]}" ] && CLAUDE_DIRS[$i]="$(mc_physical "${CLAUDE_DIRS[$i]}")"
done
for i in "${!CODE_ROOTS[@]}"; do
    [ -n "${CODE_ROOTS[$i]}" ] && CODE_ROOTS[$i]="$(mc_physical "${CODE_ROOTS[$i]}")"
done

# Recording `wired` is the one write that can lie: a wired row silences the
# detector forever, whether or not scripts/repo-init.sh ever succeeded. So
# verify the claim against the repo's own settings before recording it (same
# five-basename rule the detector and installer use). --claude-dir points the
# check elsewhere for projects whose wiring deliberately lives outside the
# repo (a working-dir .claude beside a bare code checkout, for instance);
# given more than once (INC-0104: one project, several claude-dirs), EVERY
# one given must be fully wired -- a row claiming "wired" must be true at
# every claude-dir it lists, not just the first.
# Argument validation before any work: a value the note column cannot hold is
# a bad argument, and saying so is more useful than a wiring report about a
# path that was never going to be storable anyway.
check_storable "--store" "$STORE"
check_storable "--project" "$PROJECT"
check_storable "--langs" "$LANGS"
check_storable "--never-ext" "$NEVER_EXT"
for CHECK_DIR in "${CLAUDE_DIRS[@]:-}"; do
    [ -n "$CHECK_DIR" ] && check_storable "--claude-dir" "$CHECK_DIR"
done
for CHECK_DIR in "${CODE_ROOTS[@]:-}"; do
    [ -n "$CHECK_DIR" ] && check_storable "--code-root" "$CHECK_DIR"
done

if [ "$ACTION" = "wired" ]; then
    # CLAUDE_DIRS is non-empty by here for `wired`: the default above fills
    # it before the physical resolution runs, so every dir scanned is the
    # same physical dir the note records.
    for CHECK_DIR in "${CLAUDE_DIRS[@]}"; do
        mc_wiring_scan "$CHECK_DIR/settings.local.json" "$CHECK_DIR/settings.json"
        if [ "$MC_WIRING" != "full" ]; then
            echo "REFUSED: wiring under $CHECK_DIR is '$MC_WIRING', not full -- missing:" >&2
            echo "  $MC_WIRING_MISSING" >&2
            echo "Run the installer first (the memcontinuum skill does this), or pass" >&2
            echo "--claude-dir if this repo's wiring deliberately lives elsewhere." >&2
            exit 1
        fi
    done
fi

NOTE=""
if [ -n "$STORE" ] || [ -n "$PROJECT" ]; then
    NOTE="store=$(mc_note_encode "$STORE") project=$(mc_note_encode "$PROJECT")"
fi
if [ "${#CLAUDE_DIRS[@]}" -gt 0 ]; then
    NOTE="$NOTE claude-dirs=$(join_semi "${CLAUDE_DIRS[@]}")"
fi
if [ "${#CODE_ROOTS[@]}" -gt 0 ]; then
    NOTE="$NOTE code-roots=$(join_semi "${CODE_ROOTS[@]}")"
fi
if [ -n "$LANGS" ]; then
    NOTE="$NOTE langs=$(mc_note_encode "$(printf '%s' "$LANGS" | tr ',' ';')")"
fi
if [ -n "$NEVER_EXT" ]; then
    NOTE="$NOTE never=$(mc_note_encode "$(printf '%s' "$NEVER_EXT" | tr ',' ';')")"
fi
# The unconditional store=/project= branch above may have left NOTE empty
# (forget/declined pass neither) while a later branch (wired, with only
# --claude-dir given and no --store/--project -- shouldn't happen via the
# skill, but decide.sh accepts it) appended " claude-dirs=..." straight
# onto an empty string. Trim exactly the one leading space that leaves.
NOTE="${NOTE# }"

# Rewrite without this key, then append -- so a reversal replaces the old row
# rather than shadowing it. mc_registry_rewrite_row (scripts/mc-registry-lib.sh
# -- the one shared implementation, symlink-review round 2 NEW-1) does the
# atomic temp-file+rename replacement; NEW_LINE omitted entirely for
# `forget` drops the row instead of replacing it.
NEW_LINE=""
if [ "$ACTION" != "forget" ]; then
    NEW_LINE="$(printf '%s\t%s\t%s\t%s' "$KEY" "$ACTION" "$(date +%Y-%m-%d)" "$NOTE")"
fi
mc_registry_rewrite_row "$DECISIONS" "$KEY" "$NEW_LINE" || { echo "failed to write $DECISIONS" >&2; exit 1; }

case "$ACTION" in
    wired)    echo "recorded: $KEY uses MemContinuum${NOTE:+ ($NOTE)}" ;;
    declined) echo "recorded: $KEY declined -- this repo will not be asked again" ;;
    forget)   echo "recorded: $KEY forgotten -- it will be asked about again" ;;
esac
