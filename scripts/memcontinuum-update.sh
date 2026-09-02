#!/usr/bin/env bash
# usage: memcontinuum-update.sh [--dry-run | --apply] [--machine] [--repo PATH]
#        memcontinuum-update.sh --apply --repo PATH --claude-dir DIR [--claude-dir DIR ...]
#                               [--langs LIST] [--never-ext LIST]
#        memcontinuum-update.sh --add-lang LANG [--never-ext .ext] --repo PATH
#        memcontinuum-update.sh --never-ext .ext [--add-lang LANG] --repo PATH
#
# Engine/hook LOGIC updates for free (every rendered hook line runs a script
# from this checkout by absolute path, so `git pull` is a live update) --
# but everything RENDERED at install time (settings hook lines, the rules
# file, the installed skill copy) updates only when the installer is
# re-run, and until this command existed, nothing said so. Walks every
# `wired` row in the decision registry
# ($MEMCONTINUUM_HOME/decisions.tsv), and for each of that row's claude-dirs
# compares what was rendered there against the engine checkout right now.
#
# No flags (or --dry-run, the default): prints a table and writes nothing.
#   repo | claude-dir | stamped | engine | store-match | rules | action
#     repo         the registry row's key (an origin remote URL, or a path)
#     claude-dir   one claude-dir this row lists (or recovers -- see below)
#     stamped      the MEMCONTINUUM_RENDERED value on this claude-dir's
#                  rendered hook lines ("none" = pre-stamp render)
#     engine       this engine checkout's current short sha
#     store-match  yes/no/unknown -- the row's own store= vs the rendered
#                  MEMCONTINUUM_ROOT on those hook lines (a stamp match alone
#                  cannot catch a store renamed under the same engine
#                  version)
#     rules        ok/stale/missing/foreign -- <claude-dir>/rules/
#                  memcontinuum.md's own identity marker and stamp
#     action       ok | stale | store-mismatch | rules-stale | rules-missing
#                  | rules-foreign | migrate | migrate-needs-never-exts |
#                  store-missing | no-wiring | unrecoverable
#
#                  store-missing outranks every other answer, including a
#                  stamp and a store= that both look right: those compare
#                  strings, and a string can agree with a store that has been
#                  renamed or deleted. Nothing is re-rendered against a store
#                  that is not there.
#
# --apply    re-runs scripts/repo-init.sh, with the parameters this row
#            recorded (adopting the row's existing --store -- this command
#            never creates or renames a store, only re-renders wiring that
#            points at one that already exists), for every claude-dir whose
#            action is not "ok".
#            "store-missing" (the row's store is no longer an existing
#            MemContinuum store -- renamed or deleted) is never applied
#            automatically: re-running the installer against a missing
#            --store would SEED A FRESH ONE there, which is exactly the
#            "stores never touched" line this command does not cross.
#            Fix the row's store (or restore the old one) and re-run.
# --repo PATH
#            narrows the walk to that one repository's row. Required
#            alongside the migration options below (they describe ONE row).
# --machine  also re-runs memcontinuum-setup.sh once, to refresh the
#            machine-level detector hook and skill copy. Off by default --
#            most updates are per-repo; the machine layer rarely drifts.
#
# MIGRATING A ROW WRITTEN BEFORE THE REGISTRY RECORDED WIRING PARAMETERS
#
# Such a row has no claude-dirs on record. This command can see the one
# claude-dir it can find, and it PROPOSES it in the table -- but a project
# may well have more than one (a session-home .claude beside a bare
# checkout, or two homes pointed at the same store), and there is no way to
# discover the rest. So the table proposes and a human decides; nothing is
# written from a guess. Actions:
#
#   migrate-needs-claude-dirs   name the full set:
#       memcontinuum-update.sh --apply --repo PATH \
#           --claude-dir DIR [--claude-dir DIR ...]
#   migrate-needs-langs         the wiring predates the language set being
#       written onto the hook line, so it cannot be read back -- unknown,
#       not "none". Add --langs LANG[,LANG].
#   migrate-needs-never-exts    the never-mention list on the hook line is
#       not a plain extension list. Add --never-ext .ext[,.ext].
#   migrate                     everything needed is on record or recovered;
#       --apply re-renders, and only then rewrites the row (via
#       memcontinuum-decide.sh wired) to carry the parameters from now on.
#
# Each named --claude-dir must ALREADY carry this project's wiring: this
# command records what is installed, it never wires a directory from scratch.
# A second claude-dir joins a project's row only by being named on a
# `memcontinuum-decide.sh wired` command line -- here, or typed directly.
# Nothing discovers one.
#
# --add-lang LANG [--never-ext .ext] --repo PATH
# --never-ext .ext [--add-lang LANG] --repo PATH
#            A human typed this command: that IS the consent, so it always
#            applies (--dry-run still previews it without writing, if you
#            want to check the plan first). Adds LANG to the row's recorded
#            language set and/or .ext to its recorded never-mention list
#            (both are ADDITIVE -- neither drops what the row already had),
#            rewrites the row (memcontinuum-decide.sh wired, every field),
#            then re-renders every claude-dir that row lists with the new
#            set. --repo PATH is required -- same reasoning as
#            memcontinuum-decide.sh's own --repo requirement: no silent
#            $PWD default for a write that changes what gets indexed.
#            The row is rewritten only AFTER every claude-dir has
#            re-rendered successfully, and an unknown language is refused
#            before anything is touched at all.
#            (--apply is what tells the two modes apart: the same
#            --never-ext spelling means "add this to the row" on its own,
#            and "here is the whole list for the row you are migrating"
#            under --apply.)
#
# This command never wires an undecided or declined repo (it only ever
# touches rows already marked `wired`), and never creates a store: every
# installer run it makes is passed --adopt-only, which refuses outright
# unless the store is already there. It never deletes or rewrites a store's
# own git history either -- re-rendering settings/rules/skill is all it does.
#
# Exit codes:
#   the reporting walk (no --apply) always exits 0 -- a stale row is the
#   ANSWER there, not an error.
#   --apply exits 0 only when every claude-dir it walked ended up correct:
#   already ok, or re-rendered successfully. Anything left undone -- a failed
#   installer run, or a dir deliberately skipped (store-missing, a foreign
#   rules file, a migration this command must not guess at) -- exits non-zero,
#   with the table still printed and the reason on stderr.
#   The one exception is `no-wiring`: a claude-dir with none of this
#   project's hook lines at all is a broken or never-finished INSTALL, which
#   is the memcontinuum skill's repair path (a human is asked), not this
#   command's. A `wired` row is never a licence to wire anything. It is
#   reported and left alone, and it does not make this command fail.
#   --add-lang/--never-ext exit non-zero on bad usage, same as
#   memcontinuum-decide.sh.
# --MC-USAGE-END--

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
ENGINE_ROOT="$(cd "$SCRIPT_DIR/.." >/dev/null 2>&1 && pwd)"
REPO_INIT="$SCRIPT_DIR/repo-init.sh"
DECIDE="$SCRIPT_DIR/memcontinuum-decide.sh"
SETUP="$ENGINE_ROOT/memcontinuum-setup.sh"

