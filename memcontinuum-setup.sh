#!/usr/bin/env bash
# memcontinuum-setup.sh -- MemContinuum machine setup. Run ONCE per machine, from this
# checkout. Everything after this is per-repository and goes through the
# `memcontinuum` skill, which the human is asked about rather than expected to
# remember.
#
# The split, because it is the thing people get wrong:
#
#   memcontinuum-setup.sh   machine level. Creates the venv, records where the engine
#                  and its python live, and installs the USER-level pieces
#                  into ~/.claude -- the SessionStart detector hook and the
#                  `memcontinuum` skill. These apply in every repository,
#                  including ones that have never heard of this tool. That is
#                  what makes a repo's un-initialized state noticeable at all.
#
#   scripts/repo-init.sh     repository level. Creates one store and wires the seven
#                  working hooks into THAT project's .claude/settings.local.json.
#                  Run by the skill, after a human says yes.
#
# Usage:
#   memcontinuum-setup.sh [--venv DIR] [--python PATH] [--claude-dir DIR]
#                [--no-model-warm] [--dry-run] [--uninstall]
#
#   --venv DIR        where to create the venv. Default <checkout>/.venv, which
#                     is also the path every hook falls back to on its own.
#   --python PATH     use an existing python instead of creating a venv. It
#                     must already have requirements.txt installed.
#   --claude-dir DIR  user-level Claude Code directory. Default ~/.claude.
#   --no-model-warm   skip the one-time embedding-model download.
#   --dry-run         print the plan, write nothing.
#   --uninstall       remove the user-level hook + skill and BOTH config
#                     artifacts (see MEMCONTINUUM_HOME below). Never touches
#                     a venv, a store, or any per-repo wiring.
#
# Why the model warm is on by default: fastembed downloads ~100 MB the first
# time anything needs to embed. Left lazy, that download happens inside
# somebody's first reindex -- or worse, inside a hook -- looking like a hang.
# Better to pay it here, visibly, once.
#
# MEMCONTINUUM_HOME (fix-round-4 F7): when this env var is set to something
# other than the fixed default $HOME/.memcontinuum, config.sh is written
# there AS USUAL, but a second, minimal "pointer" config.sh is ALSO written
# at the fixed default path recording the real MEMCONTINUUM_HOME. The
# detector hook's installed command line does not bake MEMCONTINUUM_HOME in
# any more (a baked value and this registry used to disagree -- two
# registries, a decline that never silenced the ask); every consumer
# (detector, decide.sh, state.sh) now resolves it the same way: env ->
# pointer at the fixed default -> the fixed default itself. See
# scripts/mc-registry-lib.sh mc_resolve_home.
# --MC-USAGE-END--

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
DEFAULT_MEMCONTINUUM_HOME="$HOME/.memcontinuum"
# R4 fix, round 4: captured BEFORE the env-or-default fallback below so
# --uninstall can tell "no env var at all" (must still follow the pointer
# chain to find a custom-HOME install) apart from "env var equals the
# default" (nothing to follow).
MEMCONTINUUM_HOME_ENV_SET=0
[ -n "${MEMCONTINUUM_HOME:-}" ] && MEMCONTINUUM_HOME_ENV_SET=1
MEMCONTINUUM_HOME="${MEMCONTINUUM_HOME:-$DEFAULT_MEMCONTINUUM_HOME}"
CONFIG="$MEMCONTINUUM_HOME/config.sh"
POINTER_CONFIG="$DEFAULT_MEMCONTINUUM_HOME/config.sh"
DETECT_HOOK="$SCRIPT_DIR/hooks/memcontinuum-detect.sh"
SKILL_SRC="$SCRIPT_DIR/skills/memcontinuum/SKILL.md"

VENV_DIR=""
PYTHON_BIN=""
CLAUDE_DIR="$HOME/.claude"
MODEL_WARM=1
DRY_RUN=0
UNINSTALL=0

# scan to the explicit end marker above rather than a hardcoded line count --
# a hardcoded `sed -n '2,Np'` silently truncates or overruns usage() every
# time this header is edited (fix-round-4 finding, same as decide.sh).
usage() {
    sed -n '2,/^# --MC-USAGE-END--$/p' "$0" | grep -v '^# --MC-USAGE-END--$' | sed 's/^# \{0,1\}//'
    exit "${1:-1}"
}

