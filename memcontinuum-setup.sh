#!/usr/bin/env bash
# memcontinuum-setup.sh -- MemContinuum machine setup. Run ONCE per machine, from this
# checkout. Everything after this is per-repository and goes through the
# `memcontinuum` skill: the SessionStart detector flags an undecided repo
# to the assistant, advisory only -- /memcontinuum is the supported way to
# actually decide.
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
#   --uninstall       remove the user-level hook, the skill, and this
#                     machine's config. Never touches a venv, a store, any
#                     per-repo wiring, or your recorded per-repo answers.
#
# With neither --python nor --venv given on a real terminal, this blocks on
# /dev/tty with a python-or-venv menu (choosing abort exits 1) instead of
# silently creating a venv; a scripted or non-interactive run is unaffected.
# The menu takes 1, 2 or 3 and nothing else: any other answer is asked again,
# three times, and then the run aborts having written nothing.
#
# Why the model warm is on by default: fastembed downloads ~100 MB the first
# time anything needs to embed. Left lazy, that download happens inside
# somebody's first reindex -- or worse, inside a hook -- looking like a hang.
# Better to pay it here, visibly, once.
#
# MEMCONTINUUM_HOME: where this machine's config, index databases, session
# state and hook log live. Defaults to $HOME/.memcontinuum. Set it in the
# environment before running this script to put them somewhere else; every
# command finds them from there afterwards, with no per-repo configuration.
# Keep it on a local, POSIX filesystem -- never a synced or cloud drive.
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
# Sourced for mc_render_fingerprint only (the library defines functions and
# nothing else when sourced). The completeness of this checkout is checked
# properly further down, alongside the other required files; a missing library
# here just means the machine layer renders with an "unknown" stamp, which
# reads downstream as "cannot tell, re-render to find out".
# shellcheck source=scripts/mc-registry-lib.sh
. "$SCRIPT_DIR/scripts/mc-registry-lib.sh" 2>/dev/null || MC_RENDER_FINGERPRINT="unknown"

VENV_DIR=""
PYTHON_BIN=""
CLAUDE_DIR="$HOME/.claude"
MODEL_WARM=1
DRY_RUN=0
UNINSTALL=0
# Whether --python/--venv were given explicitly on THIS invocation, as
# opposed to left for this step to resolve or create -- read by the
# MEMCONTINUUM_VENV_MANAGED determination below (Task 9) and by the
# interactive setup menu, which is skipped whenever either was given.
PYTHON_EXPLICIT=0
VENV_EXPLICIT=0

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
        --venv) need_value "$@"; VENV_DIR="$2"; VENV_EXPLICIT=1; shift 2 ;;
        --python) need_value "$@"; PYTHON_BIN="$2"; PYTHON_EXPLICIT=1; shift 2 ;;
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
[ -n "$BOOT_PY" ] || die "no python3 on PATH -- MemContinuum needs Python 3.12+ (see README.md Requirements)"

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
# The MACHINE render fingerprint, on the one hook line this layer renders --
# the same idea scripts/repo-init.sh stamps its per-repo lines with, and the
# only way scripts/memcontinuum-update.sh --machine can tell whether what is
# installed in ~/.claude is still current. It is the MACHINE scope
# deliberately: this layer is re-rendered by a different command than a
# repository's wiring, and a template change (which no per-machine re-run
# would fix) must not show up here, any more than an edit to this file should
# mark every wired repository stale. `unknown` when it cannot be computed --
# read as "re-render to find out", never as an error.
if command -v mc_render_fingerprint >/dev/null 2>&1; then
    mc_render_fingerprint machine "$SCRIPT_DIR" || :
fi
HOOK_CMD="MEMCONTINUUM_RENDERED=${MC_RENDER_FINGERPRINT:-unknown} bash $(sh_quote "$DETECT_HOOK")"

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

# ---------------------------------------------------------------------------
# Interactive setup menu (Task 9): a truly fresh, unscripted run -- no
# --python, no --venv, a real terminal on stdin -- asks rather than silently
# picking "create a venv" the way every other run (scripted, CI, or already
# told what to do) does. Additive only: guarded exactly like
# scripts/repo-init.sh's own census dialogue ([ -t 0 ]), so a non-interactive
# or explicit-flag run never reaches it and keeps today's
# silent-reuse-or-create behavior untouched.
#
# Choosing "1) use this python" is itself an explicit python choice, same as
# typing --python on the command line -- it sets PYTHON_EXPLICIT so the
# MEMCONTINUUM_VENV_MANAGED determination below treats it the same way (not
# managed, unless a later run re-affirms this exact path).
# ---------------------------------------------------------------------------
if [ "$PYTHON_EXPLICIT" -eq 0 ] && [ "$VENV_EXPLICIT" -eq 0 ] && [ -t 0 ] && [ "$DRY_RUN" -eq 0 ]; then
    echo
    echo "Set up MemContinuum's python environment:"
    echo "  1) use this python ($BOOT_PY)"
    echo "  2) create or reuse the engine venv"
    echo "  3) abort"
    # Only 1, 2 and 3 are answers. Anything else -- a typo, a stray word, a
    # bare Enter -- is asked again, up to three times, and then the run
    # aborts without touching anything (reviewer finding: every response but
    # 1 and 3 counted as 2, so a mistyped answer created a venv and
    # installed dependencies the person never agreed to; a bare Enter did
    # the same, and nothing documents Enter as meaning anything). An abort
    # here is safe by construction: nothing has been written yet.
    MENU_TRIES=0
    MENU_ANSWERED=0
    while [ "$MENU_TRIES" -lt 3 ]; do
        MENU_TRIES=$((MENU_TRIES + 1))
        printf '> '
        MENU_CHOICE=""
        read -r MENU_CHOICE < /dev/tty || MENU_CHOICE=""
        case "$MENU_CHOICE" in
            1) PYTHON_BIN="$BOOT_PY"; PYTHON_EXPLICIT=1; MENU_ANSWERED=1 ;;
            2) MENU_ANSWERED=1 ;;   # the create-or-reuse path below
            3) echo "aborted" >&2; exit 1 ;;
            *) echo "answer 1, 2 or 3" >&2 ;;
        esac
        if [ "$MENU_ANSWERED" -eq 1 ]; then
            break
        fi
    done
    if [ "$MENU_ANSWERED" -eq 0 ]; then
        echo "no answer of 1, 2 or 3 after $MENU_TRIES attempts -- aborted" >&2
        exit 1
    fi