# shellcheck source=./mc-registry-lib.sh
. "$SCRIPT_DIR/mc-registry-lib.sh" || { echo "missing $SCRIPT_DIR/mc-registry-lib.sh -- incomplete checkout" >&2; exit 1; }

usage() {
    sed -n '2,/^# --MC-USAGE-END--$/p' "$0" | grep -v '^# --MC-USAGE-END--$' | sed 's/^# \{0,1\}//'
    exit "${1:-1}"
}

[ $# -ge 1 ] && case "$1" in -h|--help) usage 0 ;; esac

mc_resolve_home
DECISIONS="$MEMCONTINUUM_HOME/decisions.tsv"

# What this checkout would render right now -- a fingerprint of the render
# inputs, the same one repo-init.sh stamps with (mc_render_fingerprint,
# scripts/mc-registry-lib.sh). Comparing rendered artifacts against THIS,
# rather than against the engine's HEAD commit, is what makes "a scripts-only
# fix needs nothing, a rendered-artifact fix needs a re-render" a distinction
# this table can actually draw.
mc_render_fingerprint "$ENGINE_ROOT" || :
ENGINE_SHA="$MC_RENDER_FINGERPRINT"

# From the template that defines it, not a copy (mc_rules_identity_marker):
# this command and the installer must agree on what a rendered rules file
# looks like, or one of them calls the other's output foreign.
mc_rules_identity_marker "$ENGINE_ROOT" || {
    echo "cannot read $ENGINE_ROOT/templates/memcontinuum-rules.md (or it is empty) -- incomplete checkout" >&2
    exit 1
}
RULES_IDENTITY_MARKER="$MC_RULES_MARKER"

APPLY=0
# Set only when --dry-run is literally typed -- distinct from APPLY's
# default-0, which the WALK mode reads as "no --apply given yet, preview".
# The targeted --add-lang/--never-ext mode below has the OPPOSITE default
# (a human typed the command, that IS the consent -- see its own usage text
# above): it applies unless --dry-run was explicitly given, so it must not
# key off APPLY's default the walk mode uses.
DRY_RUN_EXPLICIT=0
MACHINE=0
ADD_LANG=""
NEVER_EXT=""
LANGS_FLAG=""
TARGET_REPO=""
declare -a OVERRIDE_CLAUDE_DIRS=()
need_value() { [ $# -ge 2 ] || { echo "missing value for $1" >&2; exit 2; }; }
while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run) APPLY=0; DRY_RUN_EXPLICIT=1; shift ;;
        --apply) APPLY=1; shift ;;
        --machine) MACHINE=1; shift ;;
        --add-lang) need_value "$@"; ADD_LANG="$2"; shift 2 ;;
        --never-ext) need_value "$@"; NEVER_EXT="$2"; shift 2 ;;
        --langs) need_value "$@"; LANGS_FLAG="$2"; shift 2 ;;
        --claude-dir) need_value "$@"; OVERRIDE_CLAUDE_DIRS+=("$2"); shift 2 ;;
        --repo) need_value "$@"; TARGET_REPO="$2"; shift 2 ;;
        -h|--help) usage 0 ;;
        *) echo "unknown argument: $1" >&2; usage 1 ;;
    esac
done

# --- which mode is this? ---------------------------------------------------
#
# --apply is the discriminator, and it is the only one, because the two modes
# want opposite things from the same three option names. `--never-ext .h`
# means "add .h to what this row already records" when a human types it on
# its own; under --apply it means "here is the whole never-list for the row
# you are migrating, because it could not be read back". Splitting them on
# --apply keeps each spelling doing one thing.
TARGETED=0
if [ -n "$ADD_LANG" ]; then
    if [ "$APPLY" -eq 1 ]; then
        echo "--add-lang is not a walk-mode option: it adds one language to ONE row (which is why it takes --repo), and --apply re-renders every row. Drop --apply -- typing --add-lang is itself the consent to write." >&2
        exit 2
    fi
    TARGETED=1
elif [ -n "$NEVER_EXT" ] && [ "$APPLY" -eq 0 ]; then
    TARGETED=1
fi

if [ "$TARGETED" -eq 0 ]; then
    # Migration overrides are per-row by nature -- a claude-dir set or a
    # language list belongs to ONE project. With several legacy rows on the
    # machine and no --repo, there would be no saying which row they meant.
    if [ "${#OVERRIDE_CLAUDE_DIRS[@]}" -gt 0 ] || [ -n "$LANGS_FLAG" ] || [ -n "$NEVER_EXT" ]; then
        [ -n "$TARGET_REPO" ] || {
            echo "--claude-dir/--langs/--never-ext describe ONE registry row, so they need --repo PATH to say which. (Without --repo this command walks every wired row and writes nothing it had to guess.)" >&2
            exit 2
        }
    fi
fi

# --- python resolution (only needed for legacy-row lang recovery below) ---
#
# Deliberately duplicated, not sourced, from scripts/repo-init.sh's own
# resolve_python(): that function is private to a much larger script this
# one has no business `source`-ing just to borrow four lines, and the
# fallback chain is small and stable (README.md "Requirements").
mc_update_resolve_python() {
    if [ -n "${MEMCONTINUUM_PYTHON:-}" ]; then
        printf '%s' "$MEMCONTINUUM_PYTHON"
        return 0
    fi
    local cfg="$MEMCONTINUUM_HOME/config.sh" cfg_python
    if [ -f "$cfg" ]; then
        cfg_python="$(. "$cfg" >/dev/null 2>&1; printf '%s' "${MEMCONTINUUM_PYTHON:-}")"
        if [ -n "$cfg_python" ]; then
            printf '%s' "$cfg_python"
            return 0
        fi
    fi
    if [ -x "$ENGINE_ROOT/.venv/bin/python" ]; then
        printf '%s' "$ENGINE_ROOT/.venv/bin/python"
        return 0
    fi
    return 1
}

