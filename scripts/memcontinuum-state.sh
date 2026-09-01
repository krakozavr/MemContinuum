#!/usr/bin/env bash
# memcontinuum-state.sh [REPO_PATH] -- report one repository's MemContinuum
# state on stdout, for the `memcontinuum` skill to read before it says
# anything. Read-only: writes nothing, decides nothing.
#
# Decision (the human's recorded answer) and wiring (what the repo's .claude
# settings actually contain right now) are SEPARATE facts (fix-round-4 F5) --
# a hand-edited settings file or an interrupted install can leave them
# disagreeing, and collapsing them into one line hid exactly that. Both are
# printed:
#
#   decision=wired      a human recorded "yes" for this repo's key
#   decision=declined    a human recorded "no" for this repo's key
#   decision=none        no row for this repo's key
#
#   wiring=full           the repo's .claude settings reference ALL FIVE
#                        always-wired write-side hooks
#   wiring=partial        SOME but not all five -- a broken or half-finished
#                        install; `missing=<basenames>` names what's absent
#   wiring=none          none of the five present
#
# Plus a backward-compatible `state=` line, since decision and wiring can
# disagree (declined-but-still-wired, wired-row-but-broken-wiring):
#
#   state=no-config      MemContinuum was never bootstrapped on this machine
#   state=not-a-repo     the path is not inside a git working tree
#   state=wired          decision=wired, OR (decision=none AND wiring=full)
#                        -- grandfathered installs that predate the registry
#   state=declined       decision=declined (regardless of current wiring --
#                        the recorded answer is authoritative)
#   state=partial-wired  decision=none AND wiring=partial -- the repair path
#   state=undecided       decision=none AND wiring=none
#
# Same key rule as hooks/memcontinuum-detect.sh and memcontinuum-decide.sh:
# origin remote URL when there is one, else the working tree's absolute path
# -- scripts/mc-registry-lib.sh is the one place that mapping is written.

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
# shellcheck source=./mc-registry-lib.sh
if ! . "$SCRIPT_DIR/mc-registry-lib.sh"; then
    echo "state=no-config"
    echo "hint=missing $SCRIPT_DIR/mc-registry-lib.sh -- incomplete checkout"
    exit 0
fi

mc_resolve_home
CONFIG="$MEMCONTINUUM_HOME/config.sh"
DECISIONS="$MEMCONTINUUM_HOME/decisions.tsv"
TARGET="${1:-$PWD}"

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

if ! mc_repo_key "$TARGET"; then
    echo "state=not-a-repo"
    echo "path=$TARGET"
    exit 0
fi
REPO="$MC_REPO"
KEY="$MC_REPO_KEY"
echo "repo=$REPO"
echo "key=$KEY"

mc_wiring_scan "$REPO/.claude/settings.local.json" "$REPO/.claude/settings.json"
echo "wiring=$MC_WIRING"
[ "$MC_WIRING" = "partial" ] && echo "missing=$MC_WIRING_MISSING"

# Decision lookup happens BEFORE store/project extraction (R6 fix, round 4):
# a registry row recorded by `decide.sh wired --store DIR --project NAME`
# carries store=/project= in its note column, and that -- not "whichever
# hook entry happens to appear first in the settings file" -- is the
# authoritative source when a repo's .claude wires MORE THAN ONE project
# (two-projects-one-claude-dir topology). Without this, state.sh for repo
# B (decided, project=beta) could report project A's store/project just
# because A's hook entry sorts first in the file.
DECISION="none"
DECISION_PROJECT=""
if mc_registry_lookup "$DECISIONS" "$KEY"; then
    DECISION="$MC_LOOKUP_DECISION"
    echo "decision=$DECISION"
    echo "decided_at=$MC_LOOKUP_WHEN"
    DECISION_PROJECT="$(printf '%s\n' "$MC_LOOKUP_NOTE" | sed -n 's/.*[ ]project=\([^ ]*\).*/\1/p')"
else
    echo "decision=none"
fi

# Store/project, pulled from ONE coherent hook entry rather than two
# independent whole-file sed passes -- the latter can pair one project's
# store with a DIFFERENT project's name when a repo's .claude carries more
# than one project's wiring (two-projects-one-claude-dir topology; the old
# `head -1` on each field independently had no such guarantee). When the
# registry row names a project (DECISION_PROJECT), prefer the hook entry
# that actually carries THAT project's marker over "the first match" --
# falling back to first-found only when there is no row, or the row's
# project has no matching hook entry (R6 fix, round 4).
if [ "$MC_WIRING" != "none" ]; then
    FOUND=0
    PROJECT_SOURCE=""
    if [ -n "$DECISION_PROJECT" ] && mc_wired_command_for_project "$DECISION_PROJECT" \
            "$REPO/.claude/settings.local.json" "$REPO/.claude/settings.json"; then
        FOUND=1
        PROJECT_SOURCE="registry"
    elif mc_first_wired_command \
            "$REPO/.claude/settings.local.json" "$REPO/.claude/settings.json"; then
        FOUND=1
        PROJECT_SOURCE="wiring"
    fi
    if [ "$FOUND" -eq 1 ]; then
        echo "settings=$MC_WIRED_SETTINGS_FILE"
        # scripts/repo-init.sh emits shlex-quoted values (MEMCONTINUUM_ROOT='/a b/c'),
        # hand-written wiring often doesn't -- try the quoted form first, else
        # fall back to the bare word.
        store="$(printf '%s\n' "$MC_WIRED_COMMAND" | sed -n "s/.*MEMCONTINUUM_ROOT='\([^']*\)'.*/\1/p")"
        [ -n "$store" ] || store="$(printf '%s\n' "$MC_WIRED_COMMAND" | sed -n 's/.*MEMCONTINUUM_ROOT=\([^ "'"'"']*\).*/\1/p')"
        project="$(printf '%s\n' "$MC_WIRED_COMMAND" | sed -n "s/.*MEMCONTINUUM_PROJECT='\([^']*\)'.*/\1/p")"
        [ -n "$project" ] || project="$(printf '%s\n' "$MC_WIRED_COMMAND" | sed -n 's/.*MEMCONTINUUM_PROJECT=\([^ "'"'"']*\).*/\1/p')"
        [ -n "$store" ] && echo "store=$store"
        [ -n "$project" ] && echo "project=$project"
        # Output keys stay stable (store=/project= unchanged); this extra
        # key just says which source picked the entry above -- "registry"
        # when the decision row's own project pinned it, "wiring" when it
        # was the first match (no row, or the row named no project).
        [ -n "$project" ] && echo "project_source=$PROJECT_SOURCE"
    fi
fi

case "$DECISION" in
    wired)    STATE="wired" ;;
    declined) STATE="declined" ;;
    *)
        case "$MC_WIRING" in
            full)    STATE="wired" ;;
            partial) STATE="partial-wired" ;;
            *)       STATE="undecided" ;;
        esac
        ;;
esac
echo "state=$STATE"
