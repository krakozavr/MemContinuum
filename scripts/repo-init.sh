#!/usr/bin/env bash
# repo-init.sh -- MemContinuum per-repository initializer (formerly install.sh).
#
# Creates a Rationale-store markdown tree at --store, wires the PreToolUse
# retrieval hook and the five write-side reminder hooks into a project's
# Claude Code settings, installs the memory-search skill, sets up the
# store's git post-commit reindex hook, and runs an initial reindex + lint.
#
# Usage:
#   repo-init.sh --project NAME [--store DIR] [--code-root DIR ...]
#              [--claude-dir DIR] [--python PATH] [--bootstrap-venv [DIR]]
#              [--langs LIST] [--never-ext LIST] [--non-interactive]
#              [--dry-run] [--force]
#
# An explicit --store REQUIRES an explicit --claude-dir alongside it (fix-
# round-4 F3) -- omitting --store lets --claude-dir default from the repo the
# cwd is in instead.
#
# See README.md "## Installing into a new project" for the full contract.
#
# Nothing in this script is specific to any one project: every path is a
# parameter (or a documented default), matching the project-agnostic
# contract the rest of this repo (memidx.py, memlint.py, hooks/*.sh)
# already follows.

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
# This script lives in scripts/ (it is the SKILL's tool, not the user's entry
# point -- that is memcontinuum-setup.sh at the repo root); everything it
# consumes lives one level up.
ENGINE_ROOT="$(cd "$SCRIPT_DIR/.." >/dev/null 2>&1 && pwd)"
HOOKS_DIR="$ENGINE_ROOT/hooks"
TEMPLATES_DIR="$ENGINE_ROOT/templates"
MEMIDX="$ENGINE_ROOT/memidx.py"
MEMLINT="$ENGINE_ROOT/memlint.py"
SKILL_SRC="$ENGINE_ROOT/skills/memory-search/SKILL.md"

# Sourced up here rather than inside the record-the-decision block at the end:
# this script needs its store predicates (mc_is_git_repo/mc_is_marked_store)
# from the very first validation onwards, and having ONE definition of "this
# path still holds a store" shared with scripts/memcontinuum-update.sh is the
# point -- the re-render walk and the installer must agree, by construction,
# on what a store is. The library only defines functions when sourced.
# shellcheck source=./mc-registry-lib.sh
. "$SCRIPT_DIR/mc-registry-lib.sh" || { echo "ERROR: missing $SCRIPT_DIR/mc-registry-lib.sh -- incomplete checkout" >&2; exit 1; }

# The bash running THIS script, for the one script it shells out to (see
# scripts/memcontinuum-update.sh for the same note): `bash` off PATH would
# hop interpreters mid-install, which is what kept the bash 3.2 harness from
# reaching any of these scripts.
MC_BASH_BIN="${BASH:-bash}"

OUR_HOOK_SCRIPTS="pre-edit-chain.sh newfile-nudge.sh ledger-post-edit.sh precompact-persist.sh sessionstart-remind.sh userprompt-remind.sh sessionend-stamp.sh"

# The stamp that goes onto every hook line, the rules file, and the installed
# skill copy, so a later `memcontinuum-update.sh` can tell a current rendered
# artifact from a stale one. It is a fingerprint of this checkout's RENDER
# INPUTS -- templates, this installer, the settings merge, the copied skills,
# the machine-layer installer -- not the checkout's HEAD commit: pulling a fix
# to a hook SCRIPT changes nothing that was rendered here (hook lines run
# those scripts by absolute path), so it must not make every wired repo look
# stale. See mc_render_fingerprint in scripts/mc-registry-lib.sh.
#
# Derived from THIS checkout, never the cwd -- ENGINE_ROOT is always where
# this script itself lives, so a `--code-root`-only invocation from an
# unrelated repo still stamps correctly. The literal "unknown" when it cannot
# be computed at all (no sha256 tool, an incomplete checkout); an
# absent/unknown stamp reads as "re-render to find out" downstream, never as
# an error here.
mc_render_fingerprint repo "$ENGINE_ROOT" || :
RENDERED_SHA="$MC_RENDER_FINGERPRINT"

PROJECT=""
STORE=""
STORE_GIVEN=0
CLAUDE_DIR=""
PYTHON_BIN=""
PYTHON_BIN_EXPLICIT=0
DRY_RUN=0
FORCE=0
BOOTSTRAP_VENV=0
BOOTSTRAP_VENV_DIR=""
LANGS_FLAG=""
NEVER_EXT_FLAG=""
NON_INTERACTIVE=0
RECORD_DECISION=0
ADOPT_ONLY=0
declare -a CODE_ROOTS=()

usage() {
    cat <<'USAGE'
Usage: repo-init.sh --project NAME [--store DIR] [--code-root DIR ...]
                   [--claude-dir DIR] [--python PATH]
                   [--bootstrap-venv [DIR]] [--langs LIST] [--never-ext LIST]
                   [--non-interactive] [--adopt-only]
                   [--dry-run] [--force]

  --project NAME     project namespace (used for --project everywhere, and
                      as the index db filename <NAME>.sqlite). Must match
                      [A-Za-z0-9._-]+ (it is embedded as an identity marker
                      in every hook command line). Required.
  --store DIR        the markdown store root to create/wire. Optional: the
                      default is the conventional marked name --
                      "<repo>-MemContinuum-Store" beside the git repo the
                      cwd is in, else "$PWD/MemContinuum-Store". Never a
                      generic "memory/" (collides with other memory
                      systems) and never bare "MemContinuum" (reads as the
                      tool itself). An existing git repo at DIR with none of
                      this tool's markers (no topics/incidents/concepts dir,
                      no README mentioning MemContinuum) is refused, not
                      silently adopted -- protects against a mistyped
                      --store landing store dirs in an unrelated repo.
  --code-root DIR     a code checkout the PreToolUse hook should watch for
                      Edit/Write and the write-side hooks should scope
                      ledger entries to. Repeatable. Optional -- omit for a
                      store with no associated code checkout (a
                      rationale-only install: neither PreToolUse hook is
                      wired, so nothing is retrieved at edit time).
  --claude-dir DIR    where to merge hook wiring and install the skill.
                      Defaults to <dirname of --store>/.claude ONLY when
                      --store was also omitted (the store then defaults
                      beside the repo the cwd is in, a reliable signal). An
                      EXPLICIT --store with no --claude-dir is a hard error
                      -- an explicit --store may be run from any cwd, so the
                      cwd is not a reliable signal for where hooks belong;
                      pass --claude-dir DIR alongside it.
  --python PATH       absolute path to the venv python to use. Overrides
                      every other resolution below. Optional.
  --bootstrap-venv [DIR]
                      create a venv (prefer `uv venv`+`uv pip` when uv is on
                      PATH, else `python3 -m venv` + pip), install
                      requirements.txt into it, and use it as the python for
                      the rest of this install (unless --python was also
                      given). DIR defaults to <this checkout>/.venv. Runs
                      immediately -- even under --dry-run -- since later
                      steps need a real python to resolve paths with.
  --langs LIST        comma-separated language set to enable for --code-root
                      indexing (e.g. "python,swift"), bypassing the census
                      consent dialogue. Wins over --non-interactive. Each
                      name must be a language this engine version's table
                      knows (see `memidx.py code-census`). Ignored (with no
                      effect) when no --code-root is given.
  --never-ext LIST    comma-separated extensions (".cs" or "cs") the new-file
                      nudge must never mention again for this wiring -- the
                      non-interactive form of the consent dialogue's
                      "never for one extension" answer. Does NOT change which
                      languages are enabled: it only renders
                      MEMCONTINUUM_NEVER_EXTS onto the nudge hook line.
                      Ignored (with no effect) when no --code-root is given.
  --non-interactive   with no --langs, skip the consent dialogue entirely --
                      language-less wiring (no --lang on the initial
                      code-reindex, which is skipped; the newfile-nudge hook
                      line still gets MEMCONTINUUM_KNOWN_EXTS, and an
                      EXPLICITLY EMPTY MEMCONTINUUM_LANG_EXTS='' -- rendered,
                      never omitted: a set-but-empty value matches nothing,
                      which is distinct from a hook line rendered without the
                      variable at all). For scripted/CI runs.
  --record-decision   after a successful (non-dry-run) install, record
                      "wired" in the decision registry (same as running
                      memcontinuum-decide.sh wired by hand), so the
                      SessionStart detector never asks about this repo
                      again. OFF by default: recording a human's consent is
                      the memcontinuum skill's job, not this installer's --
                      pass this only from a driven flow where a human has
                      already said yes. Refuses to record (a warning, never
                      a failure of the install itself) when --claude-dir is
                      not inside a git working tree, same as
                      memcontinuum-decide.sh's own requirement. When a row
                      already exists for this repo, its recorded
                      claude-dirs/code-roots are UNIONED with this install's
                      (never dropped) -- installing a second claude-dir for
                      the same project must not erase the first from the
                      registry.
  --adopt-only        wire an EXISTING store; never create one. Unless
                      --store is a directory that is already a git working
                      tree carrying this tool's markers (a topics/incidents/
                      concepts directory, or a README naming MemContinuum),
                      this run is refused with "store-missing" before it
                      writes anything at all -- no store tree, no git init,
                      no hook wiring. --dry-run is refused the same way: a
                      preview of an install that must never happen would only
                      mislead. Pass this whenever the store is supposed to
                      exist already and a fresh one would be wrong -- the
                      re-render command (scripts/memcontinuum-update.sh)
                      always does, so a registry row naming a store that was
                      renamed or deleted can never quietly get a new, empty
                      store seeded at the old path.
  --dry-run           print everything this script would do; write nothing
                      (except --bootstrap-venv's venv, see above).
  --force             allow --store to sit inside another git repo's working
                      tree (normally refused). The check is by LOCATION, not
                      by tracked content: nothing at --store need exist yet.
                      It never overrides the separate refusal of an existing
                      git repo that is not a MemContinuum store.
  -h, --help          this text.

Without --python or --bootstrap-venv, the python to run memidx.py/memlint.py
with is resolved in this order: $MEMCONTINUUM_PYTHON (env) -> the
MEMCONTINUUM_PYTHON recorded in $MEMCONTINUUM_HOME/config.sh (written by
memcontinuum-setup.sh; one pointer config.sh followed) -> <this
checkout>/.venv/bin/python -> a clear error naming --bootstrap-venv.
USAGE
}

