#!/usr/bin/env bash
# usage: memcontinuum-decide.sh <wired|declined|never-ask|ask-again|forget>
#                               --repo PATH [--store DIR] [--project NAME]
#                               [--claude-dir DIR]
#
# Record a human's answer about one repository, so the SessionStart detector
# never asks again. Called by the `memcontinuum` skill AFTER a human has
# answered -- never by a hook, and never to guess.
#
#   wired      they said yes AND the hooks are actually wired -- refused when
#              settings under the repo's .claude (or --claude-dir) don't
#              reference all five always-wired write-side hooks, because a
#              wired row silences the detector forever whether or not the
#              install ever succeeded
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
# The registry is a TSV at $MEMCONTINUUM_HOME/decisions.tsv:
#   key <TAB> decision <TAB> iso-date <TAB> note
# Key = origin remote URL when there is one, else the working tree path --
# remote-keyed so a repo that moves on disk keeps its decision.
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

STORE=""; PROJECT=""; REPO_ARG=""; CLAUDE_DIR_ARG=""
# A two-argument option with no value must ERROR, not loop: `shift 2` on a
# one-element argv fails and leaves the argument in place, which spins this
# loop forever (round-3 reviewer finding, reproduced).
need_value() { [ $# -ge 2 ] || { echo "missing value for $1" >&2; exit 2; }; }
while [ $# -gt 0 ]; do
    case "$1" in
        --store) need_value "$@"; STORE="$2"; shift 2 ;;
        --project) need_value "$@"; PROJECT="$2"; shift 2 ;;
        --repo) need_value "$@"; REPO_ARG="$2"; shift 2 ;;
        --claude-dir) need_value "$@"; CLAUDE_DIR_ARG="$2"; shift 2 ;;
        -h|--help) usage 0 ;;
        *) echo "unknown argument: $1" >&2; usage 1 ;;
    esac
done

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

# Recording `wired` is the one write that can lie: a wired row silences the
# detector forever, whether or not scripts/repo-init.sh ever succeeded. So
# verify the claim against the repo's own settings before recording it (same
# five-basename rule the detector and installer use). --claude-dir points the
# check elsewhere for projects whose wiring deliberately lives outside the
# repo (a working-dir .claude beside a bare code checkout, for instance).
if [ "$ACTION" = "wired" ]; then
    CHECK_DIR="${CLAUDE_DIR_ARG:-$REPO/.claude}"
    mc_wiring_scan "$CHECK_DIR/settings.local.json" "$CHECK_DIR/settings.json"
    if [ "$MC_WIRING" != "full" ]; then
        echo "REFUSED: wiring under $CHECK_DIR is '$MC_WIRING', not full -- missing:" >&2
        echo "  $MC_WIRING_MISSING" >&2
        echo "Run the installer first (the memcontinuum skill does this), or pass" >&2
        echo "--claude-dir if this repo's wiring deliberately lives elsewhere." >&2
        exit 1
    fi
fi

NOTE=""
if [ -n "$STORE" ] || [ -n "$PROJECT" ]; then
    NOTE="store=$STORE project=$PROJECT"
fi

# Rewrite without this key, then append -- so a reversal replaces the old row
# rather than shadowing it. A temp file in the same directory keeps the
# replacement atomic on the same filesystem.
TMP="$DECISIONS.tmp.$$"
{
    if [ -f "$DECISIONS" ]; then
        while IFS= read -r line || [ -n "$line" ]; do
            k="${line%%"$MC_TAB"*}"
            [ "$k" = "$KEY" ] && continue
            printf '%s\n' "$line"
        done < "$DECISIONS"
    else
        printf '# MemContinuum per-repo decisions -- written only by memcontinuum-decide.sh\n'
        printf '# key\tdecision\tdate\tnote\n'
    fi
    if [ "$ACTION" != "forget" ]; then
        printf '%s\t%s\t%s\t%s\n' "$KEY" "$ACTION" "$(date +%Y-%m-%d)" "$NOTE"
    fi
} > "$TMP" && mv "$TMP" "$DECISIONS" || { rm -f "$TMP"; echo "failed to write $DECISIONS" >&2; exit 1; }

case "$ACTION" in
    wired)    echo "recorded: $KEY uses MemContinuum${NOTE:+ ($NOTE)}" ;;
    declined) echo "recorded: $KEY declined -- this repo will not be asked again" ;;
    forget)   echo "recorded: $KEY forgotten -- it will be asked about again" ;;
esac
