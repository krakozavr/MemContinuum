#!/usr/bin/env bash
# install.sh -- MemContinuum new-project installer.
#
# Creates a Rationale-store markdown tree at --store, wires the PreToolUse
# retrieval hook and the five write-side reminder hooks into a project's
# Claude Code settings, installs the memory-search skill, sets up the
# store's git post-commit reindex hook, and runs an initial reindex + lint.
#
# Usage:
#   install.sh --project NAME --store DIR [--code-root DIR ...]
#              [--claude-dir DIR] [--python PATH] [--bootstrap-venv [DIR]]
#              [--dry-run] [--force]
#
# See README.md "## Installing into a new project" for the full contract.
#
# Nothing in this script is specific to any one project: every path is a
# parameter (or a documented default), matching the project-agnostic
# contract the rest of this repo (memidx.py, memlint.py, hooks/*.sh)
# already follows.

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
HOOKS_DIR="$SCRIPT_DIR/hooks"
TEMPLATES_DIR="$SCRIPT_DIR/templates"
MEMIDX="$SCRIPT_DIR/memidx.py"
MEMLINT="$SCRIPT_DIR/memlint.py"
SKILL_SRC="$SCRIPT_DIR/skills/memory-search/SKILL.md"

OUR_HOOK_SCRIPTS="pre-edit-chain.sh ledger-post-edit.sh precompact-persist.sh sessionstart-remind.sh userprompt-remind.sh sessionend-stamp.sh"

PROJECT=""
STORE=""
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
Usage: install.sh --project NAME --store DIR [--code-root DIR ...]
                   [--claude-dir DIR] [--python PATH]
                   [--bootstrap-venv [DIR]] [--dry-run] [--force]

  --project NAME     project namespace (used for --project everywhere, and
                      as the index db filename <NAME>.sqlite). Required.
  --store DIR        the markdown store root to create/wire. Required.
  --code-root DIR     a code checkout the PreToolUse hook should watch for
                      Edit/Write and the write-side hooks should scope
                      ledger entries to. Repeatable. Optional -- omit for a
                      store with no associated code checkout (retrieval-only
                      / rationale-only install).
  --claude-dir DIR    where to merge hook wiring and install the skill.
                      Defaults to <dirname of --store>/.claude.
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

# resolve_python -- prints an absolute python path on stdout and returns 0,
# or returns 1 with nothing printed. Order: $MEMCONTINUUM_PYTHON env, then
# <this checkout>/.venv/bin/python. Never consults --python (the caller
# checks that first, since it's an explicit override, not a fallback).
resolve_python() {
    if [ -n "${MEMCONTINUUM_PYTHON:-}" ]; then
        printf '%s' "$MEMCONTINUUM_PYTHON"
        return 0
    fi
    if [ -x "$SCRIPT_DIR/.venv/bin/python" ]; then
        printf '%s' "$SCRIPT_DIR/.venv/bin/python"
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
    local req="$SCRIPT_DIR/requirements.txt"
    if [ ! -f "$req" ]; then
        echo "ERROR: requirements.txt not found next to install.sh: $req" >&2
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

while [ $# -gt 0 ]; do
    case "$1" in
        --project) PROJECT="${2:-}"; shift 2 ;;
        --store) STORE="${2:-}"; shift 2 ;;
        --code-root) CODE_ROOTS+=("${2:-}"); shift 2 ;;
        --claude-dir) CLAUDE_DIR="${2:-}"; shift 2 ;;
        --python) PYTHON_BIN="${2:-}"; PYTHON_BIN_EXPLICIT=1; shift 2 ;;
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
[ -n "$STORE" ] || { usage >&2; fail "--store is required" 2; }