# A two-argument option with no value must ERROR, not loop: a failed
# `shift 2` leaves the argument in place and spins forever (reviewer
# finding, reproduced).
need_value() { [ $# -ge 2 ] || { printf 'missing value for %s\n' "$1" >&2; exit 2; }; }
while [ $# -gt 0 ]; do
    case "$1" in
        --venv) need_value "$@"; VENV_DIR="$2"; shift 2 ;;
        --python) need_value "$@"; PYTHON_BIN="$2"; shift 2 ;;
        --claude-dir) need_value "$@"; CLAUDE_DIR="$2"; shift 2 ;;
        --no-model-warm) MODEL_WARM=0; shift ;;
        --dry-run) DRY_RUN=1; shift ;;
        --uninstall) UNINSTALL=1; shift ;;
        -h|--help) usage 0 ;;
        *) echo "unknown argument: $1" >&2; usage 1 ;;
    esac
done

say() { printf '%s\n' "$*"; }
plan() { if [ "$DRY_RUN" -eq 1 ]; then printf 'would: %s\n' "$*"; else printf '  %s\n' "$*"; fi; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

# A python that can run the JSON merge below. During --uninstall, and before
# the venv exists, this is whatever system python3 is on PATH.
BOOT_PY="$(command -v python3 2>/dev/null)"
[ -n "$BOOT_PY" ] || die "no python3 on PATH -- MemContinuum needs Python 3.10+ (see README.md Requirements)"

# ---------------------------------------------------------------------------
# settings.json merge, via the ONE shared implementation (fix-round-4 F8):
# scripts/mc_settings_merge.py, also used by scripts/repo-init.sh for the
# seven per-project hooks. Ours is identified by the detector script's
# basename appearing in a hook item's command (bare-needle -- this is the
# one machine-wide detector entry, no per-project concept) -- the same
# reasoning scripts/repo-init.sh's project-aware rule follows: it survives
# the user editing paths or reordering groups, and it drops only our own
# items, never a foreign hook that happens to share a group. The command
# line is passed as a plain argv value (never assembled into JSON in bash)
# so a checkout path with a quote or a space needs no shell-side escaping.
# ---------------------------------------------------------------------------
MC_SETTINGS_MERGE="$SCRIPT_DIR/scripts/mc_settings_merge.py"
merge_settings() {
    local settings="$1" mode="$2" command_line="${3:-}"
    local add_json="{}"
    if [ "$mode" = "install" ]; then
        add_json="$(env PYTHONPATH= "$BOOT_PY" -c '
import json, sys
print(json.dumps({"SessionStart": [{"hooks": [{"type": "command", "command": sys.argv[1]}]}]}))
' "$command_line")" || return 1
    fi
    env PYTHONPATH= "$BOOT_PY" "$MC_SETTINGS_MERGE" "$settings" \
        --events SessionStart --needle memcontinuum-detect.sh --add "$add_json"
}

SETTINGS="$CLAUDE_DIR/settings.json"
SKILL_DST_DIR="$CLAUDE_DIR/skills/memcontinuum"

# Single-quote a value for a shell command line (bash 3.2 safe). The hook
# command is run through a shell by Claude Code, so a checkout or home with a
# space (common on macOS) must stay one word; and it is invoked as
# `bash '<path>'` rather than by the path directly, so the executable bit --
# which a zip download or a core.filemode=false clone can drop -- is
# irrelevant to whether the machine layer works at all.
sh_quote() { printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"; }
# No MEMCONTINUUM_HOME= prefix any more (F7): a baked setup-time value here
# used to disagree with whatever decide.sh/state.sh resolved on their own --
# two registries, silently. The detector now resolves it itself, the same
# way, via scripts/mc-registry-lib.sh mc_resolve_home.
HOOK_CMD="bash $(sh_quote "$DETECT_HOOK")"

# ---------------------------------------------------------------------------
# Uninstall: user-level pieces only. A venv is shared with anything else that
# points at it; a store is its own git history. Neither is ours to delete.
# ---------------------------------------------------------------------------
if [ "$UNINSTALL" -eq 1 ]; then
    say "=== MemContinuum bootstrap --uninstall ==="
    # R4 fix, round 4: with NO MEMCONTINUUM_HOME in the environment,
    # MEMCONTINUUM_HOME above resolved to the fixed default -- but a
    # custom-HOME install left the REAL config.sh elsewhere and only a
    # pointer at that default path. Follow the same chain every consumer
    # uses (env -> pointer at the fixed default -> the fixed default
    # itself) before deciding what to remove, or this only ever deletes
    # the pointer and leaves the real config (and its artifacts) behind.
    if [ "$MEMCONTINUUM_HOME_ENV_SET" -eq 0 ] && [ -f "$POINTER_CONFIG" ]; then
        POINTED_HOME="$(. "$POINTER_CONFIG" 2>/dev/null; printf '%s' "${MEMCONTINUUM_HOME:-}")"
        if [ -n "$POINTED_HOME" ] && [ "$POINTED_HOME" != "$DEFAULT_MEMCONTINUUM_HOME" ]; then
            MEMCONTINUUM_HOME="$POINTED_HOME"
            CONFIG="$MEMCONTINUUM_HOME/config.sh"
        fi
    fi
    if [ -f "$SETTINGS" ]; then
        if [ "$DRY_RUN" -eq 1 ]; then
            plan "remove the detector hook from $SETTINGS (backup: $SETTINGS.bak-memcontinuum)"
        else
            merge_settings "$SETTINGS" remove >/dev/null || die "settings merge failed"
            plan "removed the detector hook from $SETTINGS"
        fi
    fi
    plan "remove $SKILL_DST_DIR"
    [ "$DRY_RUN" -eq 0 ] && rm -rf "$SKILL_DST_DIR"
    plan "remove $CONFIG"
    [ "$DRY_RUN" -eq 0 ] && rm -f "$CONFIG"
    # F7: remove the pointer config too, but ONLY if it points at THIS
    # MEMCONTINUUM_HOME -- an unrelated install's pointer at the same fixed
    # default path is not ours to delete.
    if [ "$MEMCONTINUUM_HOME" != "$DEFAULT_MEMCONTINUUM_HOME" ] && [ -f "$POINTER_CONFIG" ]; then
        POINTED_HOME="$(. "$POINTER_CONFIG" 2>/dev/null; printf '%s' "${MEMCONTINUUM_HOME:-}")"
        if [ "$POINTED_HOME" = "$MEMCONTINUUM_HOME" ]; then
            plan "remove pointer $POINTER_CONFIG"
            [ "$DRY_RUN" -eq 0 ] && rm -f "$POINTER_CONFIG"
        fi
    fi
    say ""
    say "Left alone on purpose: the venv, every store, every per-repo wiring,"
    say "and $MEMCONTINUUM_HOME/decisions.tsv (your answers, not an artifact)."
    exit 0
fi

say "=== MemContinuum bootstrap ==="
say "engine:     $SCRIPT_DIR"
say "home:       $MEMCONTINUUM_HOME"
say "claude dir: $CLAUDE_DIR"
say ""

[ -f "$DETECT_HOOK" ] || die "missing $DETECT_HOOK -- is this a complete checkout?"
[ -f "$SKILL_SRC" ] || die "missing $SKILL_SRC -- is this a complete checkout?"
[ -f "$MC_SETTINGS_MERGE" ] || die "missing $MC_SETTINGS_MERGE -- is this a complete checkout?"

# ---------------------------------------------------------------------------
# 1. python
# ---------------------------------------------------------------------------
say "1. python"
# Both paths end up serialized into config.sh and re-resolved from arbitrary
# cwds by every hook, so they must be ABSOLUTE -- a relative python that
# happens to work during setup would later resolve against each project's
# cwd and could run that project's unrelated .venv (reviewer finding).
case "${PYTHON_BIN:-/}" in /*) ;; *) die "--python must be an absolute path (got: $PYTHON_BIN)" ;; esac
case "${VENV_DIR:-/}" in /*) ;; *) die "--venv must be an absolute path (got: $VENV_DIR)" ;; esac
if [ -n "$PYTHON_BIN" ]; then
    [ -x "$PYTHON_BIN" ] || die "--python $PYTHON_BIN is not executable"
    plan "use existing python: $PYTHON_BIN"
else
    [ -n "$VENV_DIR" ] || VENV_DIR="$SCRIPT_DIR/.venv"
    PYTHON_BIN="$VENV_DIR/bin/python"
    if [ -x "$PYTHON_BIN" ]; then
        plan "reuse existing venv: $VENV_DIR"
    else
        plan "create venv at $VENV_DIR and install requirements.txt"
        if [ "$DRY_RUN" -eq 0 ]; then
            if command -v uv >/dev/null 2>&1; then
                uv venv "$VENV_DIR" || die "uv venv failed"
                VIRTUAL_ENV="$VENV_DIR" uv pip install -r "$SCRIPT_DIR/requirements.txt" \
                    || die "uv pip install failed"
            else
                env PYTHONPATH= "$BOOT_PY" -m venv "$VENV_DIR" || die "python3 -m venv failed"
                env PYTHONPATH= "$PYTHON_BIN" -m pip install --quiet --upgrade pip \
                    || die "pip upgrade failed"
                env PYTHONPATH= "$PYTHON_BIN" -m pip install --quiet -r "$SCRIPT_DIR/requirements.txt" \
                    || die "pip install -r requirements.txt failed"
            fi
        fi
    fi
fi

# Version gate AFTER resolution, against the python that will actually run the
# engine -- a 3.9 system python is fine for the hooks' own snippets but not for
# fastembed. Skipped under --dry-run when the venv doesn't exist yet.
if [ "$DRY_RUN" -eq 0 ] || [ -x "$PYTHON_BIN" ]; then
    env PYTHONPATH= "$PYTHON_BIN" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' \
        || die "$PYTHON_BIN is older than Python 3.10 (fastembed requires it) -- see README.md Requirements"
    plan "python is $(env PYTHONPATH= "$PYTHON_BIN" -c 'import sys; print(".".join(map(str, sys.version_info[:3])))')"
fi

# ---------------------------------------------------------------------------
# 2. embedding model
# ---------------------------------------------------------------------------
say ""
say "2. embedding model"
if [ "$MODEL_WARM" -eq 0 ]; then
    plan "skipped (--no-model-warm) -- the first reindex will download it instead"
else
    plan "download+cache the embedding model now (~100 MB, once)"
    if [ "$DRY_RUN" -eq 0 ]; then
        env PYTHONPATH= "$PYTHON_BIN" -c '
import sys
sys.path.insert(0, sys.argv[1])
import memidx
from fastembed import TextEmbedding
list(TextEmbedding(model_name=memidx.EMBED_MODEL_NAME).embed(["warm"]))
print("  model ready:", memidx.EMBED_MODEL_NAME)
' "$SCRIPT_DIR" || die "model warm failed -- rerun with --no-model-warm to skip"
    fi
fi

# ---------------------------------------------------------------------------
# 3. config
#
# One sourceable file, deliberately shell rather than JSON: hooks/memlib.sh
# reads it on every hook invocation and must not need python to do so.
#
# F7: MEMCONTINUUM_HOME is now recorded here too (it used to live only in the
# detector's baked-in hook line, which decide.sh/state.sh never read -- two
# registries, and a decline never silenced the ask). When MEMCONTINUUM_HOME
# is the fixed default, that's the whole story. When it's custom, a second,
# minimal pointer config.sh is ALSO written at the fixed default path, so a
# shell with no MEMCONTINUUM_HOME in its environment (every hook invocation,
# by construction) can still find the real one -- see mc_resolve_home in
# scripts/mc-registry-lib.sh.
# ---------------------------------------------------------------------------
say ""
say "3. config"
plan "write $CONFIG"
IS_CUSTOM_HOME=0
[ "$MEMCONTINUUM_HOME" = "$DEFAULT_MEMCONTINUUM_HOME" ] || IS_CUSTOM_HOME=1
[ "$IS_CUSTOM_HOME" -eq 1 ] && plan "write pointer $POINTER_CONFIG (MEMCONTINUUM_HOME is custom)"
if [ "$DRY_RUN" -eq 0 ]; then
    mkdir -p "$MEMCONTINUUM_HOME" || die "cannot create $MEMCONTINUUM_HOME"
    # config.sh is SOURCED by every hook, so its values are serialized with
    # sh_quote (single-quoted, embedded quotes escaped) -- a raw
    # double-quoted interpolation would let a backtick or $() in a path
    # execute on every hook invocation (reviewer finding). A newline in any
    # of the three cannot be quoted safely into a sourceable line at all, so
    # it is refused outright.
    # NB: $(printf '\n') would strip its own trailing newline and match
    # everything -- the ANSI-C quoted literal does not (bash 2.0+).
    MC_NL=$'\n'
    case "$SCRIPT_DIR$PYTHON_BIN$MEMCONTINUUM_HOME" in
        *"$MC_NL"*) die "engine, python, or MEMCONTINUUM_HOME path contains a newline -- unsupported" ;;
    esac
    Q_ENGINE="$(sh_quote "$SCRIPT_DIR")"
    Q_PYTHON="$(sh_quote "$PYTHON_BIN")"
    Q_HOME="$(sh_quote "$MEMCONTINUUM_HOME")"
    # Machine backup rule: never overwrite a config a previous setup wrote
    # without keeping a copy.
    [ -f "$CONFIG" ] && cp "$CONFIG" "$CONFIG.bak-memcontinuum"
    cat > "$CONFIG" <<CONF
# MemContinuum machine config -- written by memcontinuum-setup.sh $(date +%Y-%m-%d).
# Sourced by hooks/memlib.sh, scripts/memcontinuum-state.sh, -decide.sh, and
# hooks/memcontinuum-detect.sh (via scripts/mc-registry-lib.sh). Shell, not
# JSON, so a hook can read it without starting python. Values are
# single-quoted by sh_quote; do not hand-edit into double quotes.
MEMCONTINUUM_ENGINE=$Q_ENGINE
if [ -z "\${MEMCONTINUUM_PYTHON:-}" ]; then MEMCONTINUUM_PYTHON=$Q_PYTHON; fi
MEMCONTINUUM_HOME=$Q_HOME
CONF
    if [ "$IS_CUSTOM_HOME" -eq 1 ]; then
        mkdir -p "$DEFAULT_MEMCONTINUUM_HOME" || die "cannot create $DEFAULT_MEMCONTINUUM_HOME"
        [ -f "$POINTER_CONFIG" ] && cp "$POINTER_CONFIG" "$POINTER_CONFIG.bak-memcontinuum"
        cat > "$POINTER_CONFIG" <<CONF
# MemContinuum pointer config -- written by memcontinuum-setup.sh $(date +%Y-%m-%d)
# because MEMCONTINUUM_HOME was set to a non-default location at setup time.
# The real config lives at \$MEMCONTINUUM_HOME/config.sh; this file exists
# only so a shell with no MEMCONTINUUM_HOME in its environment can still find
# it. See mc_resolve_home in scripts/mc-registry-lib.sh.
MEMCONTINUUM_HOME=$Q_HOME
CONF
    fi
fi

# ---------------------------------------------------------------------------
# 4. user-level skill + detector hook
# ---------------------------------------------------------------------------
say ""
say "4. user-level install"
plan "copy skill -> $SKILL_DST_DIR/SKILL.md"
if [ "$DRY_RUN" -eq 0 ]; then
    mkdir -p "$SKILL_DST_DIR" || die "cannot create $SKILL_DST_DIR"
    # Machine backup rule: keep the previous installed copy before overwrite.
    [ -f "$SKILL_DST_DIR/SKILL.md" ] && cp "$SKILL_DST_DIR/SKILL.md" "$SKILL_DST_DIR/SKILL.md.bak-memcontinuum"
    cp "$SKILL_SRC" "$SKILL_DST_DIR/SKILL.md" || die "skill copy failed"
fi
plan "merge SessionStart detector into $SETTINGS"
plan "  command: $HOOK_CMD"
if [ "$DRY_RUN" -eq 0 ]; then
    merge_settings "$SETTINGS" install "$HOOK_CMD" >/dev/null || die "settings merge failed"
fi

say ""
say "=== done ==="
if [ "$DRY_RUN" -eq 1 ]; then
    say "(--dry-run: nothing was written)"
    exit 0
fi
say "From now on, every session that starts in a git repo with no recorded"
say "answer will prompt the assistant to ask you once whether that repo should"
say "keep a decision store. Answer either way and it is remembered."
say ""
say "  status of the repo you are in:  scripts/memcontinuum-state.sh"
say "  never ask me anywhere:          scripts/memcontinuum-decide.sh never-ask"
say "  undo all of the above:           memcontinuum-setup.sh --uninstall"
