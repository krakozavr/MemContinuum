#!/usr/bin/env bash
# memcontinuum-decide.sh <wired|declined|never-ask|ask-again|forget> [--store DIR]
#                        [--project NAME] [--repo PATH] [--claude-dir DIR]
#
# Record a human's answer about one repository, so the SessionStart detector
# never asks again. Called by the `memcontinuum` skill AFTER a human has
# answered -- never by a hook, and never to guess.
#
#   wired      they said yes AND the hooks are actually wired -- refused when no
#              settings under the repo's .claude (or --claude-dir) references the
#              seven hook scripts, because a wired row silences the detector
#              forever whether or not the install ever succeeded
#   declined   they said no
#   forget     drop this repo's row, returning it to "undecided"
#   never-ask  machine-wide: stop asking in every repo
#   ask-again  undo never-ask
#
# The registry is a TSV at $MEMCONTINUUM_HOME/decisions.tsv:
#   key <TAB> decision <TAB> iso-date <TAB> note
# Key = origin remote URL when there is one, else the working tree path --
# remote-keyed so a repo that moves on disk keeps its decision.
#
# Known path-key staleness (accepted, reviewer finding 2026-08-31): a NEW repo
# created at a path whose previous occupant left a path-keyed row inherits
# that answer and is not asked -- the recycled-scratch-directory case. The fix
# is `forget` from inside that directory. Detecting recycling automatically
# would mean fingerprinting repo identity beyond the path, which this registry
# deliberately does not do.

set -u

MEMCONTINUUM_HOME="${MEMCONTINUUM_HOME:-$HOME/.memcontinuum}"
DECISIONS="$MEMCONTINUUM_HOME/decisions.tsv"
TAB="$(printf '\t')"

usage() {
    sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
    exit "${1:-1}"
}

[ $# -ge 1 ] || usage 1
ACTION="$1"; shift

STORE=""; PROJECT=""; REPO_ARG="$PWD"; CLAUDE_DIR_ARG=""
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

REPO="$(git -C "$REPO_ARG" rev-parse --show-toplevel 2>/dev/null)"
[ -n "$REPO" ] || { echo "not a git repository: $REPO_ARG" >&2; exit 1; }

# Recording `wired` is the one write that can lie: a wired row silences the
# detector forever, whether or not scripts/repo-init.sh ever succeeded. So verify the
# claim against the repo's own settings before recording it (same seven-
# basename rule the detector and installer use). --claude-dir points the
# check elsewhere for projects whose wiring deliberately lives outside the
# repo (a working-dir .claude beside a bare code checkout, for instance).
if [ "$ACTION" = "wired" ]; then
    CHECK_DIR="${CLAUDE_DIR_ARG:-$REPO/.claude}"
    # ALL five always-wired write-side hooks must appear on "command" lines
    # (any one basename anywhere in the file let a comment or one stale
    # fragment authorize the permanent record -- round-3 reviewer finding).
    # The two PreToolUse hooks are excluded: rationale-only installs omit
    # them legitimately.
    FOUND=1
    for base in ledger-post-edit.sh precompact-persist.sh sessionstart-remind.sh userprompt-remind.sh sessionend-stamp.sh; do
        base_ok=0
        for settings in "$CHECK_DIR/settings.local.json" "$CHECK_DIR/settings.json"; do
            [ -f "$settings" ] || continue
            if grep '"command"' "$settings" 2>/dev/null | grep -qF "$base"; then base_ok=1; break; fi
        done
        [ "$base_ok" -eq 1 ] || { FOUND=0; break; }
    done
    if [ "$FOUND" -eq 0 ]; then
        echo "REFUSED: no MemContinuum hook wiring found under $CHECK_DIR -- run the" >&2
        echo "installer first (the memcontinuum skill does this), or pass --claude-dir" >&2
        echo "if this repo's wiring deliberately lives elsewhere." >&2
        exit 1
    fi
fi
REMOTE="$(git -C "$REPO" config --get remote.origin.url 2>/dev/null)"
if [ -n "$REMOTE" ]; then KEY="$REMOTE"; else KEY="$REPO"; fi
# Same tab/newline sanitization as the detector -- all three key readers and
# this one writer must agree, or a sanitized key never matches its row.
KEY="$(printf '%s' "$KEY" | tr '\t\n' '__')"

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
            k="${line%%$TAB*}"
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