# mc_update_rules_state CLAUDE_DIR -- sets MC_RULES_STATE to one of
# ok/stale/missing/foreign for CLAUDE_DIR/rules/memcontinuum.md.
mc_update_rules_state() {
    local dest="$1/rules/memcontinuum.md" first second
    if [ ! -f "$dest" ]; then
        MC_RULES_STATE="missing"
        return 0
    fi
    first="$(sed -n '1p' "$dest")"
    if [ "$first" != "$RULES_IDENTITY_MARKER" ]; then
        MC_RULES_STATE="foreign"
        return 0
    fi
    second="$(sed -n '2p' "$dest")"
    if [ "$second" = "<!-- memcontinuum-rendered: $ENGINE_SHA -->" ]; then
        MC_RULES_STATE="ok"
    else
        MC_RULES_STATE="stale"
    fi
    return 0
}

# mc_update_glob_to_ext GLOB -- strips a leading "*" off one
# MEMCONTINUUM_LANG_EXTS/MEMCONTINUUM_NEVER_EXTS token ("*.py" -> ".py").
mc_update_glob_to_ext() {
    case "$1" in
        \**) printf '%s' "${1#\*}" ;;
        *)   printf '%s' "$1" ;;
    esac
}

# mc_update_recover_from_settings PROJECT CLAUDE_DIR -- recovers what a
# legacy row (no claude-dirs on record) never wrote down, by reading what
# repo-init actually rendered there. Sets MC_RECOVERED_CODE_ROOTS (semicolon
# list), MC_RECOVERED_LANGS (comma list, repo-init's own --langs shape), and
# MC_RECOVERED_NEVER (comma list). Always returns 0 -- a project with no
# code-root at all (rationale-only install) recovers all three as "".
# mc_update_project_lines_for_basename PROJECT BASENAME SETTINGS_FILE... --
# like mc-registry-lib.sh's mc_wired_commands_for_project, but matched
# against ONE caller-given basename instead of MC_HOOK_BASENAMES (the fixed
# five write-side hooks that list deliberately excludes pre-edit-chain.sh
# and newfile-nudge.sh -- see that list's own header comment). Needed here
# because langs/never-exts/code-roots only ever ride on the newfile-nudge.sh
# line, which mc_wired_commands_for_project would never match. Prints one
# matching command line per stdout line; returns 1 with nothing printed
# when there is no match.
mc_update_project_lines_for_basename() {
    local project="$1" basename="$2" settings line padded found=1
    shift 2
    for settings in "$@"; do
        [ -f "$settings" ] || continue
        while IFS= read -r line || [ -n "$line" ]; do
            case "$line" in
                *"$basename"*)
                    padded=" $line "
                    case "$padded" in
                        *" MEMCONTINUUM_PROJECT=$project "*|*" MEMCONTINUUM_PROJECT='$project' "*)
                            printf '%s\n' "$line"
                            found=0
                            ;;
                    esac
                    ;;
            esac
        done < <(grep '"command"' "$settings" 2>/dev/null)
    done
    return "$found"
}

mc_update_recover_from_settings() {
    local project="$1" claude_dir="$2" line lang_glob_str never_glob_str
    local -a roots=()
    local root_seen=""
    MC_RECOVERED_CODE_ROOTS=""
    MC_RECOVERED_LANGS=""
    MC_RECOVERED_NEVER=""
    MC_RECOVERED_NEVER_OK=1
    # No nudge line at all means a rationale-only install: there is no code
    # indexing here and therefore no language set to lose, so "" IS the
    # recovered answer. A nudge line that carries no MEMCONTINUUM_LANG_EXTS
    # is the opposite -- wiring rendered before the set was written down, so
    # the answer is genuinely unknown (see MC_ENV_PRESENT in
    # scripts/mc-registry-lib.sh).
    MC_RECOVERED_LANGS_OK=1
    MC_RECOVERED_LANG_NOTE=""
    local nudge_seen=0 lang_present=0
    lang_glob_str=""
    never_glob_str=""
    while IFS= read -r line || [ -n "$line" ]; do
        [ -n "$line" ] || continue
        case "$line" in
            *newfile-nudge.sh*)
                nudge_seen=1
                mc_command_env_value "$line" "MEMCONTINUUM_CODE_ROOT"
                if [ -n "$MC_ENV_VALUE" ]; then
                    case ";$root_seen;" in
                        *";$MC_ENV_VALUE;"*) ;;
                        *) roots+=("$MC_ENV_VALUE"); root_seen="$root_seen;$MC_ENV_VALUE" ;;
                    esac
                fi
                if [ "$lang_present" -eq 0 ]; then
                    mc_command_env_value "$line" "MEMCONTINUUM_LANG_EXTS"
                    if [ "$MC_ENV_PRESENT" -eq 1 ]; then
                        lang_present=1
                        lang_glob_str="$MC_ENV_VALUE"
                    fi
                fi
                if [ -z "$never_glob_str" ]; then
                    mc_command_env_value "$line" "MEMCONTINUUM_NEVER_EXTS"
                    never_glob_str="$MC_ENV_VALUE"
                fi
                ;;
        esac
    done < <(mc_update_project_lines_for_basename "$project" "newfile-nudge.sh" \
                 "$claude_dir/settings.local.json" "$claude_dir/settings.json")
    if [ "$nudge_seen" -eq 1 ] && [ "$lang_present" -eq 0 ]; then
        MC_RECOVERED_LANGS_OK=0
    fi
    # Rationale-only wiring (no PreToolUse hooks at all) has no nudge line to
    # recover a code-root from -- fall back to the always-present write-side
    # line's own MEMCONTINUUM_CODE_ROOT (first root only; write-hooks.json.tmpl
    # only ever carries the first -- see repo-init.sh's own single-root note).
    if [ "${#roots[@]}" -eq 0 ]; then
        while IFS= read -r line || [ -n "$line" ]; do
            [ -n "$line" ] || continue
            mc_command_env_value "$line" "MEMCONTINUUM_CODE_ROOT"
            if [ -n "$MC_ENV_VALUE" ]; then
                roots=("$MC_ENV_VALUE")
                break
            fi
        done < <(mc_wired_commands_for_project "$project" \
                     "$claude_dir/settings.local.json" "$claude_dir/settings.json")
    fi
    MC_RECOVERED_CODE_ROOTS="$(join_semi "${roots[@]:-}")"

    if [ -n "$lang_glob_str" ]; then
        local py glob ext known_exts known_lang lang_list=""
        py="$(mc_update_resolve_python)" || py=""
        if [ -z "$py" ]; then
            # The extension globs are on the line, but turning them back into
            # language NAMES needs this engine's own language table, which
            # needs python. Without it the answer is unknown, not empty.
            MC_RECOVERED_LANGS_OK=0
        fi
        if [ -n "$py" ]; then
            # Two lines out: the recovered language list, then anything the
            # line carries that the list does not account for. A language is
            # "rendered" only when ALL of its extensions are present; one that
            # is half-present is NOT recorded (recording it would claim more
            # than is wired) but it is NAMED -- silently dropping it is how a
            # migration ends up recording a smaller language set than what is
            # actually installed and saying nothing about the difference.
            # Globs matching no language at all are named for the same reason.
            lang_list="$(
                MC_UPDATE_EXTS="$lang_glob_str" MC_UPDATE_ENGINE_ROOT="$ENGINE_ROOT" \
                PYTHONPATH= "$py" - <<'PYEOF' 2>/dev/null
