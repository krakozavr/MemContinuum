#!/usr/bin/env bash
# git pre-commit hook for the STORE repo (memory/, not this tool repo):
# blocks a commit that edits, removes, or renames an already-recorded link
# (TOP-0122 L1 rule 3 / docs/SCHEMA.md section 7's append-only rule) --
# the honest way to change a ruling is a NEW link, never a rewrite of the
# old one. Runs `memlint.py --against-ref HEAD --staged ROOT`, which
# compares every topic file's links at HEAD against what is about to be
# committed (the INDEX, not the working tree -- `git commit` commits
# what's staged, not whatever else sits in the working tree).
#
# Symlink or copy this as .git/hooks/pre-commit inside the store repo --
# see install-hooks.md; scripts/repo-init.sh generates a wrapper here the
# same way it generates one for post-commit (see post-commit-reindex.sh).
#
# INC-0127: this hook ALSO runs the plain schema check (`memlint.py ROOT`,
# no --against-ref, no --code-root) and blocks on any ERROR from it
# (warnings pass) -- thirteen records were committed with no opening
# frontmatter fence and nothing at commit time ever said so, because this
# hook used to run only the append-only diff, never the schema pass. This
# reads the WORKING TREE, not the staged index (memlint.py's schema mode
# has no staged-aware form the way --against-ref --staged does) -- a real
# but narrow gap: a file staged-but-then-further-edited unstaged could in
# principle diverge from what schema-lint actually reads. --code-root is
# omitted deliberately: the store repo has no code checkout of its own at
# commit time, and marker/concept-path checks are not this hook's job.
#
# Deliberately UNGUARDED (docs/INTERNALS.md "The watchdog", Unguarded:
# list, alongside memcontinuum-detect.sh): the five write-side hooks,
# newfile-nudge.sh, pre-edit-chain.sh, and post-commit-reindex.sh wrap
# themselves in hooks/mc-watchdog.sh's wall-clock budget because failing
# OPEN on a timeout is exactly the right answer for a reminder or a
# reindex -- a slow one is a missed convenience, never worth blocking the
# user's tool over. This hook's whole point is the opposite: fail CLOSED
# on a history edit. A watchdog that kills a slow check and lets the
# commit through anyway would turn the exact slowness this check exists
# to catch into a bypass -- so no timeout wraps this file at all. It still
# sources mc-watchdog.sh below, but ONLY for the MEMCONTINUUM_HOME/
# config.sh pointer-chain resolution and MC_GUARD_PY it computes as a side
# effect (the same resolution post-commit-reindex.sh gets from it) -- the
# re-exec/guard block that file also defines is never invoked here.
#
# Env (same three names/defaults as post-commit-reindex.sh):
#   MEMCONTINUUM_ROOT     the store's markdown root to check. Required --
#                     missing it is an infrastructure failure, fails open
#                     (skipped=root-unset).
#   MEMCONTINUUM_PROJECT  project namespace, for the hook.log line only.
#                     Defaults to $(basename "$MEMCONTINUUM_ROOT"), else
#                     "default".
#   MEMCONTINUUM_HOME     passed through to memidx.py unchanged (memlint
#                     itself never reads it -- it exists here for the
#                     config.sh pointer chain below and for hook.log's own
#                     location). Defaults to ~/.memcontinuum.
#   MEMCONTINUUM_PYTHON   absolute path to the venv python. Falls back to
#                     $MEMCONTINUUM_HOME/config.sh (if it sets
#                     MEMCONTINUUM_PYTHON), then <engine>/.venv/bin/python.
#
# Exit codes: 1 blocks the commit (an append-only violation, OR a schema
# ERROR such as INC-0127's missing frontmatter fence -- either way the
# errors go to stderr, one hook.log line carrying rc=1 or schema-rc=1); 0
# lets it through, either because both checks were clean (rc=0, warnings
# from the schema pass do not block) or because something
# ABOUT RUNNING THE CHECK failed (skipped=<reason>: MEMCONTINUUM_ROOT
# unset, the invoking repo is not-the-store (Codex 1 -- this wrapper fired
# for a commit outside MEMCONTINUUM_ROOT, e.g. a shared core.hooksPath),
# an unborn HEAD, no python, or the schema pass erroring out with no
# recognizable summary line). TOP-0137 L32: when the guard CANNOT RUN the
# hook fails open; when the append-only run STARTED and crashed (RC=2, or
# any RC without its `changed=` marker -- an uncaught exception exits 1
# exactly like a real finding, so the check is trusted only together with
# its summary-line marker, never RC alone) the commit is REFUSED, the
# engine's output is printed, and the log line reads `rc=<n> engine-failure
# refused`. MC_STORE_LINT_ALLOW_ENGINE_FAILURE=1 overrides it for one commit
# (log: `skipped=engine-failure override=1`).
# `git commit --no-verify` bypasses this hook entirely, same as any git
# hook; the real guarantee against a rewritten or unfenced history for a
# store other machines/CI also touch is a protected branch plus this same
# check run in CI, not this hook alone.

