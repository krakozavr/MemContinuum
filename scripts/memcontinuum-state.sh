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

# --help must never be mistaken for a REPO path: reporting `path=--help
# state=not-a-repo` looks like a real answer. Handled before anything else so
# it works with no config, no registry lib, and no git.
case "${1:-}" in
    -h|--help)
        cat <<'USAGE'
usage: memcontinuum-state.sh [REPO_PATH]

Report one repository's MemContinuum state on stdout, for the `memcontinuum`
skill to read before it says anything. Read-only: writes nothing, decides
nothing. REPO_PATH defaults to the current directory.

Prints, as separate facts:

  decision=wired|declined|none   the human's recorded answer for this repo
  wiring=full|partial|none       what the repo's .claude settings reference
                                 right now (`missing=` names what is absent
                                 when partial)
  state=...                      a combined view: no-config, not-a-repo,
                                 wired, declined, partial-wired, undecided

Decision and wiring are printed separately because they can disagree -- a
hand-edited settings file or an interrupted install leaves them out of step,
and collapsing them into one line hides exactly that.

Recording an answer is a different command: memcontinuum-decide.sh.
USAGE
        exit 0
        ;;
esac

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

# Liveness metric hint (backlog SS2, INC-0103/INC-0105): one line naming the
# exact command that reports whether this repo's read/write sides are still
# alive. No python runs here -- this script stays python-free, per its own
# contract -- it just names the interpreter/engine/project the rest of this
# script already resolved. `python`/`engine` fall back to a python-free
# guess (the engine checkout this very script lives in, via $SCRIPT_DIR)
# when config.sh left MEMCONTINUUM_ENGINE empty, so the hint is never a
# broken "/memidx.py" with no engine root at all. `--store "$store"` is
# appended when this repo's wiring named a store (project= implies store=
# above): without it, `stats` can still report the read side and the
# write-side nudge count (the write-side FLAG no longer needs --store at
# all, round 2 -- it is driven by this project's own store-kind ledger
# appends; --store only adds a corroborating store-wide git commit count).
#
# Project precedence (round-2 Codex gate, item 13): the REGISTRY's own
# recorded project (DECISION_PROJECT, from a `decide.sh wired --project
# NAME` row) wins over whatever live wiring happens to resolve, and falls
# back to it only when there is no row. A decided-but-currently-unwired
# repo (hooks missing/broken -- exactly the state where a liveness check
# matters most) used to fall straight to the repo's own basename here,
# pointing the hint at a project name that was never the one actually
# recorded -- silently steering an operator to query the wrong bucket.
STATS_PYTHON="${MEMCONTINUUM_PYTHON:-python3}"
STATS_ENGINE="${MEMCONTINUUM_ENGINE:-$SCRIPT_DIR/..}"
STATS_PROJECT="${DECISION_PROJECT:-}"
[ -z "$STATS_PROJECT" ] && STATS_PROJECT="${project:-}"
[ -z "$STATS_PROJECT" ] && STATS_PROJECT="$(basename "$REPO" 2>/dev/null)"
[ -z "$STATS_PROJECT" ] && STATS_PROJECT="default"
# Single-quoted throughout (a real install's python/engine/store/project
# value can contain a space or, for the literal "(unknown)" bucket name,
# parentheses that a shell would otherwise treat specially) so the printed
# line is a directly copy-pasteable command, not just a human-readable
# summary.
STATS_CMD="'$STATS_PYTHON' '$STATS_ENGINE/memidx.py' stats --project '$STATS_PROJECT' --days 7"
[ -n "${store:-}" ] && STATS_CMD="$STATS_CMD --store '$store'"
echo "stats: $STATS_CMD"
