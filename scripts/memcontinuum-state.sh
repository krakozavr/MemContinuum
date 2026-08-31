#!/usr/bin/env bash
# memcontinuum-state.sh [REPO_PATH] -- report one repository's MemContinuum
# state on stdout, for the `memcontinuum` skill to read before it says
# anything. Read-only: writes nothing, decides nothing.
#
# Prints `state=<value>` plus whatever context that state has:
#
#   state=no-config    MemContinuum was never bootstrapped on this machine
#   state=not-a-repo   the path is not inside a git working tree
#   state=wired        this repo's .claude settings reference the hooks
#   state=declined     a human recorded "no" for this repo's key
#   state=undecided    none of the above
#
# Same key rule as hooks/memcontinuum-detect.sh: origin remote URL when there
# is one, else the working tree's absolute path.

set -u

MEMCONTINUUM_HOME="${MEMCONTINUUM_HOME:-$HOME/.memcontinuum}"
CONFIG="$MEMCONTINUUM_HOME/config.sh"
DECISIONS="$MEMCONTINUUM_HOME/decisions.tsv"
TARGET="${1:-$PWD}"

# The five always-wired write-side hooks, matched on "command" lines only --
# same rule as the detector and decide.sh (rationale-only installs omit the
# two PreToolUse hooks, so those never count toward "wired").
HOOK_BASENAMES="ledger-post-edit.sh precompact-persist.sh sessionstart-remind.sh userprompt-remind.sh sessionend-stamp.sh"

if [ ! -f "$CONFIG" ]; then
    echo "state=no-config"
    echo "hint=run memcontinuum-setup.sh from the engine checkout"
    exit 0
fi
# shellcheck source=/dev/null
. "$CONFIG"
echo "engine=${MEMCONTINUUM_ENGINE:-}"
echo "python=${MEMCONTINUUM_PYTHON:-}"

if [ -f "$MEMCONTINUUM_HOME/no-ask" ]; then
    echo "global_ask=off"
else
    echo "global_ask=on"
fi

REPO="$(git -C "$TARGET" rev-parse --show-toplevel 2>/dev/null)"
if [ -z "$REPO" ]; then
    echo "state=not-a-repo"
    echo "path=$TARGET"
    exit 0
fi
echo "repo=$REPO"

REMOTE="$(git -C "$REPO" config --get remote.origin.url 2>/dev/null)"
if [ -n "$REMOTE" ]; then KEY="$REMOTE"; else KEY="$REPO"; fi
KEY="$(printf '%s' "$KEY" | tr '\t\n' '__')"
echo "key=$KEY"

for settings in "$REPO/.claude/settings.local.json" "$REPO/.claude/settings.json"; do
    [ -f "$settings" ] || continue
    for base in $HOOK_BASENAMES; do
        if grep '"command"' "$settings" 2>/dev/null | grep -qF "$base"; then
            echo "state=wired"
            echo "settings=$settings"
            # Surface the store/project the wiring actually names, so the
            # skill reports what is true rather than what it assumes.
            # scripts/repo-init.sh emits shlex-quoted values (MEMCONTINUUM_ROOT='/a b/c'),
            # hand-written wiring often doesn't -- try the quoted form first,
            # else fall back to the bare word.
            store="$(sed -n "s/.*MEMCONTINUUM_ROOT='\([^']*\)'.*/\1/p" "$settings" | head -1)"
            [ -n "$store" ] || store="$(sed -n 's/.*MEMCONTINUUM_ROOT=\([^ "'"'"']*\).*/\1/p' "$settings" | head -1)"
            project="$(sed -n "s/.*MEMCONTINUUM_PROJECT='\([^']*\)'.*/\1/p" "$settings" | head -1)"
            [ -n "$project" ] || project="$(sed -n 's/.*MEMCONTINUUM_PROJECT=\([^ "'"'"']*\).*/\1/p' "$settings" | head -1)"
            [ -n "$store" ] && echo "store=$store"
            [ -n "$project" ] && echo "project=$project"
            exit 0
        fi
    done
done

if [ -f "$DECISIONS" ]; then
    while IFS="$(printf '\t')" read -r k decision when rest; do
        case "$k" in \#*|"") continue ;; esac
        if [ "$k" = "$KEY" ]; then
            echo "state=$decision"
            echo "decided_at=$when"
            exit 0
        fi
    done < "$DECISIONS"
fi

echo "state=undecided"
