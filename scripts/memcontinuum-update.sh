#!/usr/bin/env bash
# usage: memcontinuum-update.sh [--dry-run | --apply] [--machine]
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
#                  | rules-foreign | migrate | store-missing | no-wiring |
#                  unrecoverable
#
# --apply    re-runs scripts/repo-init.sh, with the parameters this row
#            recorded (adopting the row's existing --store -- this command
#            never creates or renames a store, only re-renders wiring that
#            points at one that already exists), for every claude-dir whose
#            action is not "ok". A row with no --claude-dir on record
#            (written before this registry format existed) is migrated: its
#            claude-dir/code-roots/langs/never-exts are recovered from what
#            is actually rendered there today, applied, and the row is
#            rewritten (via memcontinuum-decide.sh wired) to carry them from
#            now on -- reported as `migrate`, a one-time thing per row.
#            "store-missing" (the row's store no longer exists as a git
#            repository -- a renamed or deleted store) is never applied
#            automatically: re-running repo-init against a
#            missing --store would SEED A FRESH ONE there, which is exactly
#            the "stores never touched" line this command does not cross.
#            Fix the row's store (or restore the old one) and re-run.
# --machine  also re-runs memcontinuum-setup.sh once, to refresh the
#            machine-level detector hook and skill copy. Off by default --
#            most updates are per-repo; the machine layer rarely drifts.
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
#
# This command never wires an undecided or declined repo (it only ever
# touches rows already marked `wired`), and never creates, deletes, or
# rewrites a store's own git history -- re-rendering settings/rules/skill is
# all it does. Always exits 0 in the default walk (a stale row is reported,
# not an error); --add-lang/--never-ext exit non-zero on bad usage, same as
# memcontinuum-decide.sh.
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

ENGINE_SHA="$(git -C "$ENGINE_ROOT" rev-parse --short HEAD 2>/dev/null)"
[ -n "$ENGINE_SHA" ] || ENGINE_SHA="unknown"

RULES_IDENTITY_MARKER="<!-- memcontinuum-rules v1 — rendered by MemContinuum repo-init; do not hand-edit -->"

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
TARGET_REPO=""
need_value() { [ $# -ge 2 ] || { echo "missing value for $1" >&2; exit 2; }; }
while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run) APPLY=0; DRY_RUN_EXPLICIT=1; shift ;;
        --apply) APPLY=1; shift ;;
        --machine) MACHINE=1; shift ;;
        --add-lang) need_value "$@"; ADD_LANG="$2"; shift 2 ;;
        --never-ext) need_value "$@"; NEVER_EXT="$2"; shift 2 ;;
        --repo) need_value "$@"; TARGET_REPO="$2"; shift 2 ;;
        -h|--help) usage 0 ;;
        *) echo "unknown argument: $1" >&2; usage 1 ;;
    esac
done

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

# mc_update_is_store DIR -- true iff DIR is a git working tree (root or
# linked worktree -- a linked worktree's .git is a FILE, not a directory,
# same shape scripts/repo-init.sh's own is_git_repo() checks for). A store
# that fails this check no longer exists as a real git repository --
# INC-0104's exact shape (a rename/delete under the row's nose) -- and
# --apply must never re-seed one: that is repo-init's fresh-install path,
# not this command's.
mc_update_is_store() {
    [ -e "$1/.git" ] || return 1
    git -C "$1" rev-parse --is-inside-work-tree >/dev/null 2>&1
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
    lang_glob_str=""
    never_glob_str=""
    while IFS= read -r line || [ -n "$line" ]; do
        [ -n "$line" ] || continue
        case "$line" in
            *newfile-nudge.sh*)
                mc_command_env_value "$line" "MEMCONTINUUM_CODE_ROOT"
                if [ -n "$MC_ENV_VALUE" ]; then
                    case ";$root_seen;" in
                        *";$MC_ENV_VALUE;"*) ;;
                        *) roots+=("$MC_ENV_VALUE"); root_seen="$root_seen;$MC_ENV_VALUE" ;;
                    esac
                fi
                if [ -z "$lang_glob_str" ]; then
                    mc_command_env_value "$line" "MEMCONTINUUM_LANG_EXTS"
                    lang_glob_str="$MC_ENV_VALUE"
                fi
                if [ -z "$never_glob_str" ]; then
                    mc_command_env_value "$line" "MEMCONTINUUM_NEVER_EXTS"
                    never_glob_str="$MC_ENV_VALUE"
                fi
                ;;
        esac
    done < <(mc_update_project_lines_for_basename "$project" "newfile-nudge.sh" \
                 "$claude_dir/settings.local.json" "$claude_dir/settings.json")
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
        if [ -n "$py" ]; then
            lang_list="$(
                MC_UPDATE_EXTS="$lang_glob_str" MC_UPDATE_ENGINE_ROOT="$ENGINE_ROOT" \
                PYTHONPATH= "$py" - <<'PYEOF' 2>/dev/null