fail() {
    echo "ERROR: $1" >&2
    exit "${2:-1}"
}

step() {
    # step MESSAGE -- prefixes with [dry-run] when appropriate.
    if [ "$DRY_RUN" -eq 1 ]; then
        echo "[dry-run] $1"
    else
        echo "$1"
    fi
}

abspath() {
    "$PYTHON_BIN" -c 'import os, sys; print(os.path.abspath(sys.argv[1]))' "$1"
}

nearest_existing_ancestor() {
    local d="$1"
    while [ ! -d "$d" ]; do
        local parent
        parent="$(dirname "$d")"
        [ "$parent" = "$d" ] && break
        d="$parent"
    done
    printf '%s' "$d"
}

# is_git_repo DIR -- see mc_is_git_repo in scripts/mc-registry-lib.sh (the one
# definition, shared with the re-render walk). Kept as a local name because
# this script reads better with it, not as a second implementation.
is_git_repo() { mc_is_git_repo "$@"; }

# git_hooks_dir_for DIR -- prints the hooks directory git actually consults
# for commits made in DIR (absolute). `rev-parse --git-path hooks` is the
# only correct resolver: a root repo's own .git/hooks, but for a linked
# worktree the SHARED repo's .git/hooks -- git never runs hooks from
# .git/worktrees/<name>/hooks (verified by live probe, git 2.43, regate
# round 2) -- and it honors core.hooksPath when someone has set one.
# Never assume "$DIR/.git/hooks" -- for a worktree .git is a file, not a
# directory. Returns 1 with nothing printed if DIR is not a git working
# tree.
git_hooks_dir_for() {
    local d
    d="$(git -C "$1" rev-parse --git-path hooks 2>/dev/null)" || return 1
    case "$d" in
        /*) printf '%s' "$d" ;;
        *)  printf '%s' "$1/$d" ;;
    esac
}

# resolve_python -- prints an absolute python path on stdout and returns 0,
# or returns 1 with nothing printed. Order (README.md "Requirements",
# hooks/memlib.sh): $MEMCONTINUUM_PYTHON env, then the MEMCONTINUUM_PYTHON
# recorded in $MEMCONTINUUM_HOME/config.sh (following one pointer config.sh
# to a custom HOME, exactly like memlib.sh), then <this checkout>/
# .venv/bin/python. Never consults --python (the caller checks that first,
# since it's an explicit override, not a fallback).
#
# The config.sh step was MISSING here until 2026-09-01 even though the
# README documented it and memcontinuum-setup.sh writes config.sh precisely
# so no invocation needs MEMCONTINUUM_PYTHON by hand -- a machine set up by
# memcontinuum-setup.sh with a venv elsewhere (no <engine>/.venv) died with
# "no python found" on every repo-init run. Sourced in a subshell so a
# config.sh's other assignments (MEMCONTINUUM_HOME, MEMCONTINUUM_ENGINE)
# never leak into this script's environment.
resolve_python() {
    if [ -n "${MEMCONTINUUM_PYTHON:-}" ]; then
        printf '%s' "$MEMCONTINUUM_PYTHON"
        return 0
    fi
    local cfg_python
    cfg_python="$(
        home="${MEMCONTINUUM_HOME:-$HOME/.memcontinuum}"
        cfg1="$home/config.sh"
        # stdout silenced too, not just stderr (Codex, 2026-09-01): the
        # command substitution captures ALL stdout, so a config.sh that
        # prints anything would corrupt cfg_python into "chatter/path" and
        # block a valid engine-venv fallback.
        if [ -f "$cfg1" ]; then
            # shellcheck source=/dev/null
            . "$cfg1" >/dev/null 2>&1 || true
        fi
        home="${MEMCONTINUUM_HOME:-$HOME/.memcontinuum}"
        if [ "$home/config.sh" != "$cfg1" ] && [ -f "$home/config.sh" ]; then
            # shellcheck source=/dev/null
            . "$home/config.sh" >/dev/null 2>&1 || true
        fi
        printf '%s' "${MEMCONTINUUM_PYTHON:-}"
    )"
    if [ -n "$cfg_python" ]; then
        printf '%s' "$cfg_python"
        return 0
    fi
    if [ -x "$ENGINE_ROOT/.venv/bin/python" ]; then
        printf '%s' "$ENGINE_ROOT/.venv/bin/python"
        return 0
    fi
    return 1
}

# bootstrap_venv DIR -- creates a venv at DIR and installs requirements.txt
# into it. Prefers `uv venv` + `uv pip install` when `uv` is on PATH (fast,
# no separate pip bootstrap needed); falls back to `python3 -m venv` +
# `DIR/bin/python -m pip install`. Returns non-zero on any failure; never
# touches PYTHON_BIN itself (the caller decides whether to adopt the result).
bootstrap_venv() {
    local dir="$1"
    local req="$ENGINE_ROOT/requirements.txt"
    if [ ! -f "$req" ]; then
        echo "ERROR: requirements.txt not found next to scripts/repo-init.sh: $req" >&2
        return 1
    fi
    mkdir -p "$(dirname "$dir")" 2>/dev/null || true
    if command -v uv >/dev/null 2>&1; then
        step "uv venv $dir"
        uv venv "$dir" || return 1
        step "uv pip install -r $req (into $dir)"
        uv pip install --python "$dir/bin/python" -r "$req" || return 1
    else
        command -v python3 >/dev/null 2>&1 || { echo "ERROR: neither uv nor python3 found on PATH" >&2; return 1; }
        step "python3 -m venv $dir"
        python3 -m venv "$dir" || return 1
        step "$dir/bin/python -m pip install -r $req"
        "$dir/bin/python" -m pip install -r "$req" || return 1
    fi
    return 0
}

# mc_union_semi A B -- prints A's ';'-joined list with every item from B's
# not already in A appended, deduplicated, order preserved (A's own order
# first). Declared/assigned on separate statements deliberately -- a single
# `local a="$1" out="$a"` reads "$a" as still-unset under `set -u` in this
# bash (reproduced; see scripts/memcontinuum-update.sh's add_semi for the
# same fix with the same reasoning). Used only by --record-decision below,
# to union THIS install's claude-dir/code-roots/langs/never-exts into
# whatever a pre-existing registry row already recorded, rather than
# replacing it (a second claude-dir installed for the same project must not
# erase the first from the registry).
mc_union_semi() {
    local a b out tok union_ifs
    a="$1"; b="$2"; out="$a"
    union_ifs="$IFS"
    IFS=';'
    for tok in $b; do
        IFS="$union_ifs"
        [ -n "$tok" ] || continue
        case ";$out;" in
            *";$tok;"*) ;;
            *) out="${out:+$out;}$tok" ;;
        esac
        IFS=';'
    done
    IFS="$union_ifs"
    printf '%s' "$out"
}

# --- arg parsing -----------------------------------------------------------

# A two-argument option with no value must ERROR, not loop: a failed
# `shift 2` leaves the argument in place and spins forever (reviewer
# finding, reproduced against all three entry points).
mc_need_value() { [ $# -ge 2 ] || { echo "missing value for $1" >&2; exit 2; }; }
while [ $# -gt 0 ]; do
    case "$1" in
        --project) mc_need_value "$@"; PROJECT="$2"; shift 2 ;;
        --store) mc_need_value "$@"; STORE="$2"; STORE_GIVEN=1; shift 2 ;;
        --code-root) mc_need_value "$@"; CODE_ROOTS+=("$2"); shift 2 ;;
        --claude-dir) mc_need_value "$@"; CLAUDE_DIR="$2"; shift 2 ;;
        --python) mc_need_value "$@"; PYTHON_BIN="$2"; PYTHON_BIN_EXPLICIT=1; shift 2 ;;
        --bootstrap-venv)
            BOOTSTRAP_VENV=1
            shift
            if [ $# -gt 0 ]; then
                case "$1" in
                    -*) ;;
                    *) BOOTSTRAP_VENV_DIR="$1"; shift ;;
                esac
            fi
            ;;
        --langs) mc_need_value "$@"; LANGS_FLAG="$2"; shift 2 ;;
        --never-ext) mc_need_value "$@"; NEVER_EXT_FLAG="$2"; shift 2 ;;
        --non-interactive) NON_INTERACTIVE=1; shift ;;
        --record-decision) RECORD_DECISION=1; shift ;;
        --adopt-only) ADOPT_ONLY=1; shift ;;
        --dry-run) DRY_RUN=1; shift ;;
        --force) FORCE=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; fail "unknown argument: $1" 2 ;;
    esac
done

[ -n "$PROJECT" ] || { usage >&2; fail "--project is required" 2; }
# Default store location (owner convention, 2026-08-31): the folder is named
# MemContinuum-Store -- marked as this tool's, never a generic "memory" that
# another memory system could collide with or a bare "MemContinuum" that reads
# as the tool itself. Where it lands depends on what the cwd is:
#   cwd inside a git repo   -> a SIBLING of that repo, "<name>-MemContinuum-Store"
#                              (never inside -- a store must not be absorbed
#                              into a code repo's history; same rule the
#                              inside-a-repo refusal below enforces)
#   cwd not in any git repo -> "$PWD/MemContinuum-Store" (a working FOLDER,
#                              like a docs dir, hosts its store directly)
# An explicit --store always wins; the default is printed so nothing lands
# anywhere silently.
if [ -z "$STORE" ]; then
    CWD_TOPLEVEL="$(git rev-parse --show-toplevel 2>/dev/null || true)"
    if [ -n "$CWD_TOPLEVEL" ]; then
        STORE="$(dirname "$CWD_TOPLEVEL")/$(basename "$CWD_TOPLEVEL")-MemContinuum-Store"
        # A defaulted SIBLING store must not drag --claude-dir's own default
        # (<dirname of store>/.claude) up to the parent directory -- the hooks
        # belong to the repo being initialized, so they default into that
        # repo's .claude. Deliberately scoped to the defaulted-store flow
        # only: with an EXPLICIT --store, the cwd is no signal at all (the
        # command may be run from anywhere -- a test harness, a script, an
        # unrelated checkout -- to set up paths elsewhere; round-3's fix
        # attempt keyed on cwd unconditionally and wired a test run's hooks
        # into the engine repo's own .claude). The skill's documented flow is
        # cd-into-the-repo with NO --store, which lands here.
        [ -n "$CLAUDE_DIR" ] || CLAUDE_DIR="$CWD_TOPLEVEL/.claude"
    else
        STORE="$PWD/MemContinuum-Store"
    fi
    echo "note: no --store given -- defaulting to $STORE"
    [ -n "$CLAUDE_DIR" ] && echo "note: hooks will merge into $CLAUDE_DIR"
fi

# fix-round-4 F8 addendum: canonical identity, not just "no /" -- PROJECT is
# embedded, unquoted, as a MEMCONTINUUM_PROJECT=<name> identity marker in
# every hook command line (is_ours()'s project-aware match above depends on
# it appearing as one bare shell word), so anything a shell would split or
# glob-expand there is refused outright rather than silently mismatching.
case "$PROJECT" in
    ""|*[!A-Za-z0-9._-]*) fail "--project must match [A-Za-z0-9._-]+ (got: $PROJECT)" 2 ;;
esac

# fix-round-4 R5: this refusal needs no python and must be checked before
# any python/venv work below (--bootstrap-venv, resolve_python) -- it used
# to run only after python resolution, so an invalid invocation (explicit
# --store, no --claude-dir, no python available) would bootstrap a venv or
# die naming the WRONG problem ("no python found") before ever reaching
# the actual one. See where CLAUDE_DIR is actually ASSIGNED below (after
# python resolves -- that step needs abspath(), which needs $PYTHON_BIN)
# for the full rationale.
if [ "$STORE_GIVEN" -eq 1 ] && [ -z "$CLAUDE_DIR" ]; then
    fail "--store was given explicitly with no --claude-dir -- refusing to guess which .claude the hooks belong in (an explicit --store may be run from any cwd, so the cwd is not a reliable signal). Pass --claude-dir DIR." 2
fi

# --- python resolution --------------------------------------------------
#
# --bootstrap-venv runs immediately (not gated on --dry-run): it is a tooling
# precondition, like the "does this python even run" check right below, not
# part of the install plan being previewed -- and every abspath() call from
# here on needs a real python regardless of --dry-run (unchanged from before
# this feature existed).

if [ "$BOOTSTRAP_VENV" -eq 1 ]; then
    VENV_DIR="${BOOTSTRAP_VENV_DIR:-$ENGINE_ROOT/.venv}"
    bootstrap_venv "$VENV_DIR" || fail "bootstrap-venv failed (see output above)" 3
    if [ "$PYTHON_BIN_EXPLICIT" -eq 0 ]; then
        PYTHON_BIN="$VENV_DIR/bin/python"
    fi
fi

if [ -z "$PYTHON_BIN" ]; then
    if RESOLVED_PYTHON="$(resolve_python)"; then
        PYTHON_BIN="$RESOLVED_PYTHON"
    else
        fail "no python found (checked \$MEMCONTINUUM_PYTHON, \$MEMCONTINUUM_HOME/config.sh, $ENGINE_ROOT/.venv/bin/python): set \$MEMCONTINUUM_PYTHON, pass --python PATH, run memcontinuum-setup.sh, or run '$0 --bootstrap-venv [DIR]' to create a venv (see README.md's Requirements section)" 3
    fi
fi

# --- validation --------------------------------------------------------

[ -x "$PYTHON_BIN" ] || fail "python venv not found or not executable: $PYTHON_BIN (missing python venv)" 3
"$PYTHON_BIN" -c 'pass' >/dev/null 2>&1 || fail "python venv at $PYTHON_BIN does not run (missing python venv)" 3
[ -f "$MEMIDX" ] || fail "memidx.py not found next to scripts/repo-init.sh: $MEMIDX" 3
[ -f "$MEMLINT" ] || fail "memlint.py not found next to scripts/repo-init.sh: $MEMLINT" 3

STORE="$(abspath "$STORE")"
if [ -z "$CLAUDE_DIR" ]; then
    # fix-round-4 F3 (final ruling): an EXPLICIT --store with no --claude-dir
    # is refused, not guessed -- already checked (and failed, if applicable)
    # before any python/venv work above (R5). dirname(store)/.claude is
    # right only when the store sits beside the repo being initialized --
    # true for the defaulted sibling-store case above, which is the only
    # way to reach this branch with CLAUDE_DIR still unset (an explicit
    # --store with no --claude-dir already exited above). A cwd-based
    # fallback for an explicit --store would reproduce the exact bug this
    # fixes with a log line: even a git cwd can be the wrong repo (the
    # command may be run from a test harness, a script, or any unrelated
    # checkout). The no-store branch above is untouched -- it already
    # derives CLAUDE_DIR from the repo the cwd is actually in, which IS a
    # reliable signal there.
    CLAUDE_DIR="$(dirname "$STORE")/.claude"
else
    CLAUDE_DIR="$(abspath "$CLAUDE_DIR")"
fi

declare -a CODE_ROOTS_ABS=()
for cr in "${CODE_ROOTS[@]:-}"; do
    [ -z "$cr" ] && continue
    CODE_ROOTS_ABS+=("$(abspath "$cr")")
done

if [ -n "$LANGS_FLAG" ] && [ "${#CODE_ROOTS_ABS[@]}" -eq 0 ]; then
    echo "note: --langs $LANGS_FLAG given with no --code-root -- nothing to wire it into, ignoring"
fi
if [ -n "$NEVER_EXT_FLAG" ] && [ "${#CODE_ROOTS_ABS[@]}" -eq 0 ]; then
    echo "note: --never-ext $NEVER_EXT_FLAG given with no --code-root -- there is no new-file reminder to silence, ignoring"
fi

# --adopt-only: this run may WIRE an existing store, never CREATE one.
# Checked first, before every other store check below, for two reasons: it is
# the strictest of them (an --adopt-only run that gets past here has a real
# store, so none of the seeding paths can fire at all), and it must report
# ITS OWN reason. Ordered after the nesting check, a vanished store whose
# nearest existing ancestor happens to sit inside some other git repo would
# die with "your --store is inside another repo, pass --force" -- advice that
# is both wrong and dangerous here, since --force would then seed a store
# where one must never be created.
#
# The whole point is the re-render walk (scripts/memcontinuum-update.sh),
# which always passes this: a registry row naming a store that has since been
# renamed or deleted must NOT cause a fresh, empty store to appear at the old
# path and be wired up as if nothing had happened. --dry-run refuses too.
if [ "$ADOPT_ONLY" -eq 1 ] && ! mc_is_marked_store "$STORE"; then
    fail "store-missing: --adopt-only was given, but $STORE is not an existing MemContinuum store (it must be a git working tree carrying this tool's markers -- a topics/incidents/concepts directory, or a README naming MemContinuum). Nothing was written. If the store was renamed or moved, point --store at where it lives now; if it was deleted, restore it from its own git history. This mode never creates a store." 15
fi

# store dir must not already live inside a DIFFERENT git repo's working
# tree, unless it is already its own repo (the normal re-run case, a
# linked worktree included -- R8) or --force was given.
if ! is_git_repo "$STORE" && [ "$FORCE" -eq 0 ]; then
    ANCESTOR="$(nearest_existing_ancestor "$STORE")"
    if OUTER_TOPLEVEL="$(git -C "$ANCESTOR" rev-parse --show-toplevel 2>/dev/null)"; then
        fail "--store $STORE is inside an existing git repo's working tree ($OUTER_TOPLEVEL) -- pass --force to install anyway, or pick a --store outside it" 4
    fi
fi

# --- classify: fresh seed vs adopt vs refuse (fix-round-4 F10) -------------
#
# Must run here, BEFORE any mutation below (step 1 is the first one that
# writes anything): memlint's duplicate-id promotion used to fire (exit 8)
# AFTER wiring was already written, when adopting a pre-existing store that
# happened to carry legacy duplicate ids -- a "successful" install reporting
# failure (see the lint-handling note near the summary below). Classifying
# up front also protects against a mistyped --store landing this tool's
# store directories and a replacement post-commit hook in an unrelated git
# repo: an existing git repo at --store with none of this tool's markers is
# refused outright here, never silently adopted -- no --force carve-out,
# since --force's job is "allow nesting", not "allow adopting the wrong repo".
STORE_IS_ADOPTED=0
if is_git_repo "$STORE"; then
    # mc_is_marked_store (scripts/mc-registry-lib.sh) is the one definition of
    # "this path holds a store", shared with the re-render walk.
    if ! mc_is_marked_store "$STORE"; then
        fail "--store $STORE is an existing git repo with none of this tool's markers (no topics/incidents/concepts directory, no README mentioning MemContinuum) -- refusing to seed store directories and a replacement post-commit hook into what looks like an unrelated repo. Point --store at a location that does not exist yet, or at an existing MemContinuum store." 9
    fi
    STORE_IS_ADOPTED=1
fi

# claude-dir (and store's parent) must be writable.
STORE_PARENT_ANCESTOR="$(nearest_existing_ancestor "$(dirname "$STORE")")"
[ -w "$STORE_PARENT_ANCESTOR" ] || fail "cannot write under $(dirname "$STORE") (nearest existing ancestor $STORE_PARENT_ANCESTOR is not writable)" 5

CLAUDE_DIR_ANCESTOR="$(nearest_existing_ancestor "$CLAUDE_DIR")"
[ -w "$CLAUDE_DIR_ANCESTOR" ] || fail "cannot write --claude-dir $CLAUDE_DIR (nearest existing ancestor $CLAUDE_DIR_ANCESTOR is not writable)" 5

# D4 (updater workstream): refuse a foreign rules file BEFORE any mutation
# below (fix-round-4 F10's own principle -- a refusal after hooks are
# already wired would report failure from a half-finished install).
# templates/memcontinuum-rules.md's first line is the identity marker; an
# existing $CLAUDE_DIR/rules/memcontinuum.md is only ever overwritten when
# its own first line matches -- a hand-authored or foreign file at that path
# is left alone, loudly.
# Read from the template that defines it (mc_rules_identity_marker), never
# copied into this file: one place says what a rendered rules file looks like.
mc_rules_identity_marker "$ENGINE_ROOT" \
    || fail "cannot read $TEMPLATES_DIR/memcontinuum-rules.md (or it is empty) -- incomplete checkout" 3
RULES_IDENTITY_MARKER="$MC_RULES_MARKER"
RULES_DEST="$CLAUDE_DIR/rules/memcontinuum.md"
if [ -f "$RULES_DEST" ]; then
    RULES_DEST_FIRST_LINE="$(head -n 1 "$RULES_DEST" 2>/dev/null)"
    if [ "$RULES_DEST_FIRST_LINE" != "$RULES_IDENTITY_MARKER" ]; then
        fail "$RULES_DEST already exists and was not rendered by this installer (first line does not match the identity marker) -- refusing to overwrite a hand-authored or foreign rules file. Move it aside first if you want repo-init to render one here." 14
    fi
fi

# Ruling 52: same principle, same place, for the installed memory-search
# skill copy -- refused BEFORE any mutation, not after the skill install
# step's own `cp -f` below has already clobbered it. Unlike the rules file
# this one used to have NO refusal of its own at all (memcontinuum-update.sh
# could report a foreign copy as `skill-foreign` in its table, but repo-init
# itself would still overwrite one if invoked directly -- exactly the path
# `--add-lang`/`--never-ext` take, since they call this script directly
# rather than through the updater's own skill-foreign gate). Identity is
# `mc_skill_copy_is_ours` (mc-registry-lib.sh) -- the ONE predicate this
# refusal and memcontinuum-update.sh's `skill` column both call, so the
# installer and the re-render walk agree, by construction, on what "ours"
# means here. Our own copy -- identity matches, whatever its stamp -- is
# still overwritten below as always; only a file that is not ours at all is
# refused.
SKILL_DEST="$CLAUDE_DIR/skills/memory-search/SKILL.md"
if [ -f "$SKILL_DEST" ] && ! mc_skill_copy_is_ours "$SKILL_DEST"; then
    fail "$SKILL_DEST already exists and was not rendered by this installer (no \`name: memory-search\` frontmatter) -- refusing to overwrite a hand-authored or foreign skill copy. Move it aside first if you want repo-init to install one here." 16
fi

# --- code census + consent dialogue (Task 10, Anatomy M1) -----------------
#
# Runs BEFORE the plan summary (so its outcome -- the chosen language set,
# any "never" notes -- can be printed there) and BEFORE any mutation below
# (nothing has written anything to disk yet at this point). Runs under
# --dry-run too: it is read-only discovery, not an install step; only the
# initial code-reindex later (step 7) is gated on $DRY_RUN.
#
# Precedence: --langs (if given) always wins, no dialogue, no tty needed.
# Else --non-interactive with no --langs is language-less wiring (a note,
# no dialogue). Else, if census proposes nothing at all (empty project),
# language-less wiring with no dialogue either -- there is nothing to ask
# about (spec S3: "Empty project => language-less wiring"). Only when
# something WAS proposed and neither bypass flag was given does this
# actually try to read a live consent dialogue from /dev/tty -- guarded by
# `[ -t 0 ]` so a non-interactive run with no bypass flag fails with a
# clear message instead of hanging on a read that will never complete.
CHOSEN_LANGS=""
# B4 (Anatomy M1 fix wave): the "never for one extension" answer and its
# non-interactive seam (--never-ext) both feed this ONE list. It is a
# comma/space-separated list of raw extensions (".cs" or "cs"); the render
# heredoc below normalizes each into a glob (`*.cs`) for the nudge hook's
# MEMCONTINUUM_NEVER_EXTS. It deliberately does NOT touch CHOSEN_LANGS:
# "never mention .cs" is about one extension's nagging, not about giving
# up the language set the census just proposed. (The old option 4 set
# CHOSEN_LANGS="" -- answering "stop nagging me about .cs" silently
# disabled python indexing too.)
NEVER_EXTS_RAW="$NEVER_EXT_FLAG"
declare -a NEVER_NOTES=()

if [ "${#CODE_ROOTS_ABS[@]}" -gt 0 ]; then
    # Carry (Task 8 reviewer): repo-init does its own existence check on
    # each code root BEFORE invoking census -- a missing dir is repo-init's
    # own error, never inferred from code-census's fail-open (code-census
    # exits 0 on a nonexistent root by design, walking nothing and returning
    # every language at zero, so silence there would otherwise read as
    # "found nothing", not "you pointed --code-root at nothing"). Gated on
    # real (non-dry) runs only:
    # --dry-run is a preview and a --code-root need not exist yet for one
    # (pre-existing contract -- TestMultipleCodeRoots' two-code-roots
    # dry-run test passes roots that are never created). Under --dry-run
    # with a missing root, code-census's own fail-open (nothing walked, every
    # language at zero) takes over below and the run proceeds as "nothing
    # proposed".
    if [ "$DRY_RUN" -eq 0 ]; then
        for cr in "${CODE_ROOTS_ABS[@]}"; do
            [ -d "$cr" ] || fail "--code-root $cr does not exist -- pass an existing directory (repo-init checks this itself before running any census)" 10
        done
    fi

    CENSUS_VARS="$(mktemp 2>/dev/null)" || fail "could not create a temp file for census results" 10
    trap 'rm -f "$CENSUS_VARS"' EXIT

    CODE_ROOTS_NL_FOR_CENSUS=""
    for cr in "${CODE_ROOTS_ABS[@]}"; do
        CODE_ROOTS_NL_FOR_CENSUS+="$cr"$'\n'
    done

    echo "Code census:"
    MC_CENSUS_CODE_ROOTS="$CODE_ROOTS_NL_FOR_CENSUS" \
    MC_CENSUS_PYTHON="$PYTHON_BIN" \
    MC_CENSUS_MEMIDX="$MEMIDX" \
    MC_CENSUS_ENGINE_ROOT="$ENGINE_ROOT" \
    MC_CENSUS_VARS_OUT="$CENSUS_VARS" \
    PYTHONPATH= "$PYTHON_BIN" - <<'PYEOF'
import json
import os
import subprocess
import sys

sys.path.insert(0, os.environ["MC_CENSUS_ENGINE_ROOT"])
import chunkers  # noqa: E402

roots = [l for l in os.environ["MC_CENSUS_CODE_ROOTS"].split("\n") if l]
python_bin = os.environ["MC_CENSUS_PYTHON"]
memidx_path = os.environ["MC_CENSUS_MEMIDX"]

# Aggregated across every --code-root given to this run -- one census
# table for the whole install, not one per root (repo-init installs ONE
# language set for the project, not a per-root set).
merged = {}
for root in roots:
    proc = subprocess.run(
        [python_bin, memidx_path, "code-census", "--root", root, "--json"],
        capture_output=True, text=True,
    )
    # code-census is documented exit-0-always (even a nonexistent root just
    # walks nothing and returns its zero-count rows) -- a non-zero rc here
    # means something actually crashed
    # (e.g. an import failure), and treating that as "nothing proposed"
    # would silently reach language-less wiring through the exact side
    # door the carry note (Task 8 reviewer) exists to close. Surface it as
    # this heredoc's own failure so the caller's `fail ... 10` fires.
    if proc.returncode != 0:
        print("ERROR: code-census --root %s failed (rc=%s):" % (root, proc.returncode), file=sys.stderr)
        print(proc.stderr, file=sys.stderr)
        sys.exit(1)
    # I1 (Anatomy M1 fix wave, Grok MEDIUM): unparseable stdout is a
    # FAILURE, exactly like a non-zero rc above. Swallowing the
    # JSONDecodeError and carrying on with {} let a broken census reach
    # language-less wiring through the very side door the rc check exists
    # to close -- "nothing proposed", said in the voice of a scan that
    # worked.
    try:
        data = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError as exc:
        print("ERROR: code-census --root %s printed output that is not valid JSON: %s"
              % (root, exc), file=sys.stderr)
        print(proc.stdout, file=sys.stderr)
        sys.exit(1)
    if not isinstance(data, dict):
        print("ERROR: code-census --root %s printed valid JSON that is not an object"
              % root, file=sys.stderr)
        sys.exit(1)
    for key, row in data.items():
        m = merged.setdefault(key, {"files": 0, "status": row.get("status", "unsupported")})
        m["files"] += row.get("files", 0)

known_langs = sorted(chunkers.LANGUAGE_TABLE.keys())
proposed = sorted(
    (k for k, v in merged.items() if v["status"] == "supported" and v["files"] > 0),
    key=lambda k: (-merged[k]["files"], k),
)
unfound = [l for l in known_langs if l not in proposed]
unsupported = sorted(
    ((k, v["files"]) for k, v in merged.items() if v["status"] == "unsupported"),
    key=lambda kv: (-kv[1], kv[0]),
)

print("  proposed (supported, found):")
if proposed:
    for lang in proposed:
        print("    %s: %d" % (lang, merged[lang]["files"]))
else:
    print("    (none)")
print("  supported but not found (skip):")
if unfound:
    for lang in unfound:
        print("    %s" % lang)
else:
    print("    (none)")
print("  unsupported (no chunker in this engine version):")
if unsupported:
    for ext, n in unsupported:
        print("    %s: %d" % (ext, n))
else:
    print("    (none)")

with open(os.environ["MC_CENSUS_VARS_OUT"], "w") as f:
    f.write("MC_CENSUS_PROPOSED=%s\n" % repr(" ".join(proposed)))
    f.write("MC_CENSUS_KNOWN_LANGS=%s\n" % repr(" ".join(known_langs)))
PYEOF
    CENSUS_RC=$?
    [ "$CENSUS_RC" -eq 0 ] || fail "code-census failed (see output above)" 10

    # shellcheck source=/dev/null
    . "$CENSUS_VARS"
    rm -f "$CENSUS_VARS"
    trap - EXIT
    MC_CENSUS_PROPOSED="${MC_CENSUS_PROPOSED:-}"
    MC_CENSUS_KNOWN_LANGS="${MC_CENSUS_KNOWN_LANGS:-}"

    if [ -n "$LANGS_FLAG" ]; then
        for want in $(printf '%s' "$LANGS_FLAG" | tr ',' ' '); do
            case " $MC_CENSUS_KNOWN_LANGS " in
                *" $want "*) ;;
                *) fail "--langs names an unknown language: $want (this engine version knows: $MC_CENSUS_KNOWN_LANGS)" 11 ;;
            esac
        done
        CHOSEN_LANGS="$LANGS_FLAG"
        echo "note: languages chosen via --langs: $CHOSEN_LANGS"
    elif [ "$NON_INTERACTIVE" -eq 1 ]; then
        CHOSEN_LANGS=""
        echo "note: --non-interactive with no --langs -- language-less wiring (no language enabled; initial code-reindex skipped)"
    elif [ -z "$MC_CENSUS_PROPOSED" ]; then
        # Empty project => language-less wiring (spec S3): nothing was
        # detected across any --code-root, so there is nothing to ask
        # about -- no dialogue, no note, matches the pre-Task-10 no-op
        # behavior for an empty/untouched code root.
        CHOSEN_LANGS=""
    else
        # Ruling 7 (Task 10 fix round, owner): names both bypass flags AND
        # points a non-tty caller (e.g. a Claude-Code-driven install, whose
        # Bash tool has no tty) at the census command it should run itself
        # first -- the driven flow is: run `memidx.py code-census --root
        # <code-root> --json` (with the resolved python, PYTHONPATH=
        # cleared), present the three categories and skip/enable-all/select
        # to the human in conversation, then re-run this script with
        # --langs <chosen> or --non-interactive (skill wiring, see
        # skills/memcontinuum/SKILL.md's driven-install section).
        [ -t 0 ] || fail "code census proposes a language set ($MC_CENSUS_PROPOSED) but this is not an interactive terminal (stdin is not a tty) -- pass --langs LIST or --non-interactive to continue without the consent dialogue. Driven from an agent (no tty): run 'PYTHONPATH= $PYTHON_BIN $MEMIDX code-census --root <code-root> --json' yourself, present the result to the human, then re-run this script with --langs <chosen> or --non-interactive." 12
        echo
        echo "Enable code indexing for a detected language?"
        echo "  1) skip -- language-less wiring"
        echo "  2) enable all detected: $MC_CENSUS_PROPOSED"
        echo "  3) select from detected"
        echo "  4) never for one extension (stop nagging about it)"
        printf '> '
        DIALOGUE_CHOICE=""
        read -r DIALOGUE_CHOICE < /dev/tty
        case "$DIALOGUE_CHOICE" in
            2)
                CHOSEN_LANGS="$(printf '%s' "$MC_CENSUS_PROPOSED" | tr ' ' ',')"
                ;;
            3)
                echo "Enter the languages to enable, space-separated (from: $MC_CENSUS_PROPOSED):"
                printf '> '
                SELECTED=""
                read -r SELECTED < /dev/tty
                SEL_OK=""
                for want in $SELECTED; do
                    case " $MC_CENSUS_PROPOSED " in
                        *" $want "*) SEL_OK="$SEL_OK,$want" ;;
                        *) echo "note: ignoring unrecognized/undetected language: $want" ;;
                    esac
                done
                CHOSEN_LANGS="${SEL_OK#,}"
                ;;
            4)
                echo "Enter the extension the new-file reminder should never mention again (e.g. .cs):"
                printf '> '
                NEVER_EXT=""
                read -r NEVER_EXT < /dev/tty
                # B4: keep the proposed language set -- this answer excludes
                # ONE extension from the nudge, it does not decline indexing.
                CHOSEN_LANGS="$(printf '%s' "$MC_CENSUS_PROPOSED" | tr ' ' ',')"
                if [ -n "$NEVER_EXT" ]; then
                    if [ -n "$NEVER_EXTS_RAW" ]; then
                        NEVER_EXTS_RAW="$NEVER_EXTS_RAW,$NEVER_EXT"
                    else
                        NEVER_EXTS_RAW="$NEVER_EXT"
                    fi
                fi
                echo "note: languages enabled: $CHOSEN_LANGS"
                ;;
            *)
                CHOSEN_LANGS=""
                ;;
        esac
    fi
    if [ -n "$NEVER_EXTS_RAW" ]; then
        # Deliberately narrow wording: what actually happens is that this
        # wiring's nudge hook line carries the extension, so the reminder
        # stops mentioning it. Nothing is recorded in any registry, so a
        # later install elsewhere asks again -- say that, rather than
        # claiming a durable "never ask about that one".
        for _never in $(printf '%s' "$NEVER_EXTS_RAW" | tr ',' ' '); do
            NEVER_NOTES+=("$_never")
        done
        echo "noted for this wiring only: never=$(printf '%s' "$NEVER_EXTS_RAW" | tr ',' ' ') (not recorded machine-wide)"
    fi
    echo
fi

# --- plan summary ------------------------------------------------------

echo "MemContinuum install plan"
echo "  project     : $PROJECT"
echo "  store       : $STORE"
if [ "${#CODE_ROOTS_ABS[@]}" -eq 0 ]; then
    echo "  code roots  : (none -- rationale-only install, no PreToolUse hook)"
else
    for cr in "${CODE_ROOTS_ABS[@]}"; do
        echo "  code root   : $cr"
    done
    if [ -n "$CHOSEN_LANGS" ]; then
        echo "  languages   : $CHOSEN_LANGS"
    else
        echo "  languages   : (none -- language-less wiring)"
    fi
    for note in "${NEVER_NOTES[@]:-}"; do
        [ -n "$note" ] && echo "  never-mention: $note (new-file reminder only; languages above are unaffected)"
    done
fi
echo "  claude-dir  : $CLAUDE_DIR"
echo "  python      : $PYTHON_BIN"
echo "  engine dir  : $ENGINE_ROOT"
[ "$DRY_RUN" -eq 1 ] && echo "  mode        : DRY RUN -- nothing below is actually written"
echo

# --- 1. store tree -------------------------------------------------------

STORE_DIRS=(topics incidents investigations concepts sources inbox/codex inbox/grok inbox/audit)

step "store tree: create ${STORE_DIRS[*]} under $STORE (each with .gitkeep)"
if [ "$DRY_RUN" -eq 0 ]; then
    for d in "${STORE_DIRS[@]}"; do
        mkdir -p "$STORE/$d" || fail "could not create $STORE/$d"
        touch "$STORE/$d/.gitkeep" || fail "could not write $STORE/$d/.gitkeep"
    done
fi

# --- 2. store README.md + .gitignore from templates -----------------------
#
# Write-if-absent, not overwrite: an "adopt an existing store repo" install
# (STORE already has its own .git -- see step 3) may point at a store with
# a hand-authored README carrying real provenance notes. Clobbering that on
# every re-run would be a silent, un-asked-for content loss, so both files
# are seeded only when missing; a fresh store still gets both on its first
# install.

if [ -f "$STORE/README.md" ]; then
    step "store README.md already exists -- leaving it alone"
else
    step "store README.md rendered from templates/store-README.md.tmpl"
    if [ "$DRY_RUN" -eq 0 ]; then
        FIRST_CODE_ROOT="<no code-root configured>"
        [ "${#CODE_ROOTS_ABS[@]}" -gt 0 ] && FIRST_CODE_ROOT="${CODE_ROOTS_ABS[0]}"

        README_TMPL="$(cat "$TEMPLATES_DIR/store-README.md.tmpl")"
        README_TMPL="${README_TMPL//\{\{PROJECT\}\}/$PROJECT}"
        README_TMPL="${README_TMPL//\{\{STORE\}\}/$STORE}"
        README_TMPL="${README_TMPL//\{\{PYTHON\}\}/$PYTHON_BIN}"
        README_TMPL="${README_TMPL//\{\{ENGINE_DIR\}\}/$ENGINE_ROOT}"
        README_TMPL="${README_TMPL//\{\{CODE_ROOT\}\}/$FIRST_CODE_ROOT}"
        printf '%s' "$README_TMPL" > "$STORE/README.md" || fail "could not write $STORE/README.md"
    fi
fi

if [ -f "$STORE/.gitignore" ]; then
    step "store .gitignore already exists -- leaving it alone"
else
    step "store .gitignore written"
    if [ "$DRY_RUN" -eq 0 ]; then
        printf '*.sqlite\n*.sqlite3\n__pycache__/\n' > "$STORE/.gitignore" || fail "could not write $STORE/.gitignore"
    fi
fi

# --- 3. git init + initial commit ------------------------------------------

if is_git_repo "$STORE"; then
    step "store is already a git repo -- skipping git init"
else
    step "git init $STORE + initial commit"
    if [ "$DRY_RUN" -eq 0 ]; then
        git init -q "$STORE" || fail "git init failed in $STORE"
        if git -C "$STORE" config user.name >/dev/null 2>&1 && git -C "$STORE" config user.email >/dev/null 2>&1; then
            GIT_ID_ARGS=()
        else
            GIT_ID_ARGS=(-c user.name=memcontinuum-install -c user.email=install@memcontinuum.invalid)
        fi
        git -C "$STORE" "${GIT_ID_ARGS[@]}" add -A || fail "git add failed in $STORE"
        git -C "$STORE" "${GIT_ID_ARGS[@]}" commit -q -m "MemContinuum: seed store for $PROJECT" \
            || fail "initial commit failed in $STORE"
    fi
fi

# --- 4. wire hooks into <claude-dir>/settings.local.json -------------------

step "wire hooks into $CLAUDE_DIR/settings.local.json (backup to *.bak-memcontinuum first)"

CODE_ROOTS_NL=""
for cr in "${CODE_ROOTS_ABS[@]:-}"; do
    [ -n "$cr" ] && CODE_ROOTS_NL+="$cr"$'\n'
done

MC_INSTALL_PROJECT="$PROJECT" \
MC_INSTALL_STORE="$STORE" \
MC_INSTALL_CLAUDE_DIR="$CLAUDE_DIR" \
MC_INSTALL_PYTHON="$PYTHON_BIN" \
MC_INSTALL_HOOKS_DIR="$HOOKS_DIR" \
MC_INSTALL_TEMPLATES_DIR="$TEMPLATES_DIR" \
MC_INSTALL_CODE_ROOTS="$CODE_ROOTS_NL" \
MC_INSTALL_DRY_RUN="$DRY_RUN" \
MC_INSTALL_SCRIPTS_DIR="$SCRIPT_DIR" \
MC_INSTALL_ENGINE_ROOT="$ENGINE_ROOT" \
MC_INSTALL_LANGS="$CHOSEN_LANGS" \
MC_INSTALL_NEVER_EXTS="$NEVER_EXTS_RAW" \
MC_INSTALL_RENDERED="$RENDERED_SHA" \
"$PYTHON_BIN" - <<'PYEOF'
import json
import os
import shlex
import sys

project = os.environ["MC_INSTALL_PROJECT"]
store = os.environ["MC_INSTALL_STORE"]
claude_dir = os.environ["MC_INSTALL_CLAUDE_DIR"]
python_bin = os.environ["MC_INSTALL_PYTHON"]
hooks_dir = os.environ["MC_INSTALL_HOOKS_DIR"]
templates_dir = os.environ["MC_INSTALL_TEMPLATES_DIR"]
code_roots = [l for l in os.environ.get("MC_INSTALL_CODE_ROOTS", "").split("\n") if l]
dry_run = os.environ.get("MC_INSTALL_DRY_RUN", "0") == "1"
rendered_sha = os.environ["MC_INSTALL_RENDERED"]

# Fix-round-4 F8: the ONE settings merge implementation, shared with
# memcontinuum-setup.sh -- see scripts/mc_settings_merge.py's own docstring.
sys.path.insert(0, os.environ["MC_INSTALL_SCRIPTS_DIR"])
from mc_settings_merge import merge_settings, MergeRefused, basenames_identity

sys.path.insert(0, os.environ["MC_INSTALL_ENGINE_ROOT"])
import chunkers  # noqa: E402

OUR_SCRIPTS = [
    "pre-edit-chain.sh",
    "newfile-nudge.sh",
    "ledger-post-edit.sh",
    "precompact-persist.sh",
    "sessionstart-remind.sh",
    "userprompt-remind.sh",
    "sessionend-stamp.sh",
]


def esc_json(s):
    """Escape s for embedding inside an existing JSON string literal
    (i.e. json.dumps(s) with the surrounding quotes stripped)."""
    return json.dumps(s)[1:-1]


def esc_cmd(s):
    """Shell-quote s (so spaces/special chars survive as one shell word
    inside a hook `command` line), then JSON-escape the quoted result."""
    return esc_json(shlex.quote(s))


def read_tmpl(name):
    with open(os.path.join(templates_dir, name), "r", encoding="utf-8") as f:
        return f.read()


def render(text, mapping):
    for key, val in mapping.items():
        text = text.replace("{{%s}}" % key, val)
    return text


blocks = {}  # event_name -> list of new group dicts

# --- write-hooks block (always present) ---
code_root_env = ""
if code_roots:
    code_root_env = "MEMCONTINUUM_CODE_ROOT=%s " % esc_cmd(code_roots[0])

# Task 10: newfile-nudge's env-driven extension gate (Task 9). KNOWN_EXTS is
# a constant of this engine version (every LANGUAGE_TABLE row's extensions),
# rendered onto the nudge line whenever it exists at all -- independent of
# which languages were actually chosen. LANG_EXTS_ENV is the WHOLE
# "MEMCONTINUUM_LANG_EXTS='...' " token (trailing space and all, matching
# the {{CODE_ROOT_ENV}} pattern write-hooks.json.tmpl above already uses).
#
# Ruling 6 (Task 10 fix round, owner): rendered EXPLICITLY EMPTY --
# `MEMCONTINUUM_LANG_EXTS=''` -- never omitted, when no language was
# chosen. This relies on a matching Ruling-6 fix in
# hooks/newfile-nudge.sh: its fallback there changed from
# `${MEMCONTINUUM_LANG_EXTS:-*.swift}` to `${MEMCONTINUUM_LANG_EXTS-*.swift}`
# (dash, no colon) specifically so "unset" (legacy un-re-rendered wiring,
# still falls back to `*.swift`) and "set but empty" (deliberate
# language-less wiring, matches nothing) are distinct states -- bash's
# `:-` could never tell them apart, which is why the token used to be
# omitted entirely instead (Task 10's original round; see that round's
# report for the gap this closes).
chosen_langs = [l.strip() for l in os.environ.get("MC_INSTALL_LANGS", "").split(",") if l.strip()]
known_exts_str = " ".join("*" + e for e in sorted(chunkers.known_extensions()))
known_exts_cmd = esc_cmd(known_exts_str)
wired_exts_str = " ".join("*" + e for e in sorted(chunkers.wired_extensions(chosen_langs))) if chosen_langs else ""
lang_exts_env = "MEMCONTINUUM_LANG_EXTS=%s " % esc_cmd(wired_exts_str)


def _never_glob(raw):
    """B4: normalize one human-typed extension into a nudge-hook glob.
    ".cs", "cs" and "*.cs" all become "*.cs"; anything already carrying a
    glob character is passed through as typed."""
    raw = raw.strip()
    if not raw:
        return ""
    if any(ch in raw for ch in "*?["):
        return raw
    if not raw.startswith("."):
        raw = "." + raw
    return "*" + raw


# B4 (Anatomy M1 fix wave): render-time persistence for the consent
# dialogue's "never for one extension" answer (and its --never-ext seam) --
# the extension list rides on the nudge hook's own command line, refreshed
# by every install, exactly like LANG_EXTS/KNOWN_EXTS. ALWAYS rendered,
# empty when nothing was ever answered, so the token's presence never
# depends on a run's answers (same reasoning as Ruling 6's explicitly-empty
# MEMCONTINUUM_LANG_EXTS).
never_raw = os.environ.get("MC_INSTALL_NEVER_EXTS", "").replace(",", " ")
never_globs = []
for _tok in never_raw.split():
    _glob = _never_glob(_tok)
    if _glob and _glob not in never_globs:
        never_globs.append(_glob)
never_exts_env = "MEMCONTINUUM_NEVER_EXTS=%s " % esc_cmd(" ".join(never_globs))

write_tmpl = read_tmpl("write-hooks.json.tmpl")
write_rendered = render(write_tmpl, {
    "STORE": esc_cmd(store),
    "PROJECT": esc_cmd(project),
    "PYTHON": esc_cmd(python_bin),
    "HOOKS_DIR": esc_cmd(hooks_dir),
    "CODE_ROOT_ENV": code_root_env,
    "RENDERED": esc_cmd(rendered_sha),
})
try:
    write_block = json.loads(write_rendered)
except json.JSONDecodeError as e:
    print("ERROR: rendered write-hooks template is not valid JSON: %s" % e, file=sys.stderr)
    print(write_rendered, file=sys.stderr)
    sys.exit(1)

for event, groups in write_block["hooks"].items():
    blocks.setdefault(event, []).extend(groups)

# --- pre-edit block (only if there is at least one code root) ---
if code_roots:
    pair_tmpl = read_tmpl("code-root-filter-pair.json.tmpl")
    pairs = []
    for cr in code_roots:
        strip_prefix = cr.rstrip("/") + "/"
        pairs.append(render(pair_tmpl, {
            "CODE_ROOT": esc_json(cr.rstrip("/")),
            "STORE": esc_cmd(store),
            "PROJECT": esc_cmd(project),
            "STRIP_PREFIX": esc_cmd(strip_prefix),
            "PYTHON": esc_cmd(python_bin),
            "HOOKS_DIR": esc_cmd(hooks_dir),
            "RENDERED": esc_cmd(rendered_sha),
        }))
    filters_text = ",\n".join(pairs)

    pre_tmpl = read_tmpl("pre-edit-hook.json.tmpl")
    pre_rendered = pre_tmpl.replace("{{CODE_ROOT_FILTERS}}", filters_text)
    try:
        pre_block = json.loads(pre_rendered)
    except json.JSONDecodeError as e:
        print("ERROR: rendered pre-edit-hook template is not valid JSON: %s" % e, file=sys.stderr)
        print(pre_rendered, file=sys.stderr)
        sys.exit(1)
    for event, groups in pre_block["hooks"].items():
        blocks.setdefault(event, []).extend(groups)

    # --- newfile-nudge block (finding 8): a SECOND, separate PreToolUse
    # matcher group (matcher "Write" only, never "Edit|Write" -- this hook
    # never fires on an edit to an existing file) appended alongside the
    # pre-edit-chain.sh group above, one `if` entry per --code-root. Never
    # carries MEMCONTINUUM_ROOT/STRIP_PREFIX -- this hook never calls
    # memidx.py at all (see its own header comment). It DOES carry
    # MEMCONTINUUM_PROJECT (fix-round-4 F1) -- identity only, the hook itself
    # never reads it -- so is_ours() below can scope a sweep to this project
    # and never unwire a coexisting project's nudge entries sharing the same
    # claude-dir (the bug: a markerless nudge command read as ours/sweepable
    # regardless of which project's re-run swept it).
    nudge_pair_tmpl = read_tmpl("newfile-nudge-filter-pair.json.tmpl")
    nudge_pairs = []
    for cr in code_roots:
        nudge_pairs.append(render(nudge_pair_tmpl, {
            "CODE_ROOT": esc_json(cr.rstrip("/")),
            "CODE_ROOT_CMD": esc_cmd(cr.rstrip("/")),
            "PROJECT": esc_cmd(project),
            "PYTHON": esc_cmd(python_bin),
            "HOOKS_DIR": esc_cmd(hooks_dir),
            "LANG_EXTS_ENV": lang_exts_env,
            "NEVER_EXTS_ENV": never_exts_env,
            "KNOWN_EXTS_CMD": known_exts_cmd,
            "RENDERED": esc_cmd(rendered_sha),
        }))
    nudge_filters_text = ",\n".join(nudge_pairs)

    nudge_tmpl = read_tmpl("newfile-nudge-hook.json.tmpl")
    nudge_rendered = nudge_tmpl.replace("{{CODE_ROOT_FILTERS}}", nudge_filters_text)
    try:
        nudge_block = json.loads(nudge_rendered)
    except json.JSONDecodeError as e:
        print("ERROR: rendered newfile-nudge-hook template is not valid JSON: %s" % e, file=sys.stderr)
        print(nudge_rendered, file=sys.stderr)
        sys.exit(1)
    for event, groups in nudge_block["hooks"].items():
        blocks.setdefault(event, []).extend(groups)


# Identity-aware (regate finding 1, F1 nudge fix): an entry is THIS
# install's only when it names one of our scripts AND belongs to this
# --project. Basename alone silently unwired a coexisting project sharing
# the same claude-dir -- an accepted topology, since two sibling explicit
# stores derive the same parent .claude, and exactly how the engine's own
# wiring was destroyed by a test run. An entry naming our scripts with NO
# project marker at all is treated as ours (pre-identity legacy wiring,
# safe to refresh) -- see README.md/hooks/install-hooks.md for the
# migration note this implies for pre-F1 nudge entries specifically.
is_ours = basenames_identity(OUR_SCRIPTS, project)

settings_path = os.path.join(claude_dir, "settings.local.json")

# Sweep our script-keyed items out of every event this installer ever
# writes to -- not just the events the CURRENT run happens to render.
# Otherwise re-running with fewer --code-roots than a previous run (e.g.
# down to zero) leaves stale PreToolUse entries behind: idempotency means
# "this run's config replaces the LAST run's", not "only touch what this
# run has something new to add." merge_settings (fix-round-4 F8) is the
# ONE settings-merge implementation, shared with memcontinuum-setup.sh --
# see scripts/mc_settings_merge.py.
ALL_EVENTS = sorted({
    "PreToolUse", "PostToolUse", "PreCompact",
    "SessionStart", "UserPromptSubmit", "SessionEnd",
})

if not dry_run:
    os.makedirs(claude_dir, exist_ok=True)

try:
    merge_settings(settings_path, ALL_EVENTS, is_ours, add=blocks, dry_run=dry_run, log=print)
except MergeRefused as e:
    print(str(e), file=sys.stderr)
    sys.exit(1)
except ValueError as e:
    print("ERROR: %s" % e, file=sys.stderr)
    sys.exit(1)
PYEOF
MERGE_RC=$?
[ $MERGE_RC -eq 0 ] || fail "hook wiring failed (see above)" 6

# --- 4b. render the project routing rule (D4, TOP-0117/INC-0105) ----------
#
# Rendered on EVERY install (fresh or re-run), not write-if-absent like the
# store README above -- this file's whole point is to always name the
# CURRENT store, and repo-init is its only writer (the identity-marker
# refusal above already ruled out clobbering anything foreign). {{STORE}}
# is the template's one placeholder; the stamp line goes right after the
# identity-marker first line so a `head -1` identity check (above, and any
# future one) keeps matching a stamped file.

step "rules file: $RULES_DEST (rendered from templates/memcontinuum-rules.md)"
if [ "$DRY_RUN" -eq 0 ]; then
    mkdir -p "$CLAUDE_DIR/rules" || fail "could not create $CLAUDE_DIR/rules"
    RULES_TMPL="$(cat "$TEMPLATES_DIR/memcontinuum-rules.md")"
    RULES_TMPL="${RULES_TMPL//\{\{STORE\}\}/$STORE}"
    RULES_FIRST_LINE="${RULES_TMPL%%$'\n'*}"
    RULES_REST="${RULES_TMPL#*$'\n'}"
    {
        printf '%s\n' "$RULES_FIRST_LINE"
        printf '<!-- memcontinuum-rendered: %s -->\n' "$RENDERED_SHA"
        printf '%s\n' "$RULES_REST"
    } > "$RULES_DEST" || fail "could not write $RULES_DEST"
fi

# --- 5. install the memory-search skill -------------------------------

step "install skill: $CLAUDE_DIR/skills/memory-search/SKILL.md"
if [ "$DRY_RUN" -eq 0 ]; then
    mkdir -p "$CLAUDE_DIR/skills/memory-search" || fail "could not create $CLAUDE_DIR/skills/memory-search"
    cp -f "$SKILL_SRC" "$CLAUDE_DIR/skills/memory-search/SKILL.md" || fail "could not copy SKILL.md"
    # D1: stamp the INSTALLED COPY only, right after the frontmatter's
    # closing "---" (never at byte 0 -- the skill loader needs the OPENING
    # "---" to stay the very first line of the file). SKILL_DEST is the same
    # path the foreign-copy refusal above already checked.
    FM_END_LINE="$(grep -n '^---$' "$SKILL_DEST" | sed -n '2p' | cut -d: -f1)"
    if [ -n "$FM_END_LINE" ]; then
        SKILL_TMP="$SKILL_DEST.tmp-memcontinuum-stamp"
        {
            head -n "$FM_END_LINE" "$SKILL_DEST"
            printf '<!-- memcontinuum-rendered: %s -->\n' "$RENDERED_SHA"
            tail -n "+$((FM_END_LINE + 1))" "$SKILL_DEST"
        } > "$SKILL_TMP" && mv "$SKILL_TMP" "$SKILL_DEST" || fail "could not stamp $SKILL_DEST"
    fi
fi

# --- 6. git post-commit reindex wrapper -------------------------------
#
# Deviation from a plain symlink (documented in hooks/install-hooks.md
# section 2 as the fallback): post-commit-reindex.sh exits immediately
# ("MEMCONTINUUM_ROOT not set, skipping") unless MEMCONTINUUM_ROOT/PROJECT/
# PYTHON are already in its environment, and a bare symlinked git hook
# carries no environment of its own. A tiny generated wrapper that exports
# those three vars and `exec`s the real script by its absolute path gets
# both properties install-hooks.md asks for: it works, AND edits to
# post-commit-reindex.sh are picked up automatically without reinstalling
# (because the wrapper execs the canonical file, it never copies it).

if is_git_repo "$STORE"; then
    # R8 fix, round 4 (corrected in regate round 2): install where git
    # actually RUNS post-commit for commits in $STORE -- `rev-parse
    # --git-path hooks` -- which for a linked worktree is the shared
    # repo's .git/hooks, never .git/worktrees/<name>/hooks.
    STORE_HOOKS_DIR="$(git_hooks_dir_for "$STORE")" || fail "could not resolve git hooks dir for $STORE"
    step "git post-commit reindex wrapper: $STORE_HOOKS_DIR/post-commit -> $HOOKS_DIR/post-commit-reindex.sh"
    if [ "$DRY_RUN" -eq 0 ]; then
        mkdir -p "$STORE_HOOKS_DIR" || fail "could not create $STORE_HOOKS_DIR"
        POST_COMMIT="$STORE_HOOKS_DIR/post-commit"
        {
            printf '#!/usr/bin/env bash\n'
            printf 'export MEMCONTINUUM_ROOT=%s\n' "$(printf '%q' "$STORE")"
            printf 'export MEMCONTINUUM_PROJECT=%s\n' "$(printf '%q' "$PROJECT")"
            printf 'export MEMCONTINUUM_PYTHON=%s\n' "$(printf '%q' "$PYTHON_BIN")"
            printf 'exec bash %s\n' "$(printf '%q' "$HOOKS_DIR/post-commit-reindex.sh")"
        } > "$POST_COMMIT" || fail "could not write $POST_COMMIT"
        chmod +x "$POST_COMMIT" || fail "could not chmod $POST_COMMIT"
    fi
fi

# --- 7. reindex + lint --------------------------------------------------
#
# Deviation: --no-embed on this install-time reindex. The seeded store
# holds only README/.gitkeep stubs -- there is nothing worth embedding yet,
# and running the real embedder here would make a first install silently
# depend on network access (or a pre-warmed fastembed model cache) it does
# not otherwise need. The store repo's post-commit hook (step 6) will run a
# full (embedding) reindex automatically on the first real commit of
# content, and `README.md`'s own "Running the engine" section documents
# `--no-embed` as scripts/repo-init.sh's choice, so it does not read as an accident.

REINDEX_CMD=("$PYTHON_BIN" "$MEMIDX" reindex --root "$STORE" --project "$PROJECT" --no-embed)
LINT_CMD=("$PYTHON_BIN" "$MEMLINT" "$STORE")

step "reindex: PYTHONPATH= ${REINDEX_CMD[*]}"
step "lint:    PYTHONPATH= ${LINT_CMD[*]}"

REINDEX_OUT=""
REINDEX_RC=0
LINT_OUT=""
LINT_RC=0
if [ "$DRY_RUN" -eq 0 ]; then
    REINDEX_OUT="$(PYTHONPATH= "${REINDEX_CMD[@]}" 2>&1)"
    REINDEX_RC=$?
    LINT_OUT="$(PYTHONPATH= "${LINT_CMD[@]}" 2>&1)"
    LINT_RC=$?
fi

# --- 7b. initial code-reindex (Task 10, Anatomy M1) -----------------------
#
# Separate call from the decision-store reindex above -- code-reindex is
# Anatomy's own intent index, keyed by --code-root, not --root/STORE (Task
# 7's `--lang` contract: required on a project's FIRST code-reindex, no
# hardcoded-swift default). Runs ONCE, for the first --code-root only --
# code-reindex is single-root by construction (see B3 below). Skipped
# entirely for language-less wiring (CHOSEN_LANGS
# empty) -- per Task 7's carry, an empty --lang set means "nothing to
# index yet", not "index nothing and call it done." Same --no-embed
# rationale as step 7's decision-store reindex above: a fresh corpus is
# not worth a network-dependent embed at install time. Unlike the
# decision-store reindex, a failure here HARD-FAILS the install (exit 13,
# its own code, distinct from the decision-store reindex's exit 7) --
# code-reindex's own per-file handling already fails open for a single bad
# file, so a non-zero exit here is structural (bad db, bad --code-root,
# bad --lang), the same class of problem exit 7 already treats as fatal.
#
# B3 (Anatomy M1 fix wave): exactly ONE root is indexed -- the first, the
# same one the hooks wire as MEMCONTINUUM_CODE_ROOT. `code-reindex` is
# single-root by construction: it deletes every stored path it did not see
# under the root it was given, and overwrites code_meta.code_root. Looping
# it over several roots therefore left only the LAST root indexed, having
# quietly deleted the earlier ones' rows on the way -- an install that
# reported success while throwing most of its own work away. The honest
# thing with several roots is to index one and SAY which were not indexed.
CODE_REINDEX_RAN=0
CODE_REINDEX_RC=0
CODE_REINDEX_OUT=""
if [ "${#CODE_ROOTS_ABS[@]}" -gt 0 ] && [ -n "$CHOSEN_LANGS" ]; then
    CODE_REINDEX_RAN=1
    CODE_REINDEX_ROOT="${CODE_ROOTS_ABS[0]}"
    CODE_REINDEX_CMD=("$PYTHON_BIN" "$MEMIDX" code-reindex --code-root "$CODE_REINDEX_ROOT" --project "$PROJECT" --lang "$CHOSEN_LANGS" --no-embed)
    step "code-reindex ($CODE_REINDEX_ROOT): PYTHONPATH= ${CODE_REINDEX_CMD[*]}"
    if [ "${#CODE_ROOTS_ABS[@]}" -gt 1 ]; then
        echo
        echo "*** NOTE: only the first code root is indexed ***"
        echo "    indexed     : $CODE_REINDEX_ROOT"
        i=0
        for cr in "${CODE_ROOTS_ABS[@]}"; do
            if [ "$i" -gt 0 ]; then
                echo "    NOT indexed : $cr"
            fi
            i=$((i + 1))
        done
        echo "    The code index holds one root per project: only the first code"
        echo "    root is indexed, the others are not. Code under the roots above"
        echo "    is NOT searchable via code-search, though a new file written"
        echo "    there still gets the new-file reminder."
        echo
    fi
    if [ "$DRY_RUN" -eq 0 ]; then
        CODE_REINDEX_OUT="$(PYTHONPATH= "${CODE_REINDEX_CMD[@]}" 2>&1)"
        CODE_REINDEX_RC=$?
    fi
elif [ "${#CODE_ROOTS_ABS[@]}" -gt 0 ]; then
    step "code-reindex: skipped (language-less wiring, no languages chosen)"
fi

# --- summary -------------------------------------------------------------

echo
echo "=== Verification summary ==="
if [ "$DRY_RUN" -eq 1 ]; then
    echo "Dry run only -- nothing was written."
else
    if [ "$STORE_IS_ADOPTED" -eq 1 ]; then
        echo "Store install  : adopted (--store was already a git repo carrying this tool's markers)"
    else
        echo "Store install  : fresh seed"
    fi
    echo "Store tree     : $STORE (${STORE_DIRS[*]})"
    echo "Store README   : $STORE/README.md"
    echo "Store git repo : $STORE/.git"
    echo "Settings file  : $CLAUDE_DIR/settings.local.json"
    echo "Rules file     : $RULES_DEST (rendered by $RENDERED_SHA)"
    echo "Skill installed: $CLAUDE_DIR/skills/memory-search/SKILL.md"
    if [ -n "${STORE_HOOKS_DIR:-}" ] && [ -f "$STORE_HOOKS_DIR/post-commit" ]; then
        echo "Post-commit    : $STORE_HOOKS_DIR/post-commit (wraps $HOOKS_DIR/post-commit-reindex.sh)"
    fi
    echo "Reindex        : rc=$REINDEX_RC"
    [ -n "$REINDEX_OUT" ] && echo "$REINDEX_OUT" | sed 's/^/  /'
    echo "Lint           : rc=$LINT_RC"
    [ -n "$LINT_OUT" ] && echo "$LINT_OUT" | sed 's/^/  /'
    if [ "$CODE_REINDEX_RAN" -eq 1 ]; then
        echo "Code reindex   : rc=$CODE_REINDEX_RC (languages: $CHOSEN_LANGS)"
        [ -n "$CODE_REINDEX_OUT" ] && echo "$CODE_REINDEX_OUT" | sed 's/^/  /'
    fi
    # fix-round-4 F10: for an ADOPTED store, memlint findings are reported
    # here (above) but do not fail the install -- they reflect PRE-EXISTING
    # content the adopt, not this installer, introduced (the motivating case:
    # legacy duplicate ids memlint's duplicate-id check now promotes to an
    # error). A freshly SEEDED store still hard-fails on any lint error --
    # nothing but this installer's own templates could have put one there.
    if [ "$LINT_RC" -ne 0 ] && [ "$STORE_IS_ADOPTED" -eq 1 ]; then
        echo
        echo "NOTE: adopted store has memlint findings above (rc=$LINT_RC) -- not fatal for an adopted store (pre-existing content, not this install); fix at your convenience with: PYTHONPATH= $PYTHON_BIN $MEMLINT $STORE"
    fi

    if [ "$REINDEX_RC" -ne 0 ]; then
        fail "reindex failed (rc=$REINDEX_RC) -- see output above" 7
    fi
    if [ "$LINT_RC" -ne 0 ] && [ "$STORE_IS_ADOPTED" -eq 0 ]; then
        fail "memlint reported errors (rc=$LINT_RC) -- see output above" 8
    fi
    if [ "$CODE_REINDEX_RC" -ne 0 ]; then
        fail "code-reindex failed (rc=$CODE_REINDEX_RC) -- see output above" 13
    fi
fi

# --- 8. record the decision (D2, updater workstream) -----------------------
#
# Reaching here means the install above succeeded (every failure path
# through step 7/7b already called fail(), which exits immediately) --
# --record-decision, when given, records "wired" the same way a human
# running memcontinuum-decide.sh by hand would. Off by default: recording a
# consent nobody gave is the one thing this system must never do -- see
# README.md's "A hook reports a state; only the skill records a decision"
# doctrine. This is not a hook; it is this installer's OWN successful
# completion, gated on a flag a driven flow only ever passes after a human
# has already said yes.
if [ "$DRY_RUN" -eq 0 ] && [ "$RECORD_DECISION" -eq 1 ]; then
    # mc-registry-lib.sh is sourced at the top of this script (it is a hard
    # requirement now, not an optional extra for this one block).
        if mc_repo_key "$(dirname "$CLAUDE_DIR")"; then
            RD_REPO="$MC_REPO"
            RD_KEY="$MC_REPO_KEY"
            mc_resolve_home
            RD_DECISIONS="$MEMCONTINUUM_HOME/decisions.tsv"
            RD_CLAUDE_DIRS="$CLAUDE_DIR"
            RD_CODE_ROOTS=""
            for cr in "${CODE_ROOTS_ABS[@]:-}"; do
                [ -n "$cr" ] && RD_CODE_ROOTS="${RD_CODE_ROOTS:+$RD_CODE_ROOTS;}$cr"
            done
            RD_LANGS="$(printf '%s' "$CHOSEN_LANGS" | tr ',' ';')"
            RD_NEVER="$(printf '%s' "$NEVER_EXTS_RAW" | tr ',' ';')"
            # Union with whatever a pre-existing row already recorded --
            # never dropped (INC-0104's own lesson: a project's SECOND
            # claude-dir/code-root/language must not erase its first).
            RD_DECLINED=0
            if mc_registry_lookup "$RD_DECISIONS" "$RD_KEY"; then
                # A `declined` row is a human's "no". Only an UNDECIDED repo
                # may be recorded as wired by an installer -- reversing a
                # decline is `memcontinuum-decide.sh forget`, typed by the
                # person who declined. The install itself still succeeds and
                # still exits 0: the wiring is real, it is only the REGISTRY
                # that keeps the answer already on record. (Nothing is
                # silently half-done here -- the detector reads the row, so
                # the repo stays exactly as quiet as it was asked to be.)
                if [ "$MC_LOOKUP_DECISION" = "declined" ]; then
                    RD_DECLINED=1
                fi
                mc_note_field "$MC_LOOKUP_NOTE" "claude-dirs"
                RD_CLAUDE_DIRS="$(mc_union_semi "$MC_NOTE_FIELD" "$RD_CLAUDE_DIRS")"
                mc_note_field "$MC_LOOKUP_NOTE" "code-roots"
                RD_CODE_ROOTS="$(mc_union_semi "$MC_NOTE_FIELD" "$RD_CODE_ROOTS")"
                mc_note_field "$MC_LOOKUP_NOTE" "langs"
                RD_LANGS="$(mc_union_semi "$MC_NOTE_FIELD" "$RD_LANGS")"
                mc_note_field "$MC_LOOKUP_NOTE" "never"
                RD_NEVER="$(mc_union_semi "$MC_NOTE_FIELD" "$RD_NEVER")"
            fi
            if [ "$RD_DECLINED" -eq 1 ]; then
                echo "note: --record-decision given, but $RD_KEY is recorded as declined -- leaving that answer alone. Only an undecided repo may be recorded as wired by the installer; if the decision has changed, run: $SCRIPT_DIR/memcontinuum-decide.sh forget --repo $RD_REPO   (then record it again)" >&2
            else
                declare -a RD_ARGS=(wired --repo "$RD_REPO" --store "$STORE" --project "$PROJECT")
                mc_split_semi "$RD_CLAUDE_DIRS"
                for d in ${MC_SPLIT[@]+"${MC_SPLIT[@]}"}; do RD_ARGS+=(--claude-dir "$d"); done
                # mc_build_wiring_args (scripts/mc-registry-lib.sh): the one
                # builder for the --code-root/--langs/--never-ext tail, shared
                # with scripts/memcontinuum-update.sh.
                mc_build_wiring_args "$RD_CODE_ROOTS" \
                    "$(printf '%s' "$RD_LANGS" | tr ';' ',')" \
                    "$(printf '%s' "$RD_NEVER" | tr ';' ',')"
                RD_ARGS+=(${MC_BUILT_ARGS[@]+"${MC_BUILT_ARGS[@]}"})
                if "$MC_BASH_BIN" "$SCRIPT_DIR/memcontinuum-decide.sh" "${RD_ARGS[@]}"; then
                    echo "decision recorded: $RD_KEY wired"
                else
                    echo "note: --record-decision given but recording failed (see above) -- the install itself still succeeded" >&2
                fi
            fi
        else
            echo "note: --record-decision given but $CLAUDE_DIR is not inside a git working tree -- skipping" >&2
        fi
fi

echo
echo "=== Next steps ==="
if [ "$DRY_RUN" -eq 1 ]; then
    echo "This was a dry run. Re-run without --dry-run to actually install."
else
    cat <<EOF
1. Restart the Claude Code session (or /clear) so the new hooks in
   $CLAUDE_DIR/settings.local.json take effect.
2. Write your first topic: create
     $STORE/topics/<area>/<slug>.md
   following the frontmatter shape in $ENGINE_ROOT/docs/SCHEMA.md (type: topic,
   id, title, area, code_refs, links: [...]), then reindex:
     PYTHONPATH= $PYTHON_BIN $MEMIDX reindex --root $STORE --project $PROJECT
   (the store's post-commit hook does this automatically after a commit).
3. Uninstall: remove the seven hook entries below from
     $CLAUDE_DIR/settings.local.json
   (identified by these script basenames in their "command" fields --
   safe to hand-delete, or restore $CLAUDE_DIR/settings.local.json.bak-memcontinuum):
     $OUR_HOOK_SCRIPTS
   then delete:
     $CLAUDE_DIR/skills/memory-search/
     $RULES_DEST
     ${STORE_HOOKS_DIR:-$STORE/.git/hooks}/post-commit
     ~/.memcontinuum/$PROJECT.sqlite
   (leave $STORE itself alone -- it is the store's own git history).
EOF
fi