import os, sys
sys.path.insert(0, os.environ["MC_UPDATE_ENGINE_ROOT"])
import chunkers
have = set(os.environ.get("MC_UPDATE_EXTS", "").split())
full, partial, claimed = [], [], set()
for lang, row in sorted(chunkers.LANGUAGE_TABLE.items()):
    globs = set("*" + e for e in row["extensions"])
    if not globs:
        continue
    hit = globs & have
    if hit == globs:
        full.append(lang)
        claimed |= globs
    elif hit:
        partial.append("%s (has %s, missing %s)"
                       % (lang, " ".join(sorted(hit)), " ".join(sorted(globs - hit))))
        claimed |= hit
notes = list(partial)
leftover = sorted(have - claimed)
if leftover:
    notes.append("extensions matching no language this engine knows: %s"
                 % " ".join(leftover))
print(",".join(full))
print(" | ".join(notes))
PYEOF
            )"
            MC_RECOVERED_LANG_NOTE="${lang_list#*$'\n'}"
            lang_list="${lang_list%%$'\n'*}"
        fi
        MC_RECOVERED_LANGS="$lang_list"
    fi

    if [ -n "$never_glob_str" ]; then
        # `read -ra`, never `for g in $never_glob_str`: the value being
        # recovered is a list of EXTENSION PATTERNS (`*.md *.txt`), and an
        # unquoted expansion hands them straight to pathname expansion --
        # run from a directory holding README.md and requirements.txt, the
        # loop silently recovered THOSE FILENAMES and wrote them into the
        # registry row as the never-list. `read -ra` splits on IFS and never
        # globs.
        local -a nevers=() never_toks=()
        local g ext
        read -ra never_toks <<<"$never_glob_str"
        for g in "${never_toks[@]:-}"; do
            [ -n "$g" ] || continue
            ext="$(mc_update_glob_to_ext "$g")"
            # Only a plain extension survives. Anything else -- a path, a
            # bare word, a leftover quote character -- means this line was
            # hand-edited into a shape this command cannot read, and a guess
            # would be written into a registry row and rendered back onto
            # every hook line. Refused instead: the human names the list.
            case "$ext" in
                .*[!A-Za-z0-9_+-]*|.|"") MC_RECOVERED_NEVER_OK=0 ;;
                .*) nevers+=("$ext") ;;
                *) MC_RECOVERED_NEVER_OK=0 ;;
            esac
        done
        MC_RECOVERED_NEVER="$(IFS=,; printf '%s' "${nevers[*]:-}")"
    fi
    return 0
}

# join_semi ARR... -- shared join, same as memcontinuum-decide.sh's own
# (kept in each file rather than promoted to mc-registry-lib.sh: neither the
# detector nor state.sh needs it, and mc-registry-lib.sh is sourced by the
# detector on every session start in every repo on the machine -- no
# unrelated function belongs there).
join_semi() {
    local out="" first=1 a
    for a in "$@"; do
        if [ "$first" -eq 1 ]; then out="$a"; first=0; else out="$out;$a"; fi
    done
    printf '%s' "$out"
}

TABLE_HEADER_PRINTED=0
print_row() {
    # print_row REPO CLAUDE_DIR STAMPED STORE_MATCH RULES ACTION
    if [ "$TABLE_HEADER_PRINTED" -eq 0 ]; then
        printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "repo" "claude-dir" "stamped" "engine" "store-match" "rules" "action"
        TABLE_HEADER_PRINTED=1
    fi
    printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$1" "$2" "$3" "$ENGINE_SHA" "$4" "$5" "$6"
}

# --- process_claude_dir: the per-(row, claude-dir) walk + optional apply ---
#
# Globals in: KEY PROJECT STORE CODE_ROOTS_SEMI LANGS_COMMA NEVER_COMMA
#             LEGACY (1 = this row had no claude-dirs on record)
# Globals out (for the migrate rewrite, when LEGACY=1): none -- migration is
# handled by the caller after every claude-dir of a legacy row is walked, so
# one rewrite covers the whole row, not one per claude-dir.
process_claude_dir() {
    local claude_dir="$1"
    local -a lines=()
    local line stamp="" store_match="unknown" action=""

    while IFS= read -r line || [ -n "$line" ]; do
        [ -n "$line" ] && lines+=("$line")
    done < <(mc_wired_commands_for_project "$PROJECT" \
                 "$claude_dir/settings.local.json" "$claude_dir/settings.json")

    if [ "${#lines[@]}" -eq 0 ]; then
        mc_update_rules_state "$claude_dir"
        print_row "$KEY" "$claude_dir" "none" "unknown" "$MC_RULES_STATE" "no-wiring"
        return 0
    fi

    for line in "${lines[@]}"; do
        mc_command_env_value "$line" "MEMCONTINUUM_RENDERED"
        [ -n "$MC_ENV_VALUE" ] || MC_ENV_VALUE="none"
        if [ -z "$stamp" ]; then
            stamp="$MC_ENV_VALUE"
        elif [ "$stamp" != "$MC_ENV_VALUE" ]; then
            stamp="mixed"
        fi
        mc_command_env_value "$line" "MEMCONTINUUM_ROOT"
        if [ -n "$MC_ENV_VALUE" ] && [ -n "$STORE" ]; then
            if [ "$MC_ENV_VALUE" = "$STORE" ]; then
                [ "$store_match" = "unknown" ] && store_match="yes"
            else
                store_match="no"
            fi
        fi
    done

    mc_update_rules_state "$claude_dir"

    # store-missing outranks everything, INCLUDING a clean stamp and a
    # store= that still matches what is rendered. A rendered
    # MEMCONTINUUM_ROOT agreeing with the row's store= only proves the two
    # STRINGS agree -- if nothing exists at that path any more (renamed,
    # deleted, or replaced by an unrelated git repo), that is agreement on a
    # corpse: every hook wired here points at a store that is gone, and this
    # walk's job is to say so rather than print `ok`.
    #
    # Precedence, and the reason for it: the answers this command will never
    # act on come FIRST, so the action column names why nothing will happen
    # rather than naming some lesser drift that --apply would then try to fix
    # and fail. store-missing and rules-foreign are both refusals the
    # installer would only repeat more loudly; the migrate-needs-* answers are
    # questions only a human can settle. Everything below them is drift this
    # command can and will re-render.
    if ! mc_is_marked_store "$STORE"; then
        action="store-missing"
    elif [ "$MC_RULES_STATE" = "foreign" ]; then
        action="rules-foreign"
    elif [ "$LEGACY" -eq 1 ]; then
        action="$LEGACY_ACTION"
    elif [ "$stamp" != "$ENGINE_SHA" ]; then
        action="stale"
    elif [ "$store_match" = "no" ]; then
        action="store-mismatch"
    elif [ "$MC_RULES_STATE" = "missing" ]; then
        action="rules-missing"
    elif [ "$MC_RULES_STATE" = "stale" ]; then
        action="rules-stale"
    else
        action="ok"
    fi

    print_row "$KEY" "$claude_dir" "$stamp" "$store_match" "$MC_RULES_STATE" "$action"

    if [ "$APPLY" -eq 1 ] && [ "$action" != "ok" ]; then
        apply_claude_dir "$claude_dir" "$action"
    fi
    return 0
}