import os, sys
sys.path.insert(0, os.environ["MC_UPDATE_ENGINE_ROOT"])
import chunkers
have = set(os.environ.get("MC_UPDATE_EXTS", "").split())
out = []
for lang, row in sorted(chunkers.LANGUAGE_TABLE.items()):
    globs = set("*" + e for e in row["extensions"])
    if globs and globs <= have:
        out.append(lang)
print(",".join(out))
PYEOF
            )"
        fi
        MC_RECOVERED_LANGS="$lang_list"
    fi

    if [ -n "$never_glob_str" ]; then
        local -a nevers=() g
        for g in $never_glob_str; do
            nevers+=("$(mc_update_glob_to_ext "$g")")
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

    if [ "$LEGACY" -eq 1 ]; then
        action="migrate"
    elif [ "$stamp" != "$ENGINE_SHA" ]; then
        action="stale"
    elif [ "$store_match" = "no" ]; then
        action="store-mismatch"
    elif [ "$MC_RULES_STATE" = "foreign" ]; then
        action="rules-foreign"
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
        echo "  SKIPPED $claude_dir: rules-foreign -- move $claude_dir/rules/memcontinuum.md aside first (it was not rendered by this installer)" >&2
        return 0
    fi

    if ! mc_update_is_store "$STORE"; then
        echo "  SKIPPED $claude_dir: store-missing -- $STORE is not a git repository (renamed or deleted?) -- fix the row (memcontinuum-decide.sh wired --repo ... --store NEWPATH ...) or restore the store before re-rendering" >&2
        return 0
    fi

    args=(--project "$PROJECT" --store "$STORE" --claude-dir "$claude_dir" --non-interactive)
    if [ -n "$CODE_ROOTS_SEMI" ]; then
        local IFS_OLD="$IFS"
        IFS=';'
        for cr in $CODE_ROOTS_SEMI; do
            [ -n "$cr" ] && args+=(--code-root "$cr")
        done
        IFS="$IFS_OLD"
    fi
    [ -n "$LANGS_COMMA" ] && args+=(--langs "$LANGS_COMMA")
    [ -n "$NEVER_COMMA" ] && args+=(--never-ext "$NEVER_COMMA")

    echo "  applying: bash $REPO_INIT ${args[*]}"
    if bash "$REPO_INIT" "${args[@]}" >"$SBOX_APPLY_LOG" 2>&1; then
        echo "  OK $claude_dir"
    else
        echo "  FAILED $claude_dir (rc=$?) -- see below" >&2
        sed 's/^/    /' "$SBOX_APPLY_LOG" >&2
    fi
}

SBOX_APPLY_LOG="$(mktemp 2>/dev/null || printf '/tmp/mc-update-apply-log.%s' "$$")"
trap 'rm -f "$SBOX_APPLY_LOG"' EXIT

# --- targeted mode: --add-lang / --never-ext -------------------------------

if [ -n "$ADD_LANG" ] || [ -n "$NEVER_EXT" ]; then
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

    DECIDE_ARGS=(wired --repo "$MC_REPO" --store "$STORE" --project "$PROJECT")
    OLD_IFS="$IFS"; IFS=';'
    for d in $CLAUDE_DIRS_SEMI; do [ -n "$d" ] && DECIDE_ARGS+=(--claude-dir "$d"); done
    for cr in $CODE_ROOTS_SEMI; do [ -n "$cr" ] && DECIDE_ARGS+=(--code-root "$cr"); done
    IFS="$OLD_IFS"
    [ -n "$LANGS_COMMA" ] && DECIDE_ARGS+=(--langs "$LANGS_COMMA")
    [ -n "$NEVER_COMMA" ] && DECIDE_ARGS+=(--never-ext "$NEVER_COMMA")

    echo "plan: $DECIDE wired --repo $MC_REPO --store $STORE --project $PROJECT (langs=$LANGS_COMMA never=$NEVER_COMMA claude-dirs=$CLAUDE_DIRS_SEMI code-roots=$CODE_ROOTS_SEMI)"
    for d in $(printf '%s' "$CLAUDE_DIRS_SEMI" | tr ';' ' '); do
        echo "plan: bash $REPO_INIT --project $PROJECT --store $STORE --claude-dir $d --non-interactive --langs $LANGS_COMMA --never-ext $NEVER_COMMA $(for cr in $(printf '%s' "$CODE_ROOTS_SEMI" | tr ';' ' '); do printf -- '--code-root %s ' "$cr"; done)"
    done

    # Consent is the command itself (see this mode's own usage text above):
    # applies unless --dry-run was explicitly given -- --apply is accepted
    # but redundant here, never required.
    if [ "$DRY_RUN_EXPLICIT" -eq 1 ]; then
        echo "(dry run -- drop --dry-run to write this)"
        exit 0
    fi

    if ! bash "$DECIDE" "${DECIDE_ARGS[@]}"; then
        echo "ERROR: could not rewrite the registry row -- see above" >&2
        exit 1
    fi
    RC=0
    for d in $(printf '%s' "$CLAUDE_DIRS_SEMI" | tr ';' ' '); do
        [ -n "$d" ] || continue
        REINIT_ARGS=(--project "$PROJECT" --store "$STORE" --claude-dir "$d" --non-interactive)
        for cr in $(printf '%s' "$CODE_ROOTS_SEMI" | tr ';' ' '); do
            [ -n "$cr" ] && REINIT_ARGS+=(--code-root "$cr")
        done
        [ -n "$LANGS_COMMA" ] && REINIT_ARGS+=(--langs "$LANGS_COMMA")
        [ -n "$NEVER_COMMA" ] && REINIT_ARGS+=(--never-ext "$NEVER_COMMA")
        if ! bash "$REPO_INIT" "${REINIT_ARGS[@]}"; then
            echo "ERROR: re-render failed for $d -- see above" >&2
            RC=1
        fi
    done
    exit "$RC"