set -u
# TOP-0137 L32: the override is read from the launching environment HERE, before
# any `source` (the watchdog library sources config.sh, whose assignments must
# not be able to enable it); the decision below uses only this private copy.
MC_PC_OVERRIDE_IN="${MC_STORE_LINT_ALLOW_ENGINE_FAILURE:-}"
export PYTHONPATH=

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
MEMLINT="$SCRIPT_DIR/../memlint.py"

# shellcheck source=mc-watchdog.sh
source "${MC_WATCHDOG_LIB_PATH:-$SCRIPT_DIR/mc-watchdog.sh}" 2>/dev/null
# The two memlint checks and their verdict parsing, shared with the
# post-commit re-lint (hooks/post-commit-reindex.sh).
# shellcheck source=mc-store-lint.sh
MC_NO_LINT_LIB=0
source "${MC_STORE_LINT_LIB_PATH:-$SCRIPT_DIR/mc-store-lint.sh}" 2>/dev/null || MC_NO_LINT_LIB=1
# Deliberately never invoking the re-exec/guard block mc-watchdog.sh also
# defines (see header above) -- sourcing it only for the
# MEMCONTINUUM_HOME/config.sh resolution and MC_GUARD_PY it computes as a
# side effect of being sourced.

MEMCONTINUUM_HOME="${MEMCONTINUUM_HOME:-$HOME/.memcontinuum}"
PY="${MC_GUARD_PY:-${MEMCONTINUUM_PYTHON:-$SCRIPT_DIR/../.venv/bin/python}}"
LOG="$MEMCONTINUUM_HOME/hook.log"
mkdir -p "$MEMCONTINUUM_HOME" 2>/dev/null

log_line() {
    # log_line MESSAGE -- one hook.log line, best-effort (never blocks the
    # commit over a logging failure).
    printf '%s pre-commit-append-only: %s\n' \
        "$(date -Iseconds 2>/dev/null || date)" "$1" >>"$LOG" 2>/dev/null || true
}

if [ "$MC_NO_LINT_LIB" -eq 1 ]; then
    # Infrastructure fails open: without the lint functions there is nothing
    # to judge a commit by, and a broken checkout must not block commits.
    log_line "skipped=no-lint-lib project=${MEMCONTINUUM_PROJECT:-$(basename "${MEMCONTINUUM_ROOT:-default}")}"
    exit 0
fi

if [ -z "${MEMCONTINUUM_ROOT:-}" ]; then
    _SKIP_PROJECT="${MEMCONTINUUM_PROJECT:-default}"
    log_line "skipped=root-unset project=$_SKIP_PROJECT"
    exit 0
fi

PROJECT="${MEMCONTINUUM_PROJECT:-$(basename "$MEMCONTINUUM_ROOT")}"

# Codex 1 (BLOCKING, fix wave 1 G1): this wrapper is generated once per
# store, but a shared or global core.hooksPath can still make git invoke
# the very same wrapper for an UNRELATED repository's commit (repo-init.sh
# now refuses to install into one, but an already-installed shared
# hooksPath, or a hand-copied wrapper, is not something this hook can
# assume away). Compare the PHYSICAL toplevel of the repo git is ACTUALLY
# committing in right now (never MEMCONTINUUM_ROOT itself, which may be
# stale or simply belong to a different store) against MEMCONTINUUM_
# ROOT's own physical path -- a mismatch means this hook fired for a repo
# that is not its store: skip, never block that repo's ordinary commit.
# `git rev-parse --show-toplevel` with no `-C` reads the invoking repo,
# since git runs hooks with cwd already at that repo's own working tree
# root. Both sides resolved with `cd ... && pwd -P` (portable physical
# resolution, bash 3.2-safe, matching the same technique used elsewhere
# in this codebase) rather than `realpath`/`readlink -f` (not on every
# macOS).
_INVOKING_TOPLEVEL="$(git rev-parse --show-toplevel 2>/dev/null)"
if [ -n "$_INVOKING_TOPLEVEL" ]; then
    _INVOKING_TOPLEVEL_PHYS="$(cd "$_INVOKING_TOPLEVEL" 2>/dev/null && pwd -P)"
    _ROOT_PHYS="$(cd "$MEMCONTINUUM_ROOT" 2>/dev/null && pwd -P)"
    if [ -z "$_ROOT_PHYS" ] || [ "$_INVOKING_TOPLEVEL_PHYS" != "$_ROOT_PHYS" ]; then
        log_line "skipped=not-the-store project=$PROJECT"
        exit 0
    fi