# not_applied REASON_LINE -- one place that records "--apply was asked for and
# this claude-dir did NOT end up re-rendered", whether that was a refusal
# (store-missing, a foreign rules file, a migration this command must not
# guess at) or an outright failure of repo-init. `--apply` exits non-zero
# unless every dir it walked is either already `ok` or was successfully
# re-rendered: a command that reports success while silently leaving work
# undone is the one behavior a re-render tool cannot afford. The plain
# reporting walk (no --apply) still always exits 0 -- there, a stale row is
# the ANSWER, not a failure.
WALK_RC=0
not_applied() {
    echo "$1" >&2
    WALK_RC=1
}

# apply_claude_dir CLAUDE_DIR ACTION -- re-runs repo-init.sh for one
# claude-dir with this row's recorded parameters. Never invoked for
# action=ok. action=rules-foreign is reported but never applied here
# (repo-init.sh itself refuses a foreign rules file before writing
# anything -- calling it would just fail loudly for a reason already named
# in the table; the fix is a human moving the foreign file aside).
apply_claude_dir() {
    local claude_dir="$1" action="$2"
    local -a args=()
    local cr

    if [ "$action" = "rules-foreign" ]; then
        not_applied "  SKIPPED $claude_dir: rules-foreign -- move $claude_dir/rules/memcontinuum.md aside first (it was not rendered by this installer)"
        return 0
    fi

    case "$action" in
        migrate-needs-*)
            not_applied "  SKIPPED $claude_dir: $action -- $MIGRATE_BLOCKED_HINT"
            return 0
            ;;
    esac

    if ! mc_is_marked_store "$STORE"; then
        not_applied "  SKIPPED $claude_dir: store-missing -- $STORE is not an existing MemContinuum store (renamed or deleted?) -- fix the row (memcontinuum-decide.sh wired --repo ... --store NEWPATH ...) or restore the store before re-rendering"
        return 0
    fi

    # --adopt-only, always: a re-render wires a store that already exists and
    # never creates one. This is belt AND braces with the store check just
    # above -- the check is what produces the readable message, the flag is
    # what makes it impossible for any path through this command to seed a
    # store even if a future edit forgets the check.
    args=(--project "$PROJECT" --store "$STORE" --claude-dir "$claude_dir" --non-interactive --adopt-only)
    mc_build_wiring_args "$CODE_ROOTS_SEMI" "$LANGS_COMMA" "$NEVER_COMMA"
    args+=(${MC_BUILT_ARGS[@]+"${MC_BUILT_ARGS[@]}"})

    echo "  applying: bash $REPO_INIT ${args[*]}"
    if bash "$REPO_INIT" "${args[@]}" >"$SBOX_APPLY_LOG" 2>&1; then
        echo "  OK $claude_dir"
    else
        local rc=$?
        not_applied "  FAILED $claude_dir (rc=$rc) -- see below"
        sed 's/^/    /' "$SBOX_APPLY_LOG" >&2
        MIGRATE_RENDER_FAILED=1
    fi
}

SBOX_APPLY_LOG="$(mktemp 2>/dev/null || printf '/tmp/mc-update-apply-log.%s' "$$")"
trap 'rm -f "$SBOX_APPLY_LOG"' EXIT

# --- targeted mode: --add-lang / --never-ext -------------------------------