fi

# --- default mode: walk every wired row -------------------------------

[ -f "$DECISIONS" ] || {
    echo "no registry at $DECISIONS -- nothing wired yet (run memcontinuum-setup.sh, then the memcontinuum skill)"
    exit 0
}

MIGRATE_HINTS=""

while IFS= read -r RAW_LINE || [ -n "$RAW_LINE" ]; do
    case "$RAW_LINE" in \#*|"") continue ;; esac
    KEY="${RAW_LINE%%"$MC_TAB"*}"
    REST="${RAW_LINE#*"$MC_TAB"}"
    IFS="$MC_TAB" read -r ROW_DECISION ROW_WHEN NOTE <<<"$REST"
    [ "$ROW_DECISION" = "wired" ] || continue

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
    if [ -z "$CLAUDE_DIRS_SEMI" ]; then
        LEGACY=1
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
                continue
                ;;
        esac
    fi

    # A legacy row's code-roots/langs/never are recovered ONCE, from the
    # first claude-dir's own rendered settings (repo-init installs one
    # coherent set per project; several claude-dirs for the same project
    # render the same set -- see INC-0104). Recovered here, before the
    # per-claude-dir walk, so every claude-dir's `migrate` apply (below)
    # uses the same recovered values, and the end-of-row registry rewrite
    # only has to happen once.
    if [ "$LEGACY" -eq 1 ]; then
        FIRST_CLAUDE_DIR="${CLAUDE_DIRS_SEMI%%;*}"
        mc_update_recover_from_settings "$PROJECT" "$FIRST_CLAUDE_DIR"
        [ -n "$CODE_ROOTS_SEMI" ] || CODE_ROOTS_SEMI="$MC_RECOVERED_CODE_ROOTS"
        [ -n "$LANGS_COMMA" ] || LANGS_COMMA="$MC_RECOVERED_LANGS"
        [ -n "$NEVER_COMMA" ] || NEVER_COMMA="$MC_RECOVERED_NEVER"
    fi

    OLD_IFS="$IFS"
    IFS=';'
    for CLAUDE_DIR in $CLAUDE_DIRS_SEMI; do
        IFS="$OLD_IFS"
        [ -n "$CLAUDE_DIR" ] || continue
        process_claude_dir "$CLAUDE_DIR"
        IFS=';'
    done
    IFS="$OLD_IFS"

    if [ "$LEGACY" -eq 1 ] && [ "$APPLY" -eq 1 ]; then
        MIGRATE_ARGS=(wired --repo "$KEY" --store "$STORE" --project "$PROJECT")
        OLD_IFS="$IFS"; IFS=';'
        for d in $CLAUDE_DIRS_SEMI; do [ -n "$d" ] && MIGRATE_ARGS+=(--claude-dir "$d"); done
        for cr in $CODE_ROOTS_SEMI; do [ -n "$cr" ] && MIGRATE_ARGS+=(--code-root "$cr"); done
        IFS="$OLD_IFS"
        [ -n "$LANGS_COMMA" ] && MIGRATE_ARGS+=(--langs "$LANGS_COMMA")
        [ -n "$NEVER_COMMA" ] && MIGRATE_ARGS+=(--never-ext "$NEVER_COMMA")
        # KEY is a path here (the only LEGACY branch that reaches this point
        # -- the remote-keyed one `continue`d above), so `--repo "$KEY"` is
        # safe: decide.sh re-derives the very same key from it.
        if bash "$DECIDE" "${MIGRATE_ARGS[@]}" >/dev/null 2>&1; then
            echo "  migrated: $KEY registry row now records claude-dirs/code-roots/langs/never" >&2
        else
            echo "  MIGRATE FAILED: $KEY -- registry row left as-is, re-render still applied above if it succeeded" >&2
        fi
    elif [ "$LEGACY" -eq 1 ]; then
        MIGRATE_HINTS="$MIGRATE_HINTS
  $KEY: run with --apply to migrate this row to the new registry format"
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
        echo "FAILED: machine layer refresh -- see above" >&2
    fi
fi

exit 0