fi

# Cheapest skip first (no python needed): a brand-new repo with no commits
# yet has no HEAD to compare against -- there is no history to protect,
# and `memlint --against-ref HEAD` would just fail to resolve it.
if ! git -C "$MEMCONTINUUM_ROOT" rev-parse --verify -q HEAD >/dev/null 2>&1; then
    log_line "skipped=unborn-head project=$PROJECT"
    exit 0
fi

if [ ! -x "$PY" ]; then
    log_line "skipped=no-python project=$PROJECT"
    exit 0
fi

# INC-0127: the schema check, first -- a plain `memlint.py ROOT` run (no
# --against-ref, no --code-root). Re-gate finding (MAJOR): Python exits 1
# on ANY uncaught exception, not only "at least one ERROR was printed" --
# a $PY that CAN be executed but fails to even IMPORT memlint (a venv
# missing PyYAML, reproduced with `python3 -m venv --without-pip` and no
# `pip install pyyaml`) prints a traceback and exits 1, indistinguishable
# from a real schema ERROR by RC alone. Schema mode always prints one of
# two summary lines on its LAST line of real output -- `memlint: N
# error(s), M warning(s)` or `memlint: clean (M warning(s))` -- so, exactly
# like the append-only branch below trusting `changed=` over RC alone,
# SCHEMA_RC=1 blocks the commit only when that marker is actually present;
# a 1 with no marker (a crash) is logged and falls through fail-open, the
# same as any other infrastructure problem this hook already tolerates.
mc_store_lint_schema "$PY" "$MEMLINT" "$MEMCONTINUUM_ROOT"
SCHEMA_NOTE="schema=ok"
case "$MC_LINT_SCHEMA" in
    fail)
        echo "$MC_LINT_SCHEMA_OUT" >&2
        log_line "schema-rc=1 project=$PROJECT"
        exit 1
        ;;
    engine-failure) SCHEMA_NOTE="schema=skipped-engine-failure" ;;
esac
# NIT (re-gate): the schema outcome above, when it does NOT block, folds
# into the ONE log line this invocation ends with below (the append-only
# verdict, or the engine-failure fallback) rather than writing its own
# separate line and then continuing anyway -- a line that says "skipped"
# while the hook keeps working, followed by a second line, previously
# disagreed with what actually happened.

# The verdict parsing (marker trust: `changed=` must be present, else the run
# is classified as an engine failure, which refuses the commit below) lives in
# hooks/mc-store-lint.sh.
mc_store_lint_append_only "$PY" "$MEMLINT" "$MEMCONTINUUM_ROOT" HEAD
case "$MC_LINT_APPEND" in
    ok)
        log_line "rc=0 changed=$MC_LINT_CHANGED notes=$MC_LINT_NOTES $SCHEMA_NOTE project=$PROJECT"
        exit 0
        ;;
    fail)
        echo "$MC_LINT_APPEND_OUT" >&2
        log_line "rc=1 changed=$MC_LINT_CHANGED notes=$MC_LINT_NOTES $SCHEMA_NOTE project=$PROJECT"
        exit 1
        ;;
esac

# Anything else (RC=2, a crash, no recognizable summary line at all) means
# the engine RAN and failed to judge the commit. TOP-0137 L32: that fails
# CLOSED -- a crash inside the guard must not let a history edit through
# (PR #60's gate: a TypeError in the guard was classified as an engine
# failure and the edit passed). The cases where the guard CANNOT RUN (unborn
# HEAD, no python, cwd not the store, no lint lib, root unset) were already
# skipped above and stay fail-open. An operator who must commit anyway sets
# MC_STORE_LINT_ALLOW_ENGINE_FAILURE=1 for that one commit.
if [ "$MC_PC_OVERRIDE_IN" = "1" ]; then
    log_line "skipped=engine-failure override=1 rc=$MC_LINT_APPEND_RC $SCHEMA_NOTE project=$PROJECT"
    exit 0
fi
echo "$MC_LINT_APPEND_OUT" >&2
echo "memcontinuum: the store's append-only check crashed (rc=$MC_LINT_APPEND_RC) and could not judge this commit; refusing it. To commit anyway: MC_STORE_LINT_ALLOW_ENGINE_FAILURE=1 git commit ..." >&2
log_line "rc=$MC_LINT_APPEND_RC engine-failure refused $SCHEMA_NOTE project=$PROJECT"
exit 1