case "$PROJECT" in
    */*|"") fail "--project must not contain '/' (got: $PROJECT)" 2 ;;
esac

# --- python resolution --------------------------------------------------
#
# --bootstrap-venv runs immediately (not gated on --dry-run): it is a tooling
# precondition, like the "does this python even run" check right below, not
# part of the install plan being previewed -- and every abspath() call from
# here on needs a real python regardless of --dry-run (unchanged from before
# this feature existed).

if [ "$BOOTSTRAP_VENV" -eq 1 ]; then
    VENV_DIR="${BOOTSTRAP_VENV_DIR:-$SCRIPT_DIR/.venv}"
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
[ -f "$MEMIDX" ] || fail "memidx.py not found next to install.sh: $MEMIDX" 3
[ -f "$MEMLINT" ] || fail "memlint.py not found next to install.sh: $MEMLINT" 3

STORE="$(abspath "$STORE")"
if [ -z "$CLAUDE_DIR" ]; then
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
# tree, unless it is already its own repo (the normal re-run case) or
# --force was given.
if [ ! -d "$STORE/.git" ] && [ "$FORCE" -eq 0 ]; then
    ANCESTOR="$(nearest_existing_ancestor "$STORE")"
    if OUTER_TOPLEVEL="$(git -C "$ANCESTOR" rev-parse --show-toplevel 2>/dev/null)"; then
        fail "--store $STORE is inside an existing git repo's tracked tree ($OUTER_TOPLEVEL) -- pass --force to install anyway, or pick a --store outside it" 4
    fi
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
echo "  engine dir  : $SCRIPT_DIR"
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
        README_TMPL="${README_TMPL//\{\{ENGINE_DIR\}\}/$SCRIPT_DIR}"
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

if [ -d "$STORE/.git" ]; then
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

OUR_SCRIPTS = [
    "pre-edit-chain.sh",
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


def is_ours(hook_item):
    cmd = hook_item.get("command", "")
    return any(name in cmd for name in OUR_SCRIPTS)


settings_path = os.path.join(claude_dir, "settings.local.json")
settings = {}
if os.path.isfile(settings_path):
    try:
        with open(settings_path, "r", encoding="utf-8") as f:
            raw = f.read()
        settings = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError as e:
        print("ERROR: existing %s is not valid JSON: %s" % (settings_path, e), file=sys.stderr)
        sys.exit(1)
    if not isinstance(settings, dict):
        print("ERROR: existing %s does not contain a JSON object at the top level" % settings_path, file=sys.stderr)
        sys.exit(1)

hooks_section = dict(settings.get("hooks", {}))

# Sweep our script-keyed items out of every event this installer ever
# writes to -- not just the events the CURRENT run happens to render.
# Otherwise re-running with fewer --code-roots than a previous run (e.g.
# down to zero) leaves stale PreToolUse entries behind: idempotency means
# "this run's config replaces the LAST run's", not "only touch what this
# run has something new to add."
ALL_EVENTS = {
    "PreToolUse", "PostToolUse", "PreCompact",
    "SessionStart", "UserPromptSubmit", "SessionEnd",
}

for event in ALL_EVENTS:
    existing_groups = list(hooks_section.get(event, []))
    kept_groups = []
    for group in existing_groups:
        group_hooks = group.get("hooks", [])
        filtered = [h for h in group_hooks if not is_ours(h)]
        if filtered:
            new_group = dict(group)
            new_group["hooks"] = filtered
            kept_groups.append(new_group)
        # else: group became empty (it was entirely ours) -- drop it.
    new_groups = blocks.get(event, [])
    merged = kept_groups + new_groups
    if merged:
        hooks_section[event] = merged
    elif event in hooks_section:
        del hooks_section[event]

settings["hooks"] = hooks_section

if dry_run:
    print("  (dry-run) would merge these hook entries into %s:" % settings_path)
    for event in blocks:
        print("    %s: +%d group(s), scripts: %s" % (
            event, len(blocks[event]),
            ", ".join(sorted({s for s in OUR_SCRIPTS if any(
                s in h.get("command", "") for g in blocks[event] for h in g.get("hooks", [])
            )})),
        ))
    sys.exit(0)

os.makedirs(claude_dir, exist_ok=True)

if os.path.isfile(settings_path):
    backup_path = settings_path + ".bak-memcontinuum"
    with open(settings_path, "r", encoding="utf-8") as src, open(backup_path, "w", encoding="utf-8") as dst:
        dst.write(src.read())

with open(settings_path, "w", encoding="utf-8") as f:
    json.dump(settings, f, indent=2)
    f.write("\n")

print("  wrote %s" % settings_path)
for event in blocks:
    print("    merged %d group(s) into hooks.%s" % (len(blocks[event]), event))
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

if [ -d "$STORE/.git" ]; then
    step "git post-commit reindex wrapper: $STORE/.git/hooks/post-commit -> $HOOKS_DIR/post-commit-reindex.sh"
    if [ "$DRY_RUN" -eq 0 ]; then
        POST_COMMIT="$STORE/.git/hooks/post-commit"
        {
            printf '#!/usr/bin/env bash\n'
            printf 'export MEMCONTINUUM_ROOT=%s\n' "$(printf '%q' "$STORE")"
            printf 'export MEMCONTINUUM_PROJECT=%s\n' "$(printf '%q' "$PROJECT")"
            printf 'export MEMCONTINUUM_PYTHON=%s\n' "$(printf '%q' "$PYTHON_BIN")"
            printf 'exec %s\n' "$(printf '%q' "$HOOKS_DIR/post-commit-reindex.sh")"
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
# `--no-embed` as install.sh's choice, so it does not read as an accident.

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
    echo "Store tree     : $STORE (${STORE_DIRS[*]})"
    echo "Store README   : $STORE/README.md"
    echo "Store git repo : $STORE/.git"
    echo "Settings file  : $CLAUDE_DIR/settings.local.json"
    echo "Skill installed: $CLAUDE_DIR/skills/memory-search/SKILL.md"
    if [ -f "$STORE/.git/hooks/post-commit" ]; then
        echo "Post-commit    : $STORE/.git/hooks/post-commit (wraps $HOOKS_DIR/post-commit-reindex.sh)"
    fi
    echo "Reindex        : rc=$REINDEX_RC"
    [ -n "$REINDEX_OUT" ] && echo "$REINDEX_OUT" | sed 's/^/  /'
    echo "Lint           : rc=$LINT_RC"
    [ -n "$LINT_OUT" ] && echo "$LINT_OUT" | sed 's/^/  /'

    if [ "$REINDEX_RC" -ne 0 ]; then
        fail "reindex failed (rc=$REINDEX_RC) -- see output above" 7
    fi
    if [ "$LINT_RC" -ne 0 ]; then
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
   following the frontmatter shape in $SCRIPT_DIR/docs/SCHEMA.md (type: topic,
   id, title, area, code_refs, links: [...]), then reindex:
     PYTHONPATH= $PYTHON_BIN $MEMIDX reindex --root $STORE --project $PROJECT
   (the store's post-commit hook does this automatically after a commit).
3. Uninstall: remove the six hook entries below from
     $CLAUDE_DIR/settings.local.json
   (identified by these script basenames in their "command" fields --
   safe to hand-delete, or restore $CLAUDE_DIR/settings.local.json.bak-memcontinuum):
     $OUR_HOOK_SCRIPTS
   then delete:
     $CLAUDE_DIR/skills/memory-search/
     $STORE/.git/hooks/post-commit
     ~/.memcontinuum/$PROJECT.sqlite
   (leave $STORE itself alone -- it is the store's own git history).
EOF
fi
