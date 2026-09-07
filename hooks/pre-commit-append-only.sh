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
# Exit codes: 1 blocks the commit (an append-only violation was found --
# the errors on stderr, one hook.log line carrying rc=1); 0 lets it
# through, either because the check was clean (rc=0) or because something
# ABOUT RUNNING THE CHECK failed (skipped=<reason>: MEMCONTINUUM_ROOT
# unset, the invoking repo is not-the-store (Codex 1 -- this wrapper fired
# for a commit outside MEMCONTINUUM_ROOT, e.g. a shared core.hooksPath),
# an unborn HEAD, no python, or memlint itself erroring out with no
# recognizable summary line) -- fail-open for infrastructure, fail-closed
# only for a genuine history edit. `git commit --no-verify` bypasses this
# hook entirely, same as any git hook; the real guarantee against a
# rewritten history for a store other machines/CI also touch is a
# protected branch plus this same check run in CI, not this hook alone.

set -u
export PYTHONPATH=

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
MEMLINT="$SCRIPT_DIR/../memlint.py"

# shellcheck source=mc-watchdog.sh
source "${MC_WATCHDOG_LIB_PATH:-$SCRIPT_DIR/mc-watchdog.sh}" 2>/dev/null
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

OUT="$(PYTHONPATH= "$PY" "$MEMLINT" --against-ref HEAD --staged "$MEMCONTINUUM_ROOT" 2>&1)"
RC=$?

# memlint's own append-only summary line (its last line, on both a clean
# run and a refusal) carries "changed=N" -- only trust RC as a real
# append-only verdict when that line is actually present. Anything else
# (an unexpected engine crash, RC=2 from memlint's own git/ref resolution
# failure -- which the unborn-HEAD check above means should never reach
# here for THIS hook's own invocation, but memlint is still exercised
# directly by other callers -- or any other shape with no summary line at
# all) fails OPEN: an infrastructure problem must never read as "your
# commit rewrites history".
CHANGED=""
case "$OUT" in
    *"changed="*)
        TAIL="${OUT##*changed=}"
        CHANGED="${TAIL%% *}"
        ;;
esac
case "$CHANGED" in
    ''|*[!0-9]*) CHANGED="" ;;
esac

if [ "$RC" -eq 0 ] && [ -n "$CHANGED" ]; then
    log_line "rc=0 changed=$CHANGED project=$PROJECT"
    exit 0
fi
if [ "$RC" -eq 1 ] && [ -n "$CHANGED" ]; then
    echo "$OUT" >&2
    log_line "rc=1 changed=$CHANGED project=$PROJECT"
    exit 1
fi

# Anything else (RC=2, a crash, no recognizable summary line at all) is an
# ENGINE failure, not a history edit -- fail open.
log_line "skipped=engine-failure rc=$RC project=$PROJECT"
exit 0