if [ "$TARGETED" -eq 1 ]; then
    [ -n "$TARGET_REPO" ] || { echo "--add-lang/--never-ext requires an explicit repo: pass --repo PATH" >&2; exit 2; }
    if ! mc_repo_key "$TARGET_REPO"; then
        echo "not a git repository: $TARGET_REPO" >&2
        exit 1
    fi
    if ! mc_registry_lookup "$DECISIONS" "$MC_REPO_KEY" || [ "$MC_LOOKUP_DECISION" != "wired" ]; then
        echo "no wired row for $TARGET_REPO ($MC_REPO_KEY) -- nothing to update. Wire it first (the memcontinuum skill does this)." >&2
        exit 1
    fi
    KEY="$MC_REPO_KEY"
    NOTE="$MC_LOOKUP_NOTE"
    mc_note_field "$NOTE" "store"; STORE="$MC_NOTE_FIELD"
    mc_note_field "$NOTE" "project"; PROJECT="$MC_NOTE_FIELD"
    mc_note_field "$NOTE" "claude-dirs"; CLAUDE_DIRS_SEMI="$MC_NOTE_FIELD"
    mc_note_field "$NOTE" "code-roots"; CODE_ROOTS_SEMI="$MC_NOTE_FIELD"
    mc_note_field "$NOTE" "langs"; EXISTING_LANGS_SEMI="$MC_NOTE_FIELD"
    mc_note_field "$NOTE" "never"; EXISTING_NEVER_SEMI="$MC_NOTE_FIELD"

    [ -n "$CLAUDE_DIRS_SEMI" ] || CLAUDE_DIRS_SEMI="$MC_REPO/.claude"
    [ -n "$STORE" ] && [ -n "$PROJECT" ] || {
        echo "row for $TARGET_REPO has no recorded store/project -- re-run repo-init and memcontinuum-decide.sh wired with --store/--project first" >&2
        exit 1
    }

    # Additive union, never a drop: LANG/EXT already on the row stay.
    add_semi() {
        # Declared, then assigned, on separate statements deliberately: a
        # single `local a="$1" b="$a"` reads "$a" as still-unset under
        # `set -u` in this bash -- the whole statement's right-hand sides
        # are resolved before any of ITS OWN new locals exist, not
        # left-to-right (reproduced; not documented bash behavior worth
        # relying on either way).
        local existing new out tok
        existing="$1"
        new="$2"
        out="$existing"
        [ -n "$new" ] || { printf '%s' "$existing"; return 0; }
        for tok in $(printf '%s' "$new" | tr ',' ' '); do
            case ";$out;" in
                *";$tok;"*) ;;
                *) out="${out:+$out;}$tok" ;;
            esac
        done
        printf '%s' "$out"
    }
    NEW_LANGS_SEMI="$(add_semi "$EXISTING_LANGS_SEMI" "$ADD_LANG")"
    NEW_NEVER_SEMI="$(add_semi "$EXISTING_NEVER_SEMI" "$NEVER_EXT")"

    LANGS_COMMA="$(printf '%s' "$NEW_LANGS_SEMI" | tr ';' ',')"
    NEVER_COMMA="$(printf '%s' "$NEW_NEVER_SEMI" | tr ';' ',')"

    # The store has to still be there. Checked here, before anything else, so
    # a renamed or deleted store gets this command's own plain answer rather
    # than a re-render's -- and so no registry row is ever rewritten to
    # describe wiring for a store that does not exist.
    if ! mc_is_marked_store "$STORE"; then
        echo "store-missing: $STORE is not an existing MemContinuum store (renamed or deleted?) -- nothing was written. Fix the row (memcontinuum-decide.sh wired --repo $MC_REPO --store NEWPATH --project $PROJECT ...) or restore the store, then re-run." >&2
        exit 1
    fi

    # An unknown language never reaches the registry. repo-init validates
    # --langs too, but only when the install has a --code-root to wire it
    # into: a row with no code-roots would sail straight past that check and
    # record a language this engine has no chunker for.
    if [ -n "$ADD_LANG" ]; then
        MC_UPDATE_PY="$(mc_update_resolve_python)" || MC_UPDATE_PY=""
        if [ -z "$MC_UPDATE_PY" ]; then
            echo "cannot validate --add-lang $ADD_LANG: no python found (set \$MEMCONTINUUM_PYTHON or run memcontinuum-setup.sh). Refusing to record a language this command could not check." >&2
            exit 1
        fi
        MC_UPDATE_LANG_ERR="$(
            MC_UPDATE_WANT="$ADD_LANG" MC_UPDATE_ENGINE_ROOT="$ENGINE_ROOT" \
            PYTHONPATH= "$MC_UPDATE_PY" - <<'PYEOF'
import os, sys
sys.path.insert(0, os.environ["MC_UPDATE_ENGINE_ROOT"])
import chunkers
known = set(chunkers.LANGUAGE_TABLE)
bad = [w for w in (t.strip() for t in os.environ["MC_UPDATE_WANT"].split(",")) if w and w not in known]
if bad:
    print("unknown language: %s (this engine version knows: %s)"
          % (", ".join(bad), " ".join(sorted(known))))
PYEOF
        )" || {
            echo "could not read this engine's language table to validate --add-lang $ADD_LANG -- nothing was written" >&2
            exit 1
        }
        if [ -n "$MC_UPDATE_LANG_ERR" ]; then
            echo "$MC_UPDATE_LANG_ERR" >&2
            echo "nothing was written -- the registry row is unchanged." >&2
            exit 1
        fi
    fi

    mc_split_semi "$CLAUDE_DIRS_SEMI"
    TARGET_CLAUDE_DIRS=(${MC_SPLIT[@]+"${MC_SPLIT[@]}"})
    mc_build_wiring_args "$CODE_ROOTS_SEMI" "$LANGS_COMMA" "$NEVER_COMMA"
    WIRING_ARGS=(${MC_BUILT_ARGS[@]+"${MC_BUILT_ARGS[@]}"})

    DECIDE_ARGS=(wired --repo "$MC_REPO" --store "$STORE" --project "$PROJECT")
    for d in ${TARGET_CLAUDE_DIRS[@]+"${TARGET_CLAUDE_DIRS[@]}"}; do
        DECIDE_ARGS+=(--claude-dir "$d")
    done
    DECIDE_ARGS+=(${WIRING_ARGS[@]+"${WIRING_ARGS[@]}"})

    echo "plan: $DECIDE wired --repo $MC_REPO --store $STORE --project $PROJECT (langs=$LANGS_COMMA never=$NEVER_COMMA claude-dirs=$CLAUDE_DIRS_SEMI code-roots=$CODE_ROOTS_SEMI)"
    for d in ${TARGET_CLAUDE_DIRS[@]+"${TARGET_CLAUDE_DIRS[@]}"}; do
        [ -n "$d" ] || continue
        echo "plan: bash $REPO_INIT --project $PROJECT --store $STORE --claude-dir $d --non-interactive --adopt-only --langs $LANGS_COMMA --never-ext $NEVER_COMMA (code-roots: ${CODE_ROOTS_SEMI:-none})"
    done

    # Consent is the command itself (see this mode's own usage text above):
    # applies unless --dry-run was explicitly given -- --apply is accepted
    # but redundant here, never required.
    if [ "$DRY_RUN_EXPLICIT" -eq 1 ]; then
        echo "(dry run -- drop --dry-run to write this)"
        exit 0
    fi

    # RE-RENDER FIRST, RECORD SECOND. The registry row is the description of
    # what is rendered on disk; writing it before the render means a failed
    # render (a foreign rules file, an unwritable claude-dir, a store that
    # went missing between the check above and now) leaves the registry
    # claiming a language set that exists nowhere -- and every later
    # re-render replays that claim. If any claude-dir fails, the row is left
    # exactly as it was.
    RC=0
    for d in ${TARGET_CLAUDE_DIRS[@]+"${TARGET_CLAUDE_DIRS[@]}"}; do
        REINIT_ARGS=(--project "$PROJECT" --store "$STORE" --claude-dir "$d" --non-interactive --adopt-only)
        REINIT_ARGS+=(${WIRING_ARGS[@]+"${WIRING_ARGS[@]}"})
        if ! bash "$REPO_INIT" "${REINIT_ARGS[@]}"; then
            echo "ERROR: re-render failed for $d -- see above" >&2
            RC=1
        fi
    done
    if [ "$RC" -ne 0 ]; then
        echo "ERROR: at least one claude-dir did not re-render -- the registry row was NOT changed (it still describes the wiring that is actually on disk)." >&2
        exit 1
    fi

    if ! bash "$DECIDE" "${DECIDE_ARGS[@]}"; then
        echo "ERROR: the re-render succeeded but the registry row could not be rewritten -- see above. Re-run this command to try the row again." >&2
        exit 1
    fi
    exit 0