fi

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
# the pinned dependency set (numpy 2.5.2's own wheels ship for 3.12+). Skipped
# under --dry-run when the venv doesn't exist yet.
if [ "$DRY_RUN" -eq 0 ] || [ -x "$PYTHON_BIN" ]; then
    env PYTHONPATH= "$PYTHON_BIN" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)' \
        || die "$PYTHON_BIN is older than Python 3.12 (the pinned dependency set requires it) -- see README.md Requirements"
    plan "python is $(env PYTHONPATH= "$PYTHON_BIN" -c 'import sys; print(".".join(map(str, sys.version_info[:3])))')"
fi

# Whether THIS python counts as engine-managed (Task 9) -- read back by
# scripts/memcontinuum-update.sh --machine to decide whether it may reinstall
# requirements.lock into it on a stale-fingerprint refresh, or must instead
# leave a foreign python alone and only report what is missing.
#
# Default managed=1: PYTHON_BIN was created or reused by THIS step with no
# explicit --python (the ordinary case -- an engine-owned venv). An explicit
# --python (including the interactive menu's "use this python", which sets
# PYTHON_EXPLICIT the same way) starts unmanaged (0) UNLESS it re-affirms the
# EXACT SAME path config.sh already recorded as managed=1 -- the STICKY case:
# scripts/memcontinuum-update.sh --machine always re-resolves and re-passes a
# python explicitly on every run after the first (SETUP_ARGS --python "$PY"),
# so without this stickiness an engine-created venv would read managed=0 on
# its own SECOND refresh and reconciliation could never fire again. Only a
# GENUINELY new or different explicit path resets it to 0.
NEW_VENV_MANAGED=1
if [ "$PYTHON_EXPLICIT" -eq 1 ]; then
    NEW_VENV_MANAGED=0
    # mc_config_managed_python (scripts/mc-registry-lib.sh) is the ONE
    # implementation of this read, shared with scripts/memcontinuum-update.sh
    # --machine's reconciliation, which needs exactly the same disk truth for
    # exactly the same reason. See that function for why the read unsets both
    # names first. The source line near the top of this script tolerates a
    # missing library elsewhere (it only costs the machine-layer fingerprint
    # stamp), but stickiness cannot: silently skipping this read on an
    # incomplete checkout would make an engine-created venv read unmanaged on
    # its own SECOND refresh, and reconciliation could then never fire again
    # -- exactly the failure stickiness exists to prevent (whole-branch review
    # NEW-4). Required here, loudly, rather than degraded.
    type mc_config_managed_python >/dev/null 2>&1 \
        || die "scripts/mc-registry-lib.sh did not load -- incomplete checkout, cannot determine managed-venv stickiness"
    if mc_config_managed_python "$MEMCONTINUUM_HOME/config.sh"; then
        if [ "$MC_CONFIG_PYTHON" = "$PYTHON_BIN" ] && [ "$MC_CONFIG_MANAGED" = "1" ]; then
            NEW_VENV_MANAGED=1   # re-affirming a venv THIS engine already owns, not a foreign path
        fi
    fi
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
    case "$SCRIPT_DIR$PYTHON_BIN$MEMCONTINUUM_HOME$CLAUDE_DIR" in
        *"$MC_NL"*) die "engine, python, MEMCONTINUUM_HOME, or claude-dir path contains a newline -- unsupported" ;;
    esac
    Q_ENGINE="$(sh_quote "$SCRIPT_DIR")"
    Q_PYTHON="$(sh_quote "$PYTHON_BIN")"
    Q_HOME="$(sh_quote "$MEMCONTINUUM_HOME")"
    Q_CLAUDE_DIR="$(sh_quote "$CLAUDE_DIR")"
    Q_VENV_MANAGED="$(sh_quote "$NEW_VENV_MANAGED")"
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
# Which user-level Claude Code directory this setup installed the detector
# hook and the memcontinuum skill into. Read by scripts/memcontinuum-update.sh
# --machine, which would otherwise assume ~/.claude -- and, on a machine set
# up with --claude-dir, report the real install as absent and render a second
# one at the default path.
MEMCONTINUUM_MACHINE_CLAUDE_DIR=$Q_CLAUDE_DIR
# Whether MEMCONTINUUM_PYTHON above is a venv this engine created and may
# reinstall requirements.lock into on a later --machine refresh (1), or a
# python this setup was told to use as-is and must never pip-install into
# (0). See this file's own determination above.
MEMCONTINUUM_VENV_MANAGED=$Q_VENV_MANAGED
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
say "Run /memcontinuum inside a repository to decide for it. An undecided"
say "repo also makes the SessionStart detector emit a note into the"
say "assistant's context -- advisory only, and it may never reach you."
say "Whatever you answer through the skill is recorded and reversible."
say ""
say "  status of the repo you are in:  scripts/memcontinuum-state.sh"
say "  never ask me anywhere:          scripts/memcontinuum-decide.sh never-ask"
say "  undo all of the above:           memcontinuum-setup.sh --uninstall"
