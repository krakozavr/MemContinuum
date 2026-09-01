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

OUR_HOOK_SCRIPTS="pre-edit-chain.sh newfile-nudge.sh ledger-post-edit.sh precompact-persist.sh sessionstart-remind.sh userprompt-remind.sh sessionend-stamp.sh"

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
declare -a CODE_ROOTS=()

usage() {
    cat <<'USAGE'
Usage: repo-init.sh --project NAME [--store DIR] [--code-root DIR ...]
                   [--claude-dir DIR] [--python PATH]
                   [--bootstrap-venv [DIR]] [--dry-run] [--force]

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
                      store with no associated code checkout (retrieval-only
                      / rationale-only install).
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
  --dry-run           print everything this script would do; write nothing
                      (except --bootstrap-venv's venv, see above).
  --force             allow --store to sit inside another git repo's
                      tracked working tree (normally refused).
  -h, --help          this text.

Without --python or --bootstrap-venv, the python to run memidx.py/memlint.py
with is resolved in this order: $MEMCONTINUUM_PYTHON (env) -> <this
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

# is_git_repo DIR -- true iff DIR is a git working tree, root OR linked
# worktree (fix-round-4 R8). A linked worktree (`git worktree add`) has a
# .git FILE (a "gitdir: <path>" pointer), not a directory -- every prior
# `[ -d "$STORE/.git" ]` check in this script misread that as "not a git
# repo at all", refusing a worktree store outright (or, under --force,
# seeding fresh content on top of one). `[ -e ]` accepts either shape;
# `rev-parse --is-inside-work-tree` confirms it is actually a working tree
# (not, say, some unrelated directory that merely happens to contain a
# file or dir named .git) before this counts as a real answer.
is_git_repo() {
    [ -e "$1/.git" ] || return 1
    git -C "$1" rev-parse --is-inside-work-tree >/dev/null 2>&1
}

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
# or returns 1 with nothing printed. Order: $MEMCONTINUUM_PYTHON env, then
# <this checkout>/.venv/bin/python. Never consults --python (the caller
# checks that first, since it's an explicit override, not a fallback).
resolve_python() {
    if [ -n "${MEMCONTINUUM_PYTHON:-}" ]; then
        printf '%s' "$MEMCONTINUUM_PYTHON"
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
        fail "no python found: set \$MEMCONTINUUM_PYTHON, pass --python PATH, or run '$0 --bootstrap-venv [DIR]' to create one (see README.md's Requirements section)" 3
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

# store dir must not already live inside a DIFFERENT git repo's working
# tree, unless it is already its own repo (the normal re-run case, a
# linked worktree included -- R8) or --force was given.
if ! is_git_repo "$STORE" && [ "$FORCE" -eq 0 ]; then
    ANCESTOR="$(nearest_existing_ancestor "$STORE")"
    if OUTER_TOPLEVEL="$(git -C "$ANCESTOR" rev-parse --show-toplevel 2>/dev/null)"; then
        fail "--store $STORE is inside an existing git repo's tracked tree ($OUTER_TOPLEVEL) -- pass --force to install anyway, or pick a --store outside it" 4
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
    STORE_HAS_SHAPE=0
    for d in topics incidents concepts; do
        [ -d "$STORE/$d" ] && STORE_HAS_SHAPE=1
    done
    if [ "$STORE_HAS_SHAPE" -eq 0 ] && [ -f "$STORE/README.md" ]; then
        case "$(cat "$STORE/README.md" 2>/dev/null)" in
            *MemContinuum*) STORE_HAS_SHAPE=1 ;;
        esac
    fi
    if [ "$STORE_HAS_SHAPE" -eq 0 ]; then
        fail "--store $STORE is an existing git repo with none of this tool's markers (no topics/incidents/concepts directory, no README mentioning MemContinuum) -- refusing to seed store directories and a replacement post-commit hook into what looks like an unrelated repo. Point --store at a location that does not exist yet, or at an existing MemContinuum store." 9
    fi
    STORE_IS_ADOPTED=1
fi

# claude-dir (and store's parent) must be writable.
STORE_PARENT_ANCESTOR="$(nearest_existing_ancestor "$(dirname "$STORE")")"
[ -w "$STORE_PARENT_ANCESTOR" ] || fail "cannot write under $(dirname "$STORE") (nearest existing ancestor $STORE_PARENT_ANCESTOR is not writable)" 5

CLAUDE_DIR_ANCESTOR="$(nearest_existing_ancestor "$CLAUDE_DIR")"
[ -w "$CLAUDE_DIR_ANCESTOR" ] || fail "cannot write --claude-dir $CLAUDE_DIR (nearest existing ancestor $CLAUDE_DIR_ANCESTOR is not writable)" 5

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

# Fix-round-4 F8: the ONE settings merge implementation, shared with
# memcontinuum-setup.sh -- see scripts/mc_settings_merge.py's own docstring.
sys.path.insert(0, os.environ["MC_INSTALL_SCRIPTS_DIR"])
from mc_settings_merge import merge_settings, MergeRefused, basenames_identity

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

write_tmpl = read_tmpl("write-hooks.json.tmpl")
write_rendered = render(write_tmpl, {
    "STORE": esc_cmd(store),
    "PROJECT": esc_cmd(project),
    "PYTHON": esc_cmd(python_bin),
    "HOOKS_DIR": esc_cmd(hooks_dir),
    "CODE_ROOT_ENV": code_root_env,
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

# --- 5. install the memory-search skill -------------------------------

step "install skill: $CLAUDE_DIR/skills/memory-search/SKILL.md"
if [ "$DRY_RUN" -eq 0 ]; then
    mkdir -p "$CLAUDE_DIR/skills/memory-search" || fail "could not create $CLAUDE_DIR/skills/memory-search"
    cp -f "$SKILL_SRC" "$CLAUDE_DIR/skills/memory-search/SKILL.md" || fail "could not copy SKILL.md"
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
    echo "Skill installed: $CLAUDE_DIR/skills/memory-search/SKILL.md"
    if [ -n "${STORE_HOOKS_DIR:-}" ] && [ -f "$STORE_HOOKS_DIR/post-commit" ]; then
        echo "Post-commit    : $STORE_HOOKS_DIR/post-commit (wraps $HOOKS_DIR/post-commit-reindex.sh)"
    fi
    echo "Reindex        : rc=$REINDEX_RC"
    [ -n "$REINDEX_OUT" ] && echo "$REINDEX_OUT" | sed 's/^/  /'
    echo "Lint           : rc=$LINT_RC"
    [ -n "$LINT_OUT" ] && echo "$LINT_OUT" | sed 's/^/  /'
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
     ${STORE_HOOKS_DIR:-$STORE/.git/hooks}/post-commit
     ~/.memcontinuum/$PROJECT.sqlite
   (leave $STORE itself alone -- it is the store's own git history).
EOF
fi