fi

# --- default mode: walk every wired row -------------------------------

[ -f "$DECISIONS" ] || {
    echo "no registry at $DECISIONS -- nothing wired yet (run memcontinuum-setup.sh, then the memcontinuum skill)"
    exit 0
}

MIGRATE_HINTS=""

# --repo narrows the walk to that one repository's row -- which is also what
# makes the per-row migration overrides (--claude-dir/--langs/--never-ext)
# unambiguous. Without it, every wired row is walked.
ONLY_KEY=""
if [ -n "$TARGET_REPO" ]; then
    if ! mc_repo_key "$TARGET_REPO"; then
        echo "not a git repository: $TARGET_REPO" >&2
        exit 1
    fi
    ONLY_KEY="$MC_REPO_KEY"
fi

while IFS= read -r RAW_LINE || [ -n "$RAW_LINE" ]; do
    case "$RAW_LINE" in \#*|"") continue ;; esac
    KEY="${RAW_LINE%%"$MC_TAB"*}"
    REST="${RAW_LINE#*"$MC_TAB"}"
    IFS="$MC_TAB" read -r ROW_DECISION ROW_WHEN NOTE <<<"$REST"
    [ "$ROW_DECISION" = "wired" ] || continue
    if [ -n "$ONLY_KEY" ] && [ "$KEY" != "$ONLY_KEY" ]; then
        continue
    fi

    mc_note_field "$NOTE" "store"; STORE="$MC_NOTE_FIELD"
    mc_note_field "$NOTE" "project"; PROJECT="$MC_NOTE_FIELD"
    mc_note_field "$NOTE" "claude-dirs"; CLAUDE_DIRS_SEMI="$MC_NOTE_FIELD"
    mc_note_field "$NOTE" "code-roots"; CODE_ROOTS_SEMI="$MC_NOTE_FIELD"
    mc_note_field "$NOTE" "langs"; LANGS_SEMI="$MC_NOTE_FIELD"
    mc_note_field "$NOTE" "never"; NEVER_SEMI="$MC_NOTE_FIELD"
    LANGS_COMMA="$(printf '%s' "$LANGS_SEMI" | tr ';' ',')"
    NEVER_COMMA="$(printf '%s' "$NEVER_SEMI" | tr ';' ',')"

    if [ -z "$PROJECT" ]; then
        print_row "$KEY" "(unknown)" "none" "unknown" "unknown" "unrecoverable"
        echo "  no project= recorded for $KEY -- re-run memcontinuum-decide.sh wired --repo ... --store ... --project ... to fix" >&2
        continue
    fi

    LEGACY=0
    CLAUDE_DIRS_EXPLICIT=0
    if [ -z "$CLAUDE_DIRS_SEMI" ]; then
        LEGACY=1
        if [ "${#OVERRIDE_CLAUDE_DIRS[@]}" -gt 0 ]; then
            # The human named the set on the command line. That is the ONLY
            # way a claude-dir set gets written for a legacy row: this
            # command can see one .claude, and a project may well have two
            # (a session home beside a bare checkout), so what it recovers is
            # a PROPOSAL, never an answer.
            CLAUDE_DIRS_SEMI=""
            for OD in "${OVERRIDE_CLAUDE_DIRS[@]}"; do
                [ -n "$OD" ] || continue
                CLAUDE_DIRS_SEMI="${CLAUDE_DIRS_SEMI:+$CLAUDE_DIRS_SEMI;}$OD"
            done
            CLAUDE_DIRS_EXPLICIT=1
        else
            # KEY is a path key (starts with "/") iff it IS the repo's working
            # tree path -- decide.sh writes the origin remote URL as KEY when
            # one exists, else the path itself (mc_repo_key). A remote-keyed
            # legacy row's repo path is simply not in the registry anywhere;
            # this command does not guess it (a wrong guess would touch the
            # wrong repo's .claude).
            case "$KEY" in
                /*) CLAUDE_DIRS_SEMI="$KEY/.claude" ;;
                *)
                    print_row "$KEY" "(unknown)" "none" "unknown" "unknown" "unrecoverable"
                    echo "  legacy row, no claude-dirs recorded, and the key is a remote URL (not a path) -- this row's claude-dir cannot be recovered automatically. Fix: memcontinuum-decide.sh wired --repo PATH --store $STORE --project $PROJECT --claude-dir DIR [--code-root DIR ...] [--langs LIST]" >&2
                    [ "$APPLY" -eq 1 ] && WALK_RC=1
                    continue
                    ;;
            esac
        fi
    fi

    # A legacy row's code-roots/langs/never are recovered ONCE, from the
    # first claude-dir's own rendered settings (repo-init installs one
    # coherent set per project; several claude-dirs for the same project
    # render the same set -- see INC-0104). Recovered here, before the
    # per-claude-dir walk, so every claude-dir's `migrate` apply (below)
    # uses the same recovered values, and the end-of-row registry rewrite
    # only has to happen once.
    LEGACY_ACTION="migrate"
    MIGRATE_BLOCKED_HINT=""
    # Reset per row: the registry row is rewritten only when every one of this
    # row's claude-dirs actually re-rendered. A row rewritten after a failed
    # render would record parameters that are not on disk anywhere.
    MIGRATE_RENDER_FAILED=0
    if [ "$LEGACY" -eq 1 ]; then
        FIRST_CLAUDE_DIR="${CLAUDE_DIRS_SEMI%%;*}"
        mc_update_recover_from_settings "$PROJECT" "$FIRST_CLAUDE_DIR"
        [ -n "$CODE_ROOTS_SEMI" ] || CODE_ROOTS_SEMI="$MC_RECOVERED_CODE_ROOTS"
        [ -n "$LANGS_COMMA" ] || LANGS_COMMA="$MC_RECOVERED_LANGS"
        [ -n "$NEVER_COMMA" ] || NEVER_COMMA="$MC_RECOVERED_NEVER"
        # Command-line overrides win over anything recovered -- they are the
        # human's answer to exactly the question the recovery could not.
        [ -n "$LANGS_FLAG" ] && LANGS_COMMA="$LANGS_FLAG"
        [ -n "$NEVER_EXT" ] && NEVER_COMMA="$NEVER_EXT"

        # Anything the rendered extension list carries that the recovered
        # language set does not account for is said out loud, under the row it
        # belongs to. The table has no note column, and a difference nobody is
        # told about is how a migration records less than what is wired.
        if [ -n "$MC_RECOVERED_LANG_NOTE" ] && [ -z "$LANGS_FLAG" ]; then
            echo "  $KEY: partially rendered -- $MC_RECOVERED_LANG_NOTE. Recorded languages: ${LANGS_COMMA:-(none)}. Pass --langs LIST to record something else." >&2
        fi

        # The migration PROPOSES and refuses; it never writes a value it had
        # to invent. Whatever goes into the row is replayed by every future
        # re-render, so an invention here is permanent.
        MIGRATE_FIX_CMD="$DECIDE wired --repo $KEY --store $STORE --project $PROJECT --claude-dir $FIRST_CLAUDE_DIR [--claude-dir DIR ...]${CODE_ROOTS_SEMI:+ --code-root ...}"
        if [ "$CLAUDE_DIRS_EXPLICIT" -eq 0 ]; then
            LEGACY_ACTION="migrate-needs-claude-dirs"
            MIGRATE_BLOCKED_HINT="this row records no claude-dirs. $FIRST_CLAUDE_DIR is a PROPOSAL -- the one this command can see -- and a project may have more than one (a session-home .claude beside a bare checkout, say). Name the full set and re-run:
    $0 --apply --repo $KEY --claude-dir $FIRST_CLAUDE_DIR [--claude-dir DIR ...]
  or record it directly with:
    $MIGRATE_FIX_CMD"
        elif [ "$MC_RECOVERED_LANGS_OK" -eq 0 ] && [ -z "$LANGS_FLAG" ]; then
            LEGACY_ACTION="migrate-needs-langs"
            MIGRATE_BLOCKED_HINT="the wiring at $FIRST_CLAUDE_DIR was rendered before the language set was written onto the hook line, so which languages this project indexes cannot be read back. That is UNKNOWN, not none -- recording it as none would turn code indexing off for a project that had it on. Name the set and re-run:
    $0 --apply --repo $KEY --claude-dir $FIRST_CLAUDE_DIR --langs LANG[,LANG]"
        elif [ "$MC_RECOVERED_NEVER_OK" -eq 0 ] && [ -z "$NEVER_EXT" ]; then
            LEGACY_ACTION="migrate-needs-never-exts"
            MIGRATE_BLOCKED_HINT="the never-mention list rendered at $FIRST_CLAUDE_DIR is not a plain extension list, so it cannot be read back. Name it and re-run:
    $0 --apply --repo $KEY --claude-dir $FIRST_CLAUDE_DIR --never-ext .ext[,.ext]"
        fi

        # A named claude-dir must ALREADY carry this project's wiring. This
        # command records what is installed; it never wires a directory from
        # scratch -- that is the skill's job, with a human answering.
        if [ "$CLAUDE_DIRS_EXPLICIT" -eq 1 ]; then
            mc_split_semi "$CLAUDE_DIRS_SEMI"
            for OD in ${MC_SPLIT[@]+"${MC_SPLIT[@]}"}; do
                if ! mc_wired_commands_for_project "$PROJECT" \
                        "$OD/settings.local.json" "$OD/settings.json" >/dev/null; then
                    echo "REFUSED: --claude-dir $OD carries no wiring for project $PROJECT. This command records claude-dirs that are already installed; it never wires one from scratch. Install it first (the memcontinuum skill does this), then re-run. Nothing was written." >&2
                    WALK_RC=1
                    continue 2
                fi
            done
        fi
    fi

    mc_split_semi "$CLAUDE_DIRS_SEMI"
    for CLAUDE_DIR in ${MC_SPLIT[@]+"${MC_SPLIT[@]}"}; do
        process_claude_dir "$CLAUDE_DIR"
    done

    if [ "$LEGACY" -eq 1 ] && [ "$APPLY" -eq 1 ] && [ "$LEGACY_ACTION" = "migrate" ] \
           && [ "$MIGRATE_RENDER_FAILED" -eq 0 ]; then
        MIGRATE_ARGS=(wired --repo "$KEY" --store "$STORE" --project "$PROJECT")
        mc_split_semi "$CLAUDE_DIRS_SEMI"
        for d in ${MC_SPLIT[@]+"${MC_SPLIT[@]}"}; do MIGRATE_ARGS+=(--claude-dir "$d"); done
        mc_build_wiring_args "$CODE_ROOTS_SEMI" "$LANGS_COMMA" "$NEVER_COMMA"
        MIGRATE_ARGS+=(${MC_BUILT_ARGS[@]+"${MC_BUILT_ARGS[@]}"})
        # KEY is a path here (the only LEGACY branch that reaches this point
        # -- the remote-keyed one `continue`d above), so `--repo "$KEY"` is
        # safe: decide.sh re-derives the very same key from it.
        if bash "$DECIDE" "${MIGRATE_ARGS[@]}" >/dev/null 2>&1; then
            echo "  migrated: $KEY registry row now records claude-dirs/code-roots/langs/never" >&2
        else
            not_applied "  MIGRATE FAILED: $KEY -- registry row left as-is, re-render still applied above if it succeeded"
        fi
    elif [ "$LEGACY" -eq 1 ] && [ "$APPLY" -eq 1 ]; then
        : # refused above (migrate-needs-*) or the re-render failed -- the row
          # is left exactly as it was, and not_applied has already recorded it.
    elif [ "$LEGACY" -eq 1 ]; then
        if [ -n "$MIGRATE_BLOCKED_HINT" ]; then
            MIGRATE_HINTS="$MIGRATE_HINTS
  $KEY ($LEGACY_ACTION): $MIGRATE_BLOCKED_HINT"
        else
            MIGRATE_HINTS="$MIGRATE_HINTS
  $KEY: run with --apply to migrate this row to the new registry format"
        fi
    fi
done < "$DECISIONS"

if [ -n "$MIGRATE_HINTS" ]; then
    echo >&2
    echo "legacy rows found (pre-dates claude-dirs/code-roots/langs/never in the registry):" >&2
    printf '%s\n' "$MIGRATE_HINTS" >&2
fi

if [ "$APPLY" -eq 1 ] && [ "$MACHINE" -eq 1 ]; then
    echo
    echo "refreshing machine layer: bash $SETUP"
    PY="$(mc_update_resolve_python)" || PY=""
    SETUP_ARGS=(--no-model-warm)
    [ -n "$PY" ] && SETUP_ARGS=(--python "$PY" --no-model-warm)
    if bash "$SETUP" "${SETUP_ARGS[@]}"; then
        echo "OK: machine layer refreshed"
    else
        not_applied "FAILED: machine layer refresh -- see above"
    fi
fi

exit "$WALK_RC"
