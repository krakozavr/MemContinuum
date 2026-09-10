#!/usr/bin/env bash
# usage: memcontinuum-update.sh [--dry-run | --apply] [--machine] [--repo PATH]
#        memcontinuum-update.sh --apply --repo PATH --claude-dir DIR [--claude-dir DIR ...]
#                               [--code-root DIR ...] [--langs LIST] [--set-never-ext LIST]
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
#   repo | claude-dir | stamped | engine | store-match | rules | skill | store-hooks | action
#     repo         the registry row's key (an origin remote URL, or a path)
#     claude-dir   one claude-dir this row lists (or recovers -- see below)
#     stamped      the MEMCONTINUUM_RENDERED value on this claude-dir's
#                  rendered hook lines ("none" = pre-stamp render)
#     engine       what this checkout would render right now: the REPO render
#                  fingerprint (see --machine for the other one)
#     store-match  yes/no/unknown -- the row's own store= vs the rendered
#                  MEMCONTINUUM_ROOT on those hook lines (a stamp match alone
#                  cannot catch a store renamed under the same engine
#                  version)
#     rules        ok/stale/missing/foreign -- <claude-dir>/rules/
#                  memcontinuum.md's own identity marker and stamp
#     skill        ok/stale/missing/foreign -- <claude-dir>/skills/
#                  memory-search/SKILL.md's own identity (a `name:
#                  memory-search` frontmatter line) and stamp
#     store-hooks  ok/stale/missing/foreign/not-checked -- the WORSE of
#                  STORE's git post-commit and pre-commit wrapper states
#                  (no render stamp of their own, so judged by re-deriving
#                  the exact bytes repo-init.sh would write now and comparing
#                  byte-for-byte); not-checked when the store is gone, git
#                  resolves its hooks dir outside its own .git (a shared
#                  core.hooksPath), or no python can be resolved to check
#                  against. Informational only -- unlike rules/skill, it does
#                  not feed `action` or --apply's decision (see
#                  mc_update_store_hooks_state's own comment for why, and
#                  what that narrows). Every OTHER rendered artifact this
#                  command does not put in a column of its own (STORE's
#                  README.md/.gitignore/tree -- write-once, never
#                  re-rendered, so there is no current/stale question that
#                  means anything for them) is still named, every walk, in a
#                  `not-checked: ...` line beneath the table -- never simply
#                  absent.
#     action       ok | stale | store-mismatch | store-form-stale |
#                  store-form-updated | rules-stale | rules-missing |
#                  rules-foreign | skill-foreign | migrate |
#                  migrate-needs-claude-dirs | migrate-needs-langs |
#                  migrate-needs-never-exts | migrate-dirs-disagree |
#                  store-missing | no-wiring | unrecoverable
#
#                  store-form-stale (reporting walk): the row's store= names
#                  the same store the wiring renders, written in an
#                  unresolved (symlinked) string form -- nothing is actually
#                  mismatched, and the remedy is to re-run with --apply.
#                  store-form-updated (--apply): that pass rewrites just the
#                  registry's store= field, leaving claude-dirs/code-roots/
#                  langs/never exactly as recorded. A store= naming a
#                  DIFFERENT store is store-mismatch, which this pair never
#                  stands in for.
#
#                  store-missing outranks every other answer, including a
#                  stamp and a store= that both look right: those compare
#                  strings, and a string can agree with a store that has been
#                  renamed or deleted. Nothing is re-rendered against a store
#                  that is not there. A missing or stale skill copy reports
#                  as plain `stale` (the same drift the hook-stamp check
#                  reports) rather than getting rules-missing/rules-stale's
#                  own names; a FOREIGN skill copy reports as `skill-foreign`
#                  and, like `rules-foreign`, is refused rather than applied.
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
# --machine  ALWAYS reported now, by default -- this flag is accepted for
#            compatibility (scripts that already pass it keep working) and
#            does nothing beyond what already happens. Reports the MACHINE
#            layer -- the detector hook and skill copy in the user-level
#            claude-dir memcontinuum-setup.sh installed into (it records
#            which one; ~/.claude is only the fallback), which no per-repo
#            install touches. The reported line names that directory. It is
#            compared against its own fingerprint, over its own inputs
#            (memcontinuum-setup.sh, the machine-level skill, the settings
#            merge), so an edit to any of those shows up HERE and not as
#            drift in every repository -- and a template change shows up in
#            the rows and not here. With --apply, re-runs
#            memcontinuum-setup.sh, but only when it is actually stale.
#            --machine makes no difference here -- it is a no-op either way
#            (see above). --no-machine does: it skips the machine layer,
#            and this refresh along with it, entirely (see --no-machine
#            below). Verified: `--apply --no-machine` on a stale layer
#            neither runs setup.sh nor writes config.sh.
#
#            This used to be off by default: a health check that answers
#            only for the layer it was asked about reads as "everything is
#            current" to the person running it, and real drift in the
#            machine-level skill went unreported for days because nobody
#            ever passed --machine. A silent, healthy-looking gap is worse
#            than one extra line most runs do not need, so this now always
#            prints (and --apply always fixes a stale one).
# --no-machine
#            the opt-out, for the rare caller that wants the repo rows alone
#            (e.g. a script that greps the table and cannot afford an extra
#            non-tabular line, or a run that must never shell out to
#            memcontinuum-setup.sh). Skips the machine layer entirely --
#            no report line, no refresh under --apply. Combining --machine
#            and --no-machine is refused, the same as --apply/--dry-run:
#            they are opposites, and "whichever came last" would mean the
#            same pair of flags reports or skips depending only on typing
#            order.
#
#            Right after a stale refresh, --apply --machine also reconciles
#            the pinned tree-sitter grammar wheels and the tree-sitter runtime:
#            when the refreshed
#            config.sh records an engine-managed venv (MEMCONTINUUM_VENV_MANAGED=1
#            -- memcontinuum-setup.sh's own venv, not a python you pointed it
#            at with --python), it reinstalls requirements.lock into that
#            venv, then runs backend-preflight and names any row still
#            missing. Against a python you supplied with --python, it never
#            installs anything -- it only reports what backend-preflight
#            finds missing there, and the remedy (install those pins
#            yourself, or re-run memcontinuum-setup.sh without --python for
#            a venv this command can maintain). A machine layer that is
#            already current runs neither step -- this reconciliation is
#            part of the refresh, not a separate check.
#
# MODES, AND WHICH FLAGS EACH ONE TAKES
#
# The same option names mean different things depending on what is being
# asked for, so each mode consumes a fixed set and REFUSES anything else,
# naming the mode and the flag. A flag accepted and quietly dropped is the
# worst answer this command could give: you typed what you wanted, it
# reported success, and it did something else.
#
#   walk        no --repo. Every wired row.
#               --dry-run --apply --machine --no-machine
#   targeted    --add-lang / --never-ext (with --repo). One row's language
#               and never-extension lists, additively, and nothing else.
#               --dry-run --repo --add-lang --never-ext
#   repo        --repo, without --add-lang/--never-ext. One row.
#               --dry-run --apply --machine --no-machine --repo --claude-dir
#
# In `repo` mode the remaining flags depend on the ROW, not on the command
# line, so they are settled when the row is read:
#
#   a row that RECORDS NO claude-dirs also takes --code-root, --langs and
#   --set-never-ext -- they supply what the row never wrote down. See below.
#
#   a CURRENT-FORMAT row already records all of that, so those three are
#   refused; there is nothing for them to supply, and this mode does not
#   rewrite what a row records. --claude-dir instead NARROWS the walk: it
#   means "re-render these dirs of this row and leave its others alone", and
#   every dir named must be one the row already records (a dir it does not is
#   refused as `dir-not-recorded`, never walked and never installed into).
#
# Two refusals apply to every mode, at the point the arguments are read:
#
#   an EMPTY or whitespace-only value for any flag that takes one
#   (--repo, --add-lang, --never-ext, --set-never-ext, --langs, --code-root,
#   --claude-dir) is refused. Each of these reads its own empty value as "not
#   given", so an empty one did not fail -- it changed what the command was.
#
#   --apply together with --dry-run is refused whichever order they are typed
#   in. They are opposites; "whichever came last" would mean the same pair of
#   flags writes or previews depending only on typing order, with the other
#   discarded in silence.
#
# --repo naming a repository with NO wired row -- undecided, or a recorded
# `declined` -- is refused (`no-wired-row: <key> (decision=...)`), never
# answered with an empty table at exit 0. An empty table reads as "checked,
# all current" for a repository this command had nothing to say about, and it
# is also where the row-dependent flags above would go unjudged: with no row
# to judge them against, they were neither consumed nor refused. Wiring a repo
# is the memcontinuum skill's job, with a human answering; this command only
# ever re-renders rows already marked `wired`.
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
#       not a plain extension list. Add --set-never-ext .ext[,.ext].
#   migrate-dirs-disagree       the named claude-dirs were recovered
#       separately -- as they must be, since nothing ever wrote this row's
#       parameters down -- and they do not hold the same code-roots,
#       languages or never-list. One row is one project, and one project has
#       one such set for all of its claude-dirs, so there is nothing here to
#       record. Both recoveries are printed; say which set is right with
#       --code-root DIR (repeatable), --langs LIST, --set-never-ext LIST.
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
#            re-renders every claude-dir that row lists with the new set,
#            and only THEN rewrites the row (memcontinuum-decide.sh wired,
#            every field). Render first, record second: a row written first
#            would describe a language set that exists nowhere the moment a
#            render failed, and every later re-render replays that claim.
#            --repo PATH is required -- same reasoning as
#            memcontinuum-decide.sh's own --repo requirement: no silent
#            $PWD default for a write that changes what gets indexed.
#
#            The re-render is all-or-nothing: every claude-dir is run with
#            --dry-run first, and only an all-clear turns into real writes.
#            The dirs on one row share one language set, so converting the
#            first and failing on the second would leave the project
#            describing itself two different ways.
#
#            Refused before anything is written anywhere: a row that records
#            no claude-dirs (migrate it first -- <repo>/.claude is never
#            substituted for a set the row does not name); a row that
#            records no code-roots (no-code-root -- the installer ignores
#            --langs without a --code-root, so nothing would render while
#            the row claimed it); a store that is gone; an unknown language;
#            and a recorded claude-dir carrying no wiring for this project
#            (dir-not-wired -- this command re-renders what is installed, it
#            never installs).
#            --never-ext ADDS; it has no second meaning. Supplying the whole
#            list for a migration is --set-never-ext, above. Combining
#            --add-lang or --never-ext with --apply is refused rather than
#            guessed at.
#
# This command never wires an undecided or declined repo (it only ever
# touches rows already marked `wired`), and never creates a store: every
# installer run it makes is passed --adopt-only, which refuses outright
# unless the store is already there. It never deletes or rewrites a store's
# own git history either -- re-rendering settings/rules/skill is all it does.
#
# Exit codes:
#   three refusals happen BEFORE any row is walked, and none of them is the
#   walk's own answer: an empty/whitespace-only flag value, --apply together
#   with --dry-run, and --repo naming a repository with no wired row -- each
#   exits non-zero (2, 2, 1) with nothing read and nothing written.
#   Past that point, the reporting walk (no --apply) always exits 0 -- a
#   stale row IS the answer there, not an error.
#   --apply exits 0 only when every claude-dir it walked ended up correct:
#   already ok, or re-rendered successfully. Anything left undone -- a failed
#   installer run, a dir deliberately skipped (store-missing, a foreign
#   rules file or skill copy, a migration this command must not guess at), or a row that
#   could not be resolved to a claude-dir at all (unrecoverable) -- exits
#   non-zero, with the table still printed and the reason on stderr.
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
MEMIDX="$ENGINE_ROOT/memidx.py"
# Where scripts/repo-init.sh's install_store_hook_wrapper points its exec
# line at -- needed here too, for the store-hooks health column's own
# byte-for-byte expected-content check (mc_store_hook_wrapper_state,
# scripts/mc-registry-lib.sh) to reconstruct exactly what repo-init.sh would
# write right now.
HOOKS_DIR="$ENGINE_ROOT/hooks"

# The bash that is running THIS script, for the scripts it shells out to.
# `bash` off PATH would silently hop interpreters mid-command -- which is
# exactly what made the bash-3.2 harness unable to reach any of this: it
# launches the entry point under a real 3.2.57 binary, and every nested call
# went straight back to the system's bash 5.
MC_BASH_BIN="${BASH:-bash}"

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
mc_render_fingerprint repo "$ENGINE_ROOT" || :
ENGINE_SHA="$MC_RENDER_FINGERPRINT"

# From the template that defines it, not a copy (mc_rules_identity_marker):
# this command and the installer must agree on what a rendered rules file
# looks like, or one of them calls the other's output foreign.
mc_rules_identity_marker "$ENGINE_ROOT" || {
    echo "cannot read $ENGINE_ROOT/templates/memcontinuum-rules.md (or it is empty) -- incomplete checkout" >&2
    exit 1
}
RULES_IDENTITY_MARKER="$MC_RULES_MARKER"

# Same idea, for the installed memory-search skill copy (mc_skill_identity_marker):
# repo-init.sh's own foreign-copy refusal reads this identity from the same
# place, at runtime, rather than either side hardcoding a copy of it.
mc_skill_identity_marker "$ENGINE_ROOT" || {
    echo "cannot read $ENGINE_ROOT/skills/memory-search/SKILL.md (or its frontmatter carries no name: line) -- incomplete checkout" >&2
    exit 1
}
SKILL_IDENTITY_MARKER="$MC_SKILL_MARKER"

APPLY=0
# Set only when --dry-run is literally typed -- distinct from APPLY's
# default-0, which the WALK mode reads as "no --apply given yet, preview".
# The targeted --add-lang/--never-ext mode below has the OPPOSITE default
# (a human typed the command, that IS the consent -- see its own usage text
# above): it applies unless --dry-run was explicitly given, so it must not
# key off APPLY's default the walk mode uses.
DRY_RUN_EXPLICIT=0
APPLY_EXPLICIT=0
# Default ON since INC-0117 (a health check that only answers for the layer
# it was asked about reads as "everything is current" for the layer it was
# not). --machine is kept, accepted and harmless, for compatibility;
# --no-machine is the new opt-out. MACHINE_EXPLICIT/NO_MACHINE_EXPLICIT track
# which one (if either) was actually TYPED, distinct from MACHINE's own
# default-derived value -- the targeted mode's refusal below, and the
# contradiction check right after arg parsing, both need to know that a human
# asked for one of these, not that the default happens to be 1.
MACHINE=1
MACHINE_EXPLICIT=0
NO_MACHINE_EXPLICIT=0
ADD_LANG=""
NEVER_EXT=""
LANGS_FLAG=""
SET_NEVER_EXT=""
SET_NEVER_GIVEN=0
TARGET_REPO=""
declare -a OVERRIDE_CLAUDE_DIRS=()
declare -a OVERRIDE_CODE_ROOTS=()
need_value() { [ $# -ge 2 ] || { echo "missing value for $1" >&2; exit 2; }; }

# need_value catches `--repo` with nothing after it. This catches `--repo ''`
# and `--repo '   '`, which are a different failure and a quieter one: EVERY
# one of these flags reads its own empty value as "not given", so an empty
# value did not fail -- it changed what the command was. `--repo ''` walked
# every wired row; `--langs ''` migrated with whatever it recovered instead of
# what was asked for; `--claude-dir ''` narrowed a recorded row's walk to
# nothing and exited 0 with no table, which is the shape of a clean bill of
# health for a repository it had just been told to re-render.
#
# `*[![:space:]]*` is "contains at least one non-whitespace character" -- false
# for both "" and "   ", and a plain POSIX glob, so bash 3.2 reads it too.
need_nonempty() {
    case "$2" in
        *[![:space:]]*) return 0 ;;
    esac
    echo "$1 needs a non-empty value" >&2
    echo "An empty value is not an answer, and this command will not read it as one -- every one of these flags would otherwise take it for the flag not being given at all, and quietly do something else." >&2
    exit 2
}
take_value() { need_value "$@"; need_nonempty "$1" "${2:-}"; }

while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run) APPLY=0; DRY_RUN_EXPLICIT=1; shift ;;
        --apply) APPLY=1; APPLY_EXPLICIT=1; shift ;;
        --machine) MACHINE=1; MACHINE_EXPLICIT=1; shift ;;
        --no-machine) MACHINE=0; NO_MACHINE_EXPLICIT=1; shift ;;
        --add-lang) take_value "$@"; ADD_LANG="$2"; shift 2 ;;
        --never-ext) take_value "$@"; NEVER_EXT="$2"; shift 2 ;;
        --langs) take_value "$@"; LANGS_FLAG="$2"; shift 2 ;;
        --set-never-ext) take_value "$@"; SET_NEVER_EXT="$2"; SET_NEVER_GIVEN=1; shift 2 ;;
        --code-root) take_value "$@"; OVERRIDE_CODE_ROOTS+=("$2"); shift 2 ;;
        --claude-dir) take_value "$@"; OVERRIDE_CLAUDE_DIRS+=("$2"); shift 2 ;;
        --repo) take_value "$@"; TARGET_REPO="$2"; shift 2 ;;
        -h|--help) usage 0 ;;
        *) echo "unknown argument: $1" >&2; usage 1 ;;
    esac
done

# --apply and --dry-run are opposites, and "whichever came last" is not a
# reading of them -- it is one of the two being discarded in silence, with the
# same pair of flags meaning WRITE or PREVIEW depending only on typing order.
# The one place this command must not guess is the one that decides whether it
# writes.
if [ "$APPLY_EXPLICIT" -eq 1 ] && [ "$DRY_RUN_EXPLICIT" -eq 1 ]; then
    echo "--apply and --dry-run are contradictory: one writes, the other only reports. Give exactly one (--dry-run is the default when neither is given)." >&2
    echo "Nothing was read and nothing was written." >&2
    exit 2
fi

# Same reasoning, same shape, for --machine/--no-machine: opposites, and
# "whichever came last" would mean the same pair of flags reports or skips
# the machine layer depending only on typing order.
if [ "$MACHINE_EXPLICIT" -eq 1 ] && [ "$NO_MACHINE_EXPLICIT" -eq 1 ]; then
    echo "--machine and --no-machine are contradictory: one reports the machine layer, the other skips it. Give at most one (the machine layer is reported by default when neither is given)." >&2
    echo "Nothing was read and nothing was written." >&2
    exit 2
fi

# --- which mode is this? ---------------------------------------------------
#
# --apply is the discriminator, and it is the only one, because the two modes
# want opposite things from the same three option names. `--never-ext .h`
# means "add .h to what this row already records" when a human types it on
# its own; under --apply it means "here is the whole never-list for the row
# you are migrating, because it could not be read back". Splitting them on
# --apply keeps each spelling doing one thing.
TARGETED=0
if [ -n "$ADD_LANG" ] || [ -n "$NEVER_EXT" ]; then
    TARGETED=1
fi

# --add-lang/--never-ext ADD to one row; the walk re-renders every row. They
# are not two readings of one command, and this refuses rather than picking
# one: an earlier version let --apply silently change what --never-ext MEANT
# (additive on its own, "the whole list" under --apply), which is exactly the
# kind of quiet reinterpretation a command that writes a registry must not do.
# The migration's own spellings are --langs and --set-never-ext.
if [ "$TARGETED" -eq 1 ] && [ "$APPLY" -eq 1 ]; then
    echo "--add-lang/--never-ext add to ONE row (which is why they take --repo); --apply re-renders every row. They cannot be combined." >&2
    echo "  to ADD a language or extension:  memcontinuum-update.sh --add-lang LANG --repo PATH   (typing it is the consent; no --apply needed)" >&2
    echo "  to supply the whole never-list while migrating a legacy row: --apply --repo PATH --claude-dir DIR --set-never-ext LIST" >&2
    exit 2
fi

# --- the flag/mode matrix is enforced here ----------------------------------
#
# What each mode consumes (and refuses) is documented once, in the --help
# text above ("MODES, AND WHICH FLAGS EACH ONE TAKES") -- this block and the
# ROW-dependent half further down (search this file for "only the ROW can
# settle") are what actually enforces it. Keep the prose there in sync with
# the checks here, not the other way around.
#
# refuse_flag MODE FLAG WHY
refuse_flag() {
    echo "$2 is not accepted in $1 mode: $3" >&2
    echo "Nothing was read and nothing was written. \`$0 --help\` lists what each mode takes." >&2
    exit 2
}

# require_wired_row KEY REPO -- refuses unless KEY has a `wired` row, naming
# the decision that IS recorded.
#
# The completing half of the matrix above. Every mode that takes --repo acts on
# one row, and the row-dependent flags (--claude-dir/--code-root/--langs/
# --set-never-ext) can only be judged against that row -- so with no row there
# was nothing to judge them against, and they were neither consumed nor
# refused: the walk matched nothing, printed an empty table, and exited 0.
# That reads as "checked, all current" for a repository this command never had
# anything to say about, and it silently swallowed whatever else was typed.
#
# `wired` is the only decision this command acts on -- it re-renders wiring
# that is already there and never wires anything -- so `declined` and "no row
# at all" are both refusals, and each names what it found so the answer is
# actionable rather than a bare miss.
#
# Leaves MC_LOOKUP_DECISION/WHEN/NOTE set on success (mc_registry_lookup's own
# out-parameter contract), so the caller reads the row it just validated
# instead of looking it up twice.
require_wired_row() {
    local key="$1" repo="$2" decision="none"
    if mc_registry_lookup "$DECISIONS" "$key"; then
        [ "$MC_LOOKUP_DECISION" = "wired" ] && return 0
        [ -z "$MC_LOOKUP_DECISION" ] || decision="$MC_LOOKUP_DECISION"
    fi
    echo "no-wired-row: $key (decision=$decision)" >&2
    case "$decision" in
        none)
            echo "  There is no wired row for $repo -- this repo is undecided: nobody has answered the MemContinuum question for it yet. Nothing was read further and nothing was written." >&2
            echo "  This command only ever re-renders rows already marked \`wired\`; wiring one is the memcontinuum skill's job, with a human answering." >&2
            ;;
        declined)
            echo "  There is no wired row for $repo -- the recorded answer is \`declined\`. Nothing was read further and nothing was written." >&2
            echo "  A declined repo is a recorded NO. If that has changed, record the new answer where answers are recorded: $DECIDE wired --repo $repo --store DIR --project NAME --claude-dir DIR" >&2
            ;;
        *)
            echo "  There is no wired row for $repo -- its row records decision=$decision, which this command does not act on. Nothing was read further and nothing was written." >&2
            ;;
    esac
    exit 1
}

WALK_WHY="--claude-dir/--code-root/--langs/--set-never-ext describe ONE registry row, so they need --repo PATH to say which. Without --repo this command walks every wired row and writes nothing it had to guess."
TARGETED_WHY="--add-lang/--never-ext re-render exactly the claude-dirs the row records, with the code-roots the row records -- they change its language and never-extension lists and nothing else. To change which dirs or code-roots a row records, say so where rows are written: $DECIDE wired --repo PATH ..."

if [ "$TARGETED" -eq 0 ]; then
    if [ -z "$TARGET_REPO" ]; then
        [ "${#OVERRIDE_CLAUDE_DIRS[@]}" -eq 0 ] || refuse_flag walk --claude-dir "$WALK_WHY"
        [ "${#OVERRIDE_CODE_ROOTS[@]}" -eq 0 ] || refuse_flag walk --code-root "$WALK_WHY"
        [ -z "$LANGS_FLAG" ] || refuse_flag walk --langs "$WALK_WHY"
        [ "$SET_NEVER_GIVEN" -eq 0 ] || refuse_flag walk --set-never-ext "$WALK_WHY"
    fi
    # `repo` mode: --claude-dir is always legal here (it names the migration's
    # set on a legacy row, and narrows the walk on a current-format one); the
    # other three are decided against the row, below.
else
    [ -n "$TARGET_REPO" ] || { echo "--add-lang/--never-ext requires an explicit repo: pass --repo PATH" >&2; exit 2; }
    [ "${#OVERRIDE_CLAUDE_DIRS[@]}" -eq 0 ] || refuse_flag targeted --claude-dir "$TARGETED_WHY"
    [ "${#OVERRIDE_CODE_ROOTS[@]}" -eq 0 ] || refuse_flag targeted --code-root "$TARGETED_WHY"
    [ -z "$LANGS_FLAG" ] || refuse_flag targeted --langs "$TARGETED_WHY (--add-lang is how this mode names a language.)"
    [ "$SET_NEVER_GIVEN" -eq 0 ] || refuse_flag targeted --set-never-ext "--set-never-ext supplies the WHOLE never-list for a legacy row's migration (--apply --repo PATH --claude-dir DIR --set-never-ext LIST). To add one extension to a row that already records its parameters, use --never-ext."
    # MACHINE defaults to 1 now (reported by default in walk/repo mode), so
    # the refusal below keys on MACHINE_EXPLICIT/NO_MACHINE_EXPLICIT -- whether
    # one of these was actually TYPED -- never on MACHINE's own value, or this
    # would refuse targeted mode unconditionally.
    [ "$MACHINE_EXPLICIT" -eq 0 ] || refuse_flag targeted --machine "this mode acts on one repository's row. The machine layer is a separate layer with its own command: $0 --apply --machine (or a plain $0, which now reports it by default)."
    [ "$NO_MACHINE_EXPLICIT" -eq 0 ] || refuse_flag targeted --no-machine "this mode never reports or touches the machine layer -- there is nothing here for --no-machine to opt out of."
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

# mc_update_artifact_state DEST IS_FOREIGN STAMP_LINE -- shared
# missing/foreign/stale/ok determination for a rendered artifact whose
# identity check the caller has already done (a rules file's fixed first
# line; the skill copy's frontmatter `name:` line -- the two shapes differ
# enough that identity detection stays per-artifact) and whose render-stamp
# line the caller has already located and handed over as STAMP_LINE (empty
# when none applies). Sets MC_ARTIFACT_STATE; always returns 0.
#
# The stamp is pulled out of the rendered `<!-- memcontinuum-rendered: ... -->`
# comment and compared through mc_fingerprint_match rather than matching the
# whole line as text: a file stamped `unknown` against an engine that also
# cannot fingerprint itself would otherwise compare EQUAL as text and read
# `ok`, which is exactly the claim nobody was able to check.
mc_update_artifact_state() {
    local dest="$1" is_foreign="$2" stamp_line="$3" stamp=""
    if [ ! -f "$dest" ]; then
        MC_ARTIFACT_STATE="missing"
        return 0
    fi
    if [ "$is_foreign" -ne 0 ]; then
        MC_ARTIFACT_STATE="foreign"
        return 0
    fi
    case "$stamp_line" in
        "<!-- memcontinuum-rendered: "*" -->")
            stamp="${stamp_line#<!-- memcontinuum-rendered: }"
            stamp="${stamp% -->}"
            ;;
    esac
    if mc_fingerprint_match "$stamp" "$ENGINE_SHA"; then
        MC_ARTIFACT_STATE="ok"
    else
        MC_ARTIFACT_STATE="stale"
    fi
    return 0
}

# mc_update_rules_state CLAUDE_DIR -- sets MC_RULES_STATE to one of
# ok/stale/missing/foreign for CLAUDE_DIR/rules/memcontinuum.md. The identity
# marker is the file's first line (mc_rules_identity_marker, from the
# template that defines it); the stamp comment repo-init renders sits on
# line 2, right after it.
mc_update_rules_state() {
    local dest="$1/rules/memcontinuum.md" is_foreign=0 stamp_line=""
    if [ -f "$dest" ]; then
        [ "$(sed -n '1p' "$dest")" = "$RULES_IDENTITY_MARKER" ] || is_foreign=1
        stamp_line="$(sed -n '2p' "$dest")"
    fi
    mc_update_artifact_state "$dest" "$is_foreign" "$stamp_line"
    MC_RULES_STATE="$MC_ARTIFACT_STATE"
    return 0
}

# mc_update_skill_state CLAUDE_DIR -- sets MC_SKILL_STATE to one of
# ok/stale/missing/foreign for CLAUDE_DIR/skills/memory-search/SKILL.md.
# Identity is mc_skill_copy_is_ours against SKILL_IDENTITY_MARKER
# (mc-registry-lib.sh; the marker read at runtime by mc_skill_identity_marker
# above, never a literal) -- the same predicate and the same marker
# scripts/repo-init.sh's own foreign-copy refusal uses, rather than either
# side hardcoding its own copy of "is this ours". Unlike the rules file,
# that identity cannot be a fixed first line -- the opening "---" has to
# stay byte 0 for the skill loader, so repo-init stamps right after the
# frontmatter's CLOSING "---" instead, and the predicate hands that boundary
# back as MC_SKILL_FM_END so this function does not re-find it.
#
# repo-init.sh refuses to overwrite a foreign copy at this path, the same as
# it does for the rules file -- so `skill-foreign` below is belt and braces
# with that refusal, not the only thing standing between a hand-authored
# file here and being overwritten (that WAS true before repo-init.sh grew
# its own check; it stayed true a moment longer for --add-lang/--never-ext,
# which call repo-init.sh directly and bypass this command's own action
# gating -- also closed now that repo-init.sh checks for itself).
mc_update_skill_state() {
    local dest="$1/skills/memory-search/SKILL.md" is_foreign=1 stamp_line=""
    if mc_skill_copy_is_ours "$dest" "$SKILL_IDENTITY_MARKER"; then
        is_foreign=0
        stamp_line="$(sed -n "$((MC_SKILL_FM_END + 1))p" "$dest")"
    fi
    mc_update_artifact_state "$dest" "$is_foreign" "$stamp_line"
    MC_SKILL_STATE="$MC_ARTIFACT_STATE"
    return 0
}

# mc_update_store_hooks_state -- sets MC_STORE_HOOKS_STATE, a combined
# ok/stale/missing/foreign/not-checked verdict for STORE's git post-commit
# and pre-commit wrappers (scripts/repo-init.sh install_store_hook_wrapper's
# two rendered artifacts -- the only artifacts it installs that carry no
# render stamp of their own; see mc_store_hook_wrapper_state's own comment
# in scripts/mc-registry-lib.sh for why currency has to be judged by
# re-deriving the exact expected bytes instead). Globals in: STORE, PROJECT.
#
# "not-checked" (never a guess) when:
#   - the store itself does not exist (mc_is_marked_store) -- nothing to
#     check the wrappers of;
#   - git resolves the store's hooks directory OUTSIDE its own .git (a
#     shared/global core.hooksPath) -- the exact condition repo-init.sh
#     itself refuses to install a wrapper into, so this command has no
#     location it may safely inspect either;
#   - no python can be resolved (mc_update_resolve_python) -- the expected
#     wrapper content names a python path, and this table does not guess at
#     one it cannot confirm repo-init.sh would actually use.
#
# Combined as the WORST of the two wrappers' individual states -- one column,
# not two, for the same "keep the table readable" reason rules/skill already
# stay single columns -- ranked foreign > missing > stale > ok: a foreign
# wrapper is a human decision repo-init.sh will never make for you (it skips
# and reports, same as a foreign rules file or skill copy); missing means the
# append-only guard is not installed at all; stale is the ordinary
# re-render-and-it's-fixed case.
#
# Deliberately informational only -- unlike rules/skill, NONE of
# stale/missing/foreign here feeds the `action` column or --apply's
# re-render decision (round-2 gate finding G4: an earlier version of this
# comment named only the stale/python-drift case below, but missing and
# foreign ride the identical action=ok / --apply-is-a-no-op path and are
# the same trade at a wider blast radius). repo-init.sh already regenerates
# (or correctly skips) both wrappers unconditionally on every real install
# it performs, so whenever --apply re-renders a claude-dir for any OTHER
# reason (stale hook lines, missing rules, ...) a stale store-hooks state
# is fixed as a side effect. --apply does NOT reach, ever, on its own:
#   - stale, when everything else is already `ok` and only a machine-level
#     python change made the wrapper's embedded python path stale;
#   - missing, when the append-only guard was never installed (deleted, or
#     a pre-this-feature store) -- --apply leaves it missing, exactly as
#     it leaves a missing rules file alone until something else triggers a
#     re-render;
#   - foreign, a hand-authored wrapper -- --apply leaves it in place, same
#     as a foreign rules file or skill copy.
# All three are named here rather than silently left unfixed and
# unmentioned; pinned by tests/test_update.py TestStoreHooksColumn
# .test_missing_and_foreign_leave_action_ok_and_apply_is_a_no_op.
mc_update_store_hooks_state() {
    MC_STORE_HOOKS_STATE="not-checked"
    mc_is_marked_store "$STORE" || return 0
    mc_store_hooks_dir "$STORE" || return 0
    local py
    py="$(mc_update_resolve_python)" || return 0
    local post pre
    mc_store_hook_wrapper_state "$MC_STORE_HOOKS_DIR/post-commit" "$HOOKS_DIR" \
        "post-commit-reindex.sh" "$STORE" "$PROJECT" "$py"
    post="$MC_STORE_HOOK_STATE"
    mc_store_hook_wrapper_state "$MC_STORE_HOOKS_DIR/pre-commit" "$HOOKS_DIR" \
        "pre-commit-append-only.sh" "$STORE" "$PROJECT" "$py"
    pre="$MC_STORE_HOOK_STATE"
    case "$post $pre" in
        *foreign*) MC_STORE_HOOKS_STATE="foreign" ;;
        *missing*) MC_STORE_HOOKS_STATE="missing" ;;
        *stale*)   MC_STORE_HOOKS_STATE="stale" ;;
        *)         MC_STORE_HOOKS_STATE="ok" ;;
    esac
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
    # The RAW rendered values, exactly as they sit on the hook line, beside
    # the normalized ones. Normalizing is lossy in the direction that matters
    # for comparing two dirs: `*.py` and `*.py *.zz` both come back as the
    # language list "python", but they are not the same wiring, and a
    # migration that treats them as equal replays one over the other.
    MC_RECOVERED_LANG_GLOBS=""
    MC_RECOVERED_NEVER_GLOBS=""
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

    # Design R5 (audit MC-P1-05, TOP-0123 L5): read MEMCONTINUUM_CODE_ROOTS
    # (JSON) from the write-side lines FIRST -- one always-present line
    # already carries every recorded root, so this recovers roots even for
    # a project with zero newfile-nudge lines (rationale-only wiring) or
    # wiring whose nudge lines were swept away for some other reason. The
    # newfile-nudge scan below stays the fallback for wiring rendered
    # BEFORE this task (an old-shape write-side line with no ROOTS token).
    #
    # Escaping note: the settings file is itself one JSON document, so the
    # array's internal double quotes are backslash-escaped in the RAW file
    # text mc_command_env_value reads (`MEMCONTINUUM_CODE_ROOTS='[\"a\", \"b\"]'`)
    # -- decoding needs one extra step (treat the captured text as the
    # interior of a JSON string literal) before json.loads sees a real
    # array. A root path containing a literal `'` would still truncate
    # mc_command_env_value's own capture at that point regardless of this
    # decode (documented, not solved -- pre-existing limitation of that
    # helper, MC-P1-05's own note).
    local root_json="" have_json_roots=0
    while IFS= read -r line || [ -n "$line" ]; do
        [ -n "$line" ] || continue
        mc_command_env_value "$line" "MEMCONTINUUM_CODE_ROOTS"
        if [ -n "$MC_ENV_VALUE" ]; then
            root_json="$MC_ENV_VALUE"
            break
        fi
    done < <(mc_wired_commands_for_project "$project" \
                 "$claude_dir/settings.local.json" "$claude_dir/settings.json")
    if [ -n "$root_json" ]; then
        local py2=""
        py2="$(mc_update_resolve_python)" || py2=""
        if [ -n "$py2" ]; then
            while IFS= read -r rp || [ -n "$rp" ]; do
                [ -n "$rp" ] || continue
                case ";$root_seen;" in
                    *";$rp;"*) ;;
                    *) roots+=("$rp"); root_seen="$root_seen;$rp" ;;
                esac
            done < <(MC_UPDATE_ROOTS_JSON="$root_json" PYTHONPATH= "$py2" -c '
import json, os
raw = os.environ.get("MC_UPDATE_ROOTS_JSON", "")
try:
    parsed = json.loads(raw)
except Exception:
    try:
        parsed = json.loads(json.loads(chr(34) + raw + chr(34)))
    except Exception:
        parsed = []
if isinstance(parsed, list):
    for r in parsed:
        if isinstance(r, str) and r:
            print(r)
' 2>/dev/null)
            [ "${#roots[@]}" -gt 0 ] && have_json_roots=1
        fi
    fi

    while IFS= read -r line || [ -n "$line" ]; do
        [ -n "$line" ] || continue
        case "$line" in
            *newfile-nudge.sh*)
                nudge_seen=1
                if [ "$have_json_roots" -eq 0 ]; then
                    mc_command_env_value "$line" "MEMCONTINUUM_CODE_ROOT"
                    if [ -n "$MC_ENV_VALUE" ]; then
                        case ";$root_seen;" in
                            *";$MC_ENV_VALUE;"*) ;;
                            *) roots+=("$MC_ENV_VALUE"); root_seen="$root_seen;$MC_ENV_VALUE" ;;
                        esac
                    fi
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
    # only ever carries the first root's env var into that hook's command,
    # regardless of how many --code-root values repo-init indexed).
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
    MC_RECOVERED_LANG_GLOBS="$lang_glob_str"
    MC_RECOVERED_NEVER_GLOBS="$never_glob_str"

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
            #
            # Both answers come back on ONE stdout, each behind a key, and
            # neither is read positionally. They used to be two bare lines
            # split on the newline between them -- but command substitution
            # strips TRAILING newlines, so when the notes line was empty (the
            # normal case: nothing is partial) there was no newline left to
            # split on, `${x#*\n}` matched nothing and handed back the whole
            # string, and the LANGUAGE LIST became the note. Every fully
            # rendered project was told it was "partially rendered -- python",
            # naming the one language that was perfectly fine.
            local lang_out=""
            lang_out="$(
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
print("MC_LANGS=" + ",".join(full))
print("MC_NOTE=" + " | ".join(notes))
PYEOF
            )"
            local lang_line lang_seen=0
            while IFS= read -r lang_line || [ -n "$lang_line" ]; do
                case "$lang_line" in
                    MC_LANGS=*) lang_list="${lang_line#MC_LANGS=}"; lang_seen=1 ;;
                    MC_NOTE=*)  MC_RECOVERED_LANG_NOTE="${lang_line#MC_NOTE=}" ;;
                esac
            done <<<"$lang_out"
            # No keyed answer at all means the helper did not run (a broken
            # checkout, an import failure -- its stderr is discarded because a
            # traceback is not this table's business). That is UNKNOWN, not
            # "no languages": recording the latter would turn code indexing
            # off for a project that had it on.
            if [ "$lang_seen" -eq 0 ]; then
                MC_RECOVERED_LANGS_OK=0
                lang_list=""
                MC_RECOVERED_LANG_NOTE=""
            fi
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

# mc_physical now lives in scripts/mc-registry-lib.sh (symlink-review round
# 3, concern 2 -- memcontinuum-decide.sh needed it too, and mc-registry-lib.sh
# is the one file both this script and memcontinuum-decide.sh already
# source, so moving it there needed no new sourcing wired up for either).
# See that file's own doc comment.

# mc_note_replace_field NOTE KEY NEW_VALUE
#
# Returns (printf) NOTE with KEY's token's value replaced by
# mc_note_encode(NEW_VALUE), every OTHER token (and their order) copied
# through byte-identical. KEY must already be present in NOTE -- this never
# ADDS a field a row never recorded, only corrects the STRING FORM of one
# that is already there (symlink-review round 1, finding 6+7: a row's
# store= recorded before Ruling 89, or typed directly into
# memcontinuum-decide.sh, may be the SAME store in an unresolved/symlinked
# form -- correcting just that one field is safe; rebuilding the whole note
# the way `memcontinuum-decide.sh wired` does would silently drop
# claude-dirs/code-roots/langs/never that call never re-typed).
mc_note_replace_field() {
    local note="$1" key="$2" new_value="$3" tok out="" first=1
    local -a toks=()
    read -ra toks <<<"$note"
    for tok in "${toks[@]:-}"; do
        case "$tok" in
            "$key="*) tok="$key=$(mc_note_encode "$new_value")" ;;
        esac
        if [ "$first" -eq 1 ]; then out="$tok"; first=0; else out="$out $tok"; fi
    done
    printf '%s' "$out"
}

TABLE_HEADER_PRINTED=0
print_row() {
    # print_row REPO CLAUDE_DIR STAMPED STORE_MATCH RULES SKILL STORE_HOOKS ACTION
    #
    # store-hooks (I2, updater-coverage workstream): ok/stale/missing/
    # foreign/not-checked for STORE's git post-commit and pre-commit
    # wrappers -- see mc_update_store_hooks_state's own comment. One more
    # column, deliberately not two (one per wrapper): the table is what a
    # human scans, and rules/skill already establish the one-column-per-
    # artifact-class precedent this follows rather than doubling.
    if [ "$TABLE_HEADER_PRINTED" -eq 0 ]; then
        printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "repo" "claude-dir" "stamped" "engine" "store-match" "rules" "skill" "store-hooks" "action"
        TABLE_HEADER_PRINTED=1
    fi
    printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$1" "$2" "$3" "$ENGINE_SHA" "$4" "$5" "$6" "$7" "$8"
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
    local rendered_root="" store_form_stale=0

    while IFS= read -r line || [ -n "$line" ]; do
        [ -n "$line" ] && lines+=("$line")
    done < <(mc_wired_commands_for_project "$PROJECT" \
                 "$claude_dir/settings.local.json" "$claude_dir/settings.json")

    for line in ${lines[@]+"${lines[@]}"}; do
        mc_command_env_value "$line" "MEMCONTINUUM_RENDERED"
        [ -n "$MC_ENV_VALUE" ] || MC_ENV_VALUE="none"
        if [ -z "$stamp" ]; then
            stamp="$MC_ENV_VALUE"
        elif [ "$stamp" != "$MC_ENV_VALUE" ]; then
            stamp="mixed"
        fi
        mc_command_env_value "$line" "MEMCONTINUUM_ROOT"
        if [ -n "$MC_ENV_VALUE" ] && [ -n "$STORE" ]; then
            [ -z "$rendered_root" ] && rendered_root="$MC_ENV_VALUE"
            if [ "$MC_ENV_VALUE" = "$STORE" ]; then
                [ "$store_match" = "unknown" ] && store_match="yes"
            else
                store_match="no"
            fi
        fi
    done
    [ -n "$stamp" ] || stamp="none"

    # A registry row's store= disagreeing with what is actually rendered
    # has two different causes, and they must never be reported the same
    # way (symlink-review round 1, findings 6+7): a GENUINE mismatch (the
    # row names a different store entirely -- store-mismatch, unchanged
    # below) versus the SAME physical store recorded in an unresolved
    # (symlinked) STRING form -- a row written before Ruling 89, or by a
    # human typing a symlinked path directly into memcontinuum-decide.sh.
    # mc_physical(STORE) == the rendered value proves the latter: nothing
    # is actually wrong on disk, only the registry's own record of the
    # path is stale.
    if [ "$store_match" = "no" ] && [ -n "$rendered_root" ] \
            && [ "$(mc_physical "$STORE")" = "$rendered_root" ]; then
        store_form_stale=1
    fi

    mc_update_rules_state "$claude_dir"
    mc_update_skill_state "$claude_dir"
    mc_update_store_hooks_state

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
    # and fail. store-missing, rules-foreign and skill-foreign are all
    # refusals the installer would only repeat more loudly -- repo-init.sh
    # itself refuses to overwrite a foreign rules file OR a foreign skill
    # copy, both before any mutation, both through the one identity
    # predicate this ranking's own MC_RULES_STATE/MC_SKILL_STATE checks use
    # (mc_rules_identity_marker, mc_skill_copy_is_ours -- mc-registry-lib.sh);
    # the migrate-needs-* answers are questions only a human can settle.
    # Everything below them is drift this command can and will re-render --
    # a missing or stale skill copy (Ruling 51) folds into the same generic
    # `stale` the hook-stamp check already reports, rather than getting its
    # own name the way rules-missing/rules-stale do: it is drift of the same
    # kind (a rendered artifact behind the engine), not a new question.
    #
    # store-missing outranks `no-wiring` too, and that is the whole point of
    # checking it before the wiring is even looked at. A claude-dir with none
    # of this project's hook lines is normally the skill's repair path --
    # "finish the install". But when the store is gone as well, sending a
    # human to re-install is sending them to seed a fresh store over a dead
    # one and call the result repaired. The dead store is the fact that has
    # to be said first.
    if ! mc_is_marked_store "$STORE"; then
        action="store-missing"
    elif [ "${#lines[@]}" -eq 0 ]; then
        action="no-wiring"
    elif [ "$MC_RULES_STATE" = "foreign" ]; then
        action="rules-foreign"
    elif [ "$MC_SKILL_STATE" = "foreign" ]; then
        action="skill-foreign"
    elif [ "$LEGACY" -eq 1 ]; then
        action="$LEGACY_ACTION"
    elif ! mc_fingerprint_match "$stamp" "$ENGINE_SHA"; then
        action="stale"
    elif [ "$store_match" = "no" ] && [ "$store_form_stale" -eq 1 ]; then
        # Same physical store, stale STRING form only -- never the plain
        # store-mismatch label, and never conflated across modes: walk
        # mode reports it (and only ever suggests --apply, never the
        # decide.sh `wired` remedy, which would drop every other field);
        # --apply mode's own dispatch below rewrites just the store=
        # field and reports the DISTINCT completed-action name.
        if [ "$APPLY" -eq 1 ]; then
            action="store-form-updated"
        else
            action="store-form-stale"
        fi
    elif [ "$store_match" = "no" ]; then
        action="store-mismatch"
    elif [ "$MC_RULES_STATE" = "missing" ]; then
        action="rules-missing"
    elif [ "$MC_RULES_STATE" = "stale" ]; then
        action="rules-stale"
    elif [ "$MC_SKILL_STATE" = "missing" ] || [ "$MC_SKILL_STATE" = "stale" ]; then
        action="stale"
    else
        action="ok"
    fi

    print_row "$KEY" "$claude_dir" "$stamp" "$store_match" "$MC_RULES_STATE" "$MC_SKILL_STATE" "$MC_STORE_HOOKS_STATE" "$action"

    # store-form-stale only ever fires with APPLY=0 (see above) -- the
    # hint's remedy is always "re-run with --apply", literally, never
    # `memcontinuum-decide.sh wired ...` (Verdict C's own finding: that
    # remedy rebuilds the note from scratch and silently drops every
    # claude-dirs/code-roots/langs/never field this row already carries).
    if [ "$action" = "store-form-stale" ]; then
        echo "  $KEY ($claude_dir): store= is recorded in an unresolved (symlinked) form, but the wiring already renders the physical path -- nothing is actually mismatched, so re-run with --apply to correct just the registry's store= field (claude-dirs/code-roots/langs/never are left exactly as recorded)." >&2
    fi

    # `no-wiring` is the one action --apply neither fixes nor fails on: a
    # claude-dir with none of this project's hook lines is a broken or
    # never-finished INSTALL, which is the memcontinuum skill's repair path
    # (a human is asked), not this command's. A `wired` row is never a licence
    # to wire anything.
    if [ "$APPLY" -eq 1 ]; then
        # Counted for a legacy row whatever happens next: the migration gate
        # compares this against the number that actually re-rendered.
        [ "$LEGACY" -eq 1 ] && MIGRATE_DIRS_WALKED=$((MIGRATE_DIRS_WALKED + 1))
        if [ "$action" != "ok" ] && [ "$action" != "no-wiring" ]; then
            # store_form_stale is passed through regardless of which action
            # WON the precedence chain above (symlink-review round 2,
            # NEW-2): a row can be BOTH fingerprint-stale (action="stale",
            # this diff's own repo-init.sh edits are themselves a
            # fingerprint input) AND store-form-stale on the very first
            # --apply after this rollout -- without this, only the SECOND
            # --apply (once the fingerprint already matches) would ever
            # reach the store-form branch at all. apply_claude_dir folds
            # the registry correction into the SAME re-render pass instead
            # of requiring a second one.
            apply_claude_dir "$claude_dir" "$action" "$store_form_stale"
        fi
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

# apply_store_form_fix CLAUDE_DIR -- the store-form-updated correction
# (symlink-review round 1, findings 6+7): rewrites ONLY the registry row's
# store= token to its physical form via mc_registry_rewrite_row
# (scripts/mc-registry-lib.sh), preserving claude-dirs/code-roots/langs/
# never byte-identical. Factored out (round 2, NEW-2) so it can run either
# on its own (action=store-form-updated, no re-render needed at all) or
# folded into the SAME --apply pass right after a successful re-render
# (action=stale etc. -- see apply_claude_dir below) -- one implementation,
# not two copies of the same three-line sequence.
apply_store_form_fix() {
    local claude_dir="$1" resolved_store new_note new_line
    resolved_store="$(mc_physical "$STORE")"
    new_note="$(mc_note_replace_field "$NOTE" "store" "$resolved_store")"
    # Decision stays "wired" -- this only ever runs for a row already
    # confirmed wired -- and the date refreshes, matching
    # memcontinuum-decide.sh's own convention that any registry write
    # stamps today.
    new_line="$(printf '%s\t%s\t%s\t%s' "$KEY" "wired" "$(date +%Y-%m-%d)" "$new_note")"
    if mc_registry_rewrite_row "$DECISIONS" "$KEY" "$new_line"; then
        echo "  OK $claude_dir: registry store= corrected to $resolved_store (claude-dirs/code-roots/langs/never unchanged)"
        return 0
    fi
    not_applied "  FAILED $claude_dir: could not rewrite the registry row's store= field -- see above"
    return 1
}

# apply_claude_dir CLAUDE_DIR ACTION [STORE_FORM_STALE] -- re-runs
# repo-init.sh for one claude-dir with this row's recorded parameters.
# Never invoked for action=ok. action=rules-foreign is reported but never
# applied here (repo-init.sh itself refuses a foreign rules file before
# writing anything -- calling it would just fail loudly for a reason
# already named in the table; the fix is a human moving the foreign file
# aside). action=skill-foreign is skipped for the same reason (repo-init.sh
# refuses a foreign skill copy before writing anything too, through the
# same mc_skill_copy_is_ours predicate this table's own skill column uses).
# STORE_FORM_STALE (default 0): whether THIS claude-dir's own
# process_claude_dir call independently found the registry's store= to be
# the same physical store in a stale string form -- passed through
# regardless of which action WON the walk's precedence chain (round 2,
# NEW-2: a row can be both fingerprint-stale, action=stale, and
# store-form-stale on the very first --apply after a repo-init.sh change
# like this one's own; without this, only a SECOND --apply, once the
# fingerprint already matches, would ever reach the dedicated
# store-form-updated action at all). When true, the registry correction
# (apply_store_form_fix) runs in the SAME pass, right after a successful
# re-render below -- never before, and never after a FAILED one.
apply_claude_dir() {
    local claude_dir="$1" action="$2" store_form_stale="${3:-0}"
    local -a args=()
    local cr

    case "$action" in
        rules-foreign)
            not_applied "  SKIPPED $claude_dir: rules-foreign -- move $claude_dir/rules/memcontinuum.md aside first (it was not rendered by this installer)"
            return 0
            ;;
        skill-foreign)
            not_applied "  SKIPPED $claude_dir: skill-foreign -- move $claude_dir/skills/memory-search/SKILL.md aside first (it was not rendered by this installer)"
            return 0
            ;;
    esac

    case "$action" in
        migrate-needs-*|migrate-dirs-disagree)
            not_applied "  SKIPPED $claude_dir: $action -- $MIGRATE_BLOCKED_HINT"
            return 0
            ;;
    esac

    # store-form-updated: the SAME physical store, only its STRING form in
    # the registry is stale (symlink-review round 1, findings 6+7) --
    # nothing here needs a re-render at all (the hook lines already carry
    # the physical path); apply_store_form_fix is the whole fix. Idempotent:
    # a multi-claude-dir row calls this once per dir, and every call
    # recomputes the identical corrected note from the same in-memory
    # $NOTE/$STORE, so a second write is a harmless no-op rewrite (same
    # content, refreshed date).
    if [ "$action" = "store-form-updated" ]; then
        apply_store_form_fix "$claude_dir"
        return 0
    fi

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
    if "$MC_BASH_BIN" "$REPO_INIT" "${args[@]}" >"$SBOX_APPLY_LOG" 2>&1; then
        echo "  OK $claude_dir"
        # The ONE place a dir counts as re-rendered. Everything above this
        # line -- every skip, every refusal -- leaves the count alone, which
        # is what makes the migration gate below say "all of them" rather
        # than "none of them failed loudly enough".
        MIGRATE_DIRS_RENDERED=$((MIGRATE_DIRS_RENDERED + 1))
        # One --apply pass must converge (round 2, NEW-2): a row can be
        # fingerprint-stale AND store-form-stale at once (this diff's own
        # repo-init.sh edits are themselves a fingerprint input, so every
        # wired row reads "stale" on the first --apply after it lands) --
        # fold the registry correction into this SAME successful re-render
        # instead of requiring a second --apply once the fingerprint
        # already matches. Never runs after a FAILED re-render (the `else`
        # branch below never reaches here).
        if [ "$store_form_stale" -eq 1 ]; then
            apply_store_form_fix "$claude_dir"
        fi
    else
        local rc=$?
        not_applied "  FAILED $claude_dir (rc=$rc) -- see below"
        sed 's/^/    /' "$SBOX_APPLY_LOG" >&2
    fi
}

SBOX_APPLY_LOG="$(mktemp 2>/dev/null || printf '/tmp/mc-update-apply-log.%s' "$$")"
trap 'rm -f "$SBOX_APPLY_LOG"' EXIT

# --- targeted mode: --add-lang / --never-ext -------------------------------

if [ "$TARGETED" -eq 1 ]; then
    # --repo, and every flag this mode does not consume, were settled by the
    # matrix above -- before the registry was read.
    if ! mc_repo_key "$TARGET_REPO"; then
        echo "not a git repository: $TARGET_REPO" >&2
        exit 1
    fi
    # Same refusal as the walk's, from the same place: a repo with no wired
    # row gets one answer, whichever mode asked.
    require_wired_row "$MC_REPO_KEY" "$MC_REPO"
    KEY="$MC_REPO_KEY"
    NOTE="$MC_LOOKUP_NOTE"
    mc_note_field "$NOTE" "store"; STORE="$MC_NOTE_FIELD"
    mc_note_field "$NOTE" "project"; PROJECT="$MC_NOTE_FIELD"
    mc_note_field "$NOTE" "claude-dirs"; CLAUDE_DIRS_SEMI="$MC_NOTE_FIELD"
    mc_note_field "$NOTE" "code-roots"; CODE_ROOTS_SEMI="$MC_NOTE_FIELD"
    mc_note_field "$NOTE" "langs"; EXISTING_LANGS_SEMI="$MC_NOTE_FIELD"
    mc_note_field "$NOTE" "never"; EXISTING_NEVER_SEMI="$MC_NOTE_FIELD"

    # A LEGACY row (no claude-dirs on record) is refused here, and no
    # <repo>/.claude is put in its place. This mode re-renders the dirs the
    # row NAMES; substituting the one directory this command can imagine is a
    # guess with two ways to be wrong -- a project's wiring may live in a
    # claude-dir outside the repo entirely (a session home), and it may live
    # in more than one. Either way the substituted dir gets installed into or
    # skipped, and the row is rewritten to describe a set nobody chose.
    if [ -z "$CLAUDE_DIRS_SEMI" ]; then
        echo "this row records no claude-dirs (it predates the registry recording them), and --add-lang/--never-ext re-render the dirs a row NAMES. $MC_REPO/.claude is not substituted for them: a project's wiring can live outside the repo, and in more than one place." >&2
        echo "Migrate the row first -- name the full claude-dir set:" >&2
        echo "  $0 --apply --repo $MC_REPO --claude-dir DIR [--claude-dir DIR ...]" >&2
        echo "then re-run this command. Nothing was written." >&2
        exit 1
    fi
    [ -n "$STORE" ] && [ -n "$PROJECT" ] || {
        echo "row for $TARGET_REPO has no recorded store/project -- re-run repo-init and memcontinuum-decide.sh wired with --store/--project first" >&2
        exit 1
    }

    # No code-roots means no code indexing is wired for this project at all,
    # and repo-init IGNORES --langs/--never-ext without a --code-root to wire
    # them into. Recording a language set here would put a value in the
    # registry that renders nowhere and that every later re-render replays --
    # a row describing wiring that does not exist.
    if [ -z "$CODE_ROOTS_SEMI" ]; then
        echo "no-code-root: this row records no code-roots, so there is no code indexing here for a language or never-extension to apply to -- repo-init ignores --langs/--never-ext without a --code-root, so nothing would render and the row would claim something that is wired nowhere." >&2
        echo "Add a code-root first:" >&2
        echo "  $DECIDE wired --repo $MC_REPO --store $STORE --project $PROJECT --claude-dir DIR [--claude-dir DIR ...] --code-root DIR" >&2
        echo "and re-run repo-init there. Nothing was written." >&2
        exit 1
    fi

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

    # Every recorded dir must ALREADY carry this project's wiring before the
    # installer is pointed at it. A row is a record, not a warrant: it can name
    # a directory that was never installed (hand-edited, or installed and then
    # wiped), and running repo-init there would WIRE IT FROM SCRATCH -- this
    # command's one prohibition. Checked for every dir before any of them is
    # touched, so the refusal costs nothing half-done.
    #
    # Scoped to THIS project (mc_wired_commands_for_project), not to any
    # MemContinuum wiring: one claude-dir can carry two projects' hook lines,
    # and the other project's presence says nothing about this one's.
    for TD in ${TARGET_CLAUDE_DIRS[@]+"${TARGET_CLAUDE_DIRS[@]}"}; do
        [ -n "$TD" ] || continue
        if ! mc_wired_commands_for_project "$PROJECT" \
                "$TD/settings.local.json" "$TD/settings.json" >/dev/null; then
            echo "dir-not-wired: the row records $TD, but it carries no wiring for project $PROJECT. This command re-renders what is installed; it never wires a directory from scratch (the memcontinuum skill does that, with a human answering)." >&2
            echo "Either install it there first, or drop it from the row:" >&2
            echo "  $DECIDE wired --repo $MC_REPO --store $STORE --project $PROJECT --claude-dir DIR [--claude-dir DIR ...]" >&2
            echo "Nothing was written -- no dir was re-rendered and the registry row is unchanged." >&2
            exit 1
        fi
    done

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
    # ALL-OR-NOTHING, as far as two installer runs can be made to be. Every
    # claude-dir is DRY-RUN first, and only an all-clear turns into real
    # writes. The dirs on one row share one language/never set by
    # construction, so re-rendering the first with a new set and then failing
    # on the second leaves a project half-converted -- two claude-dirs
    # disagreeing about what it indexes, and a registry row describing
    # neither. repo-init's refusals (a foreign rules file, an unwritable
    # claude-dir, a store that went missing) all fire in --dry-run, before it
    # writes anything, which is what makes the preflight worth running.
    RC=0
    for d in ${TARGET_CLAUDE_DIRS[@]+"${TARGET_CLAUDE_DIRS[@]}"}; do
        REINIT_ARGS=(--project "$PROJECT" --store "$STORE" --claude-dir "$d" --non-interactive --adopt-only --dry-run)
        REINIT_ARGS+=(${WIRING_ARGS[@]+"${WIRING_ARGS[@]}"})
        if ! "$MC_BASH_BIN" "$REPO_INIT" "${REINIT_ARGS[@]}" >"$SBOX_APPLY_LOG" 2>&1; then
            echo "ERROR: $d would not re-render:" >&2
            sed 's/^/    /' "$SBOX_APPLY_LOG" >&2
            RC=1
        fi
    done
    if [ "$RC" -ne 0 ]; then
        echo "ERROR: at least one claude-dir could not be re-rendered, so NONE of them was. The dirs on this row share one language and never-extension set; converting some of them would leave the project describing itself two different ways. Nothing was written anywhere and the registry row is unchanged -- fix what is named above and re-run." >&2
        exit 1
    fi

    APPLIED_DIRS=""
    for d in ${TARGET_CLAUDE_DIRS[@]+"${TARGET_CLAUDE_DIRS[@]}"}; do
        REINIT_ARGS=(--project "$PROJECT" --store "$STORE" --claude-dir "$d" --non-interactive --adopt-only)
        REINIT_ARGS+=(${WIRING_ARGS[@]+"${WIRING_ARGS[@]}"})
        if ! "$MC_BASH_BIN" "$REPO_INIT" "${REINIT_ARGS[@]}"; then
            # Its dry run passed and the real run did not, so something
            # changed underneath us (a permission, a disk, a concurrent edit).
            # The preflight cannot rule this out, and an installer run cannot
            # be rolled back -- so the one thing left is to say exactly what
            # is now inconsistent, rather than exiting with a bare failure
            # over a project that is genuinely half-converted.
            echo "ERROR: re-render failed for $d -- see above. Its dry run had passed, so this failed part way." >&2
            echo "DRIFT: ${APPLIED_DIRS:-(no dir)} re-rendered with the new parameters; $d did not. The registry row was NOT changed, so it still describes the old set -- which now matches neither half. Fix what failed above and re-run this same command to converge (re-rendering an already-current dir is a no-op)." >&2
            exit 1
        fi
        APPLIED_DIRS="${APPLIED_DIRS:+$APPLIED_DIRS, }$d"
    done

    if ! "$MC_BASH_BIN" "$DECIDE" "${DECIDE_ARGS[@]}"; then
        echo "ERROR: the re-render succeeded but the registry row could not be rewritten -- see above. Re-run this command to try the row again." >&2
        exit 1
    fi
    exit 0
fi

# --- default mode: walk every wired row -------------------------------

# No registry is not an error, and it is not a reason to skip --machine
# either: the machine layer can perfectly well be installed on a machine where
# no repository has been wired yet -- that is what a fresh setup looks like.
DECISIONS_SRC="$DECISIONS"
[ -f "$DECISIONS" ] || {
    echo "no registry at $DECISIONS -- nothing wired yet (run memcontinuum-setup.sh, then the memcontinuum skill)"
    DECISIONS_SRC=/dev/null
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
    # Before the walk, not during it: a --repo that matches no wired row must
    # be an answer, not an empty table.
    require_wired_row "$MC_REPO_KEY" "$MC_REPO"
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
        print_row "$KEY" "(unknown)" "none" "unknown" "unknown" "unknown" "unknown" "unrecoverable"
        echo "  no project= recorded for $KEY -- re-run memcontinuum-decide.sh wired --repo ... --store ... --project ... to fix" >&2
        # Work left undone is work left undone: --apply promised to end with
        # every walked row correct, and this one was never even resolved to a
        # claude-dir. `no-wiring` is the sole exception to that rule; a row
        # this command cannot read is not one.
        [ "$APPLY" -eq 1 ] && WALK_RC=1
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
                # mc_physical: this becomes the row's claude-dirs=, and
                # repo-init.sh (re-run below with this same --claude-dir)
                # resolves its own copy physically -- see mc_physical's
                # own comment.
                CLAUDE_DIRS_SEMI="${CLAUDE_DIRS_SEMI:+$CLAUDE_DIRS_SEMI;}$(mc_physical "$OD")"
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
                    print_row "$KEY" "(unknown)" "none" "unknown" "unknown" "unknown" "unknown" "unrecoverable"
                    echo "  legacy row, no claude-dirs recorded, and the key is a remote URL (not a path) -- this row's claude-dir cannot be recovered automatically. Fix: memcontinuum-decide.sh wired --repo PATH --store $STORE --project $PROJECT --claude-dir DIR [--code-root DIR ...] [--langs LIST]" >&2
                    [ "$APPLY" -eq 1 ] && WALK_RC=1
                    continue
                    ;;
            esac
        fi
    fi

    # The half of the flag/mode matrix that only the ROW can settle. A
    # current-format row already records its code-roots, languages and
    # never-list; the migration overrides exist to supply what a legacy row
    # never wrote down, so here they have nothing to supply and are refused
    # rather than accepted and dropped. (Changing what a recorded row says is
    # --add-lang/--never-ext, additively, or a decide.sh line.)
    #
    # --claude-dir is the exception, and it changes meaning rather than being
    # refused: on a recorded row it NARROWS the walk to the dirs it names --
    # "re-render these and leave the rest alone" -- and each must be one the
    # row already records. A dir that is not on the row is refused
    # (dir-not-recorded) rather than walked: this command re-renders what a
    # row describes, and a dir the row has never heard of is not that.
    if [ "$LEGACY" -eq 0 ]; then
        [ "${#OVERRIDE_CODE_ROOTS[@]}" -eq 0 ] || refuse_flag repo --code-root \
            "$KEY already records code-roots=${CODE_ROOTS_SEMI:-(none)}. --code-root/--langs/--set-never-ext supply a LEGACY row's parameters during migration; this row is already migrated."
        [ -z "$LANGS_FLAG" ] || refuse_flag repo --langs \
            "$KEY already records langs=${LANGS_SEMI:-(none)}. To add one: $0 --add-lang LANG --repo $TARGET_REPO. To set the whole list: $DECIDE wired --repo $TARGET_REPO ... --langs LIST."
        [ "$SET_NEVER_GIVEN" -eq 0 ] || refuse_flag repo --set-never-ext \
            "$KEY already records never=${NEVER_SEMI:-(none)}. To add one: $0 --never-ext .ext --repo $TARGET_REPO. To set the whole list: $DECIDE wired --repo $TARGET_REPO ... --never-ext LIST."
        if [ "${#OVERRIDE_CLAUDE_DIRS[@]}" -gt 0 ]; then
            SUBSET_SEMI=""
            for OD in "${OVERRIDE_CLAUDE_DIRS[@]}"; do
                [ -n "$OD" ] || continue
                OD_FOUND=0
                mc_split_semi "$CLAUDE_DIRS_SEMI"
                for RD in ${MC_SPLIT[@]+"${MC_SPLIT[@]}"}; do
                    [ "$RD" = "$OD" ] && OD_FOUND=1
                done
                if [ "$OD_FOUND" -eq 0 ]; then
                    echo "dir-not-recorded: --claude-dir $OD is not one of the claude-dirs $KEY records ($CLAUDE_DIRS_SEMI). On a row that records its dirs, --claude-dir narrows the walk to some of THEM; it does not add one." >&2
                    echo "To record another claude-dir for this row: $DECIDE wired --repo $TARGET_REPO --store $STORE --project $PROJECT --claude-dir $OD [--claude-dir DIR ...]" >&2
                    echo "Nothing was read further and nothing was written." >&2
                    exit 2
                fi
                SUBSET_SEMI="${SUBSET_SEMI:+$SUBSET_SEMI;}$OD"
            done
            CLAUDE_DIRS_SEMI="$SUBSET_SEMI"
        fi
    fi

    # A legacy row's code-roots/langs/never are recovered from EVERY one of
    # its claude-dirs, not from whichever happens to be first.
    #
    # The doctrine the recovery has to hold up: one row is one project, and a
    # project has ONE code-root/language/never set, applied to all of its
    # claude-dirs. Reading the first dir and replaying its parameters onto the
    # rest ASSUMES that -- and the whole reason a legacy row is being migrated
    # is that nothing ever wrote the parameters down, so nothing enforced it
    # either. Two dirs installed months apart can genuinely differ. Replaying
    # the first one's set would silently re-render the second with languages
    # it never indexed and a never-list it never had, and record the result as
    # if a human had chosen it. So: recover per dir, and when the dirs
    # disagree, refuse and show both recoveries. The human resolves it with
    # explicit --code-root/--langs/--set-never-ext, which is the only place
    # that answer can come from.
    LEGACY_ACTION="migrate"
    MIGRATE_BLOCKED_HINT=""
    # Reset per row. The registry row is rewritten only when EVERY one of this
    # row's claude-dirs actually re-rendered in this run -- counted, rather
    # than inferred from "no failure was recorded". A skip is not a success:
    # a dir refused for a dead store or a foreign rules file was never
    # re-rendered, and a row rewritten after it describes parameters that dir
    # does not carry -- which every future re-render then replays.
    MIGRATE_DIRS_WALKED=0
    MIGRATE_DIRS_RENDERED=0
    if [ "$LEGACY" -eq 1 ]; then
        FIRST_CLAUDE_DIR="${CLAUDE_DIRS_SEMI%%;*}"

        # A named claude-dir must ALREADY carry this project's wiring. Checked
        # BEFORE the recovery below, not after: recovering from a dir with no
        # wiring yields three empty answers, which would then read as a
        # disagreement with the dirs that do have wiring -- the right refusal
        # for the wrong reason. This command records what is installed; it
        # never wires a directory from scratch -- that is the skill's job,
        # with a human answering.
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

        REC_CODE_ROOTS=""
        REC_LANGS=""
        REC_NEVER=""
        # The RAW rendered values of the first dir, which every later dir is
        # compared against. Comparison is on these, NOT on the normalized
        # language names: what a re-render replays is the value, and `*.py`
        # against `*.py *.zz` normalizes to the same "python" while being two
        # different things on disk. Recording either as the answer for both
        # would silently change what one of them indexes.
        REC_LANG_GLOBS=""
        REC_NEVER_GLOBS=""
        REC_LANGS_OK=1
        REC_NEVER_OK=1
        REC_NOTES=""
        REC_FIRST=1
        REC_REPORT=""
        REC_NL=$'\n'
        DISAGREE_CODE_ROOTS=0
        DISAGREE_LANGS=0
        DISAGREE_NEVER=0
        mc_split_semi "$CLAUDE_DIRS_SEMI"
        for RD in ${MC_SPLIT[@]+"${MC_SPLIT[@]}"}; do
            mc_update_recover_from_settings "$PROJECT" "$RD"
            REC_REPORT="$REC_REPORT
      $RD: code-roots=${MC_RECOVERED_CODE_ROOTS:-(none)} lang-exts='$MC_RECOVERED_LANG_GLOBS' never-exts='$MC_RECOVERED_NEVER_GLOBS' (languages: ${MC_RECOVERED_LANGS:-none})"
            # "cannot be read back" from ANY dir blocks the whole row: the row
            # describes all of them at once.
            [ "$MC_RECOVERED_LANGS_OK" -eq 0 ] && REC_LANGS_OK=0
            [ "$MC_RECOVERED_NEVER_OK" -eq 0 ] && REC_NEVER_OK=0
            # Notes are collected PER DIR and every one is printed. Keeping
            # only the first dir's meant the row's report described whichever
            # dir happened to sort first -- so a fully rendered first dir hid
            # a partially rendered second one completely.
            if [ -n "$MC_RECOVERED_LANG_NOTE" ]; then
                REC_NOTES="${REC_NOTES:+$REC_NOTES$REC_NL}  $KEY [$RD]: partially rendered -- $MC_RECOVERED_LANG_NOTE"
            fi
            if [ "$REC_FIRST" -eq 1 ]; then
                REC_CODE_ROOTS="$MC_RECOVERED_CODE_ROOTS"
                REC_LANGS="$MC_RECOVERED_LANGS"
                REC_NEVER="$MC_RECOVERED_NEVER"
                REC_LANG_GLOBS="$MC_RECOVERED_LANG_GLOBS"
                REC_NEVER_GLOBS="$MC_RECOVERED_NEVER_GLOBS"
                REC_FIRST=0
            else
                [ "$MC_RECOVERED_CODE_ROOTS" = "$REC_CODE_ROOTS" ] || DISAGREE_CODE_ROOTS=1
                [ "$MC_RECOVERED_LANG_GLOBS" = "$REC_LANG_GLOBS" ] || DISAGREE_LANGS=1
                [ "$MC_RECOVERED_NEVER_GLOBS" = "$REC_NEVER_GLOBS" ] || DISAGREE_NEVER=1
            fi
        done

        [ -n "$CODE_ROOTS_SEMI" ] || CODE_ROOTS_SEMI="$REC_CODE_ROOTS"
        [ -n "$LANGS_COMMA" ] || LANGS_COMMA="$REC_LANGS"
        [ -n "$NEVER_COMMA" ] || NEVER_COMMA="$REC_NEVER"
        # Command-line overrides win over anything recovered -- they are the
        # human's answer to exactly the question the recovery could not settle.
        # An overridden field is also no longer a disagreement: the answer has
        # been given, so what the dirs happen to hold no longer decides it.
        if [ "${#OVERRIDE_CODE_ROOTS[@]}" -gt 0 ]; then
            # mc_physical: this becomes the row's code-roots=, and
            # repo-init.sh (re-run below with these same --code-root
            # values) resolves its own copy physically -- see
            # mc_physical's own comment.
            declare -a OVERRIDE_CODE_ROOTS_PHYSICAL=()
            for cr in "${OVERRIDE_CODE_ROOTS[@]}"; do
                OVERRIDE_CODE_ROOTS_PHYSICAL+=("$(mc_physical "$cr")")
            done
            CODE_ROOTS_SEMI="$(join_semi "${OVERRIDE_CODE_ROOTS_PHYSICAL[@]}")"
            DISAGREE_CODE_ROOTS=0
        fi
        if [ -n "$LANGS_FLAG" ]; then
            LANGS_COMMA="$LANGS_FLAG"
            DISAGREE_LANGS=0
        fi
        if [ "$SET_NEVER_GIVEN" -eq 1 ]; then
            NEVER_COMMA="$SET_NEVER_EXT"
            DISAGREE_NEVER=0
        fi

        DISAGREE_FIELDS=""
        [ "$DISAGREE_CODE_ROOTS" -eq 1 ] && DISAGREE_FIELDS="${DISAGREE_FIELDS:+$DISAGREE_FIELDS, }code-roots"
        [ "$DISAGREE_LANGS" -eq 1 ] && DISAGREE_FIELDS="${DISAGREE_FIELDS:+$DISAGREE_FIELDS, }langs"
        [ "$DISAGREE_NEVER" -eq 1 ] && DISAGREE_FIELDS="${DISAGREE_FIELDS:+$DISAGREE_FIELDS, }never"

        # Anything the rendered extension list carries that the recovered
        # language set does not account for is said out loud, under the row it
        # belongs to. The table has no note column, and a difference nobody is
        # told about is how a migration records less than what is wired.
        if [ -n "$REC_NOTES" ] && [ -z "$LANGS_FLAG" ]; then
            printf '%s\n' "$REC_NOTES" >&2
            echo "  $KEY: recorded languages would be ${LANGS_COMMA:-(none)}. Pass --langs LIST to record something else." >&2
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
        elif [ -n "$DISAGREE_FIELDS" ]; then
            LEGACY_ACTION="migrate-dirs-disagree"
            MIGRATE_BLOCKED_HINT="the claude-dirs named for this row do not agree on: $DISAGREE_FIELDS. One row is one project, and one project has ONE code-root, language and never-extension set, applied to every claude-dir it wires -- so there is no single answer to record here, and picking one dir's would silently re-render the others with parameters they never had. What each dir carries right now:$REC_REPORT
  Say which set this project has, and re-run with the same --claude-dir flags plus the fields that disagree:
    $0 --apply --repo $KEY --claude-dir DIR [--claude-dir DIR ...] [--code-root DIR ...] [--langs LANG[,LANG]] [--set-never-ext .ext[,.ext]]"
        elif [ "$REC_LANGS_OK" -eq 0 ] && [ -z "$LANGS_FLAG" ]; then
            LEGACY_ACTION="migrate-needs-langs"
            MIGRATE_BLOCKED_HINT="the wiring at $FIRST_CLAUDE_DIR was rendered before the language set was written onto the hook line, so which languages this project indexes cannot be read back. That is UNKNOWN, not none -- recording it as none would turn code indexing off for a project that had it on. Name the set and re-run:
    $0 --apply --repo $KEY --claude-dir $FIRST_CLAUDE_DIR --langs LANG[,LANG]"
        elif [ "$REC_NEVER_OK" -eq 0 ] && [ "$SET_NEVER_GIVEN" -eq 0 ]; then
            LEGACY_ACTION="migrate-needs-never-exts"
            MIGRATE_BLOCKED_HINT="the never-mention list rendered at $FIRST_CLAUDE_DIR is not a plain extension list, so it cannot be read back. Name it and re-run:
    $0 --apply --repo $KEY --claude-dir $FIRST_CLAUDE_DIR --set-never-ext .ext[,.ext]"
        fi
    fi

    mc_split_semi "$CLAUDE_DIRS_SEMI"
    for CLAUDE_DIR in ${MC_SPLIT[@]+"${MC_SPLIT[@]}"}; do
        process_claude_dir "$CLAUDE_DIR"
    done

    # Named explicitly, once per row -- never silently: scripts/repo-init.sh
    # also renders $STORE/README.md, $STORE/.gitignore and the store tree
    # (topics/incidents/investigations/concepts/sources/inbox/*), all
    # write-if-absent (steps 1-2 of that script) and never touched again by
    # any later re-render, by design -- an "adopt an existing store" install
    # must not clobber hand-authored provenance notes in a README a human
    # already edited. There is no fingerprint stamp on them and no
    # "current/stale" question that means anything for a file this command
    # will never rewrite, so "not checked" is the honest and complete
    # answer, not a gap -- named here so the coverage test
    # (tests/test_update.py TestUpdaterCoversEveryRenderedArtifact) can
    # confirm every artifact repo-init.sh renders is accounted for
    # somewhere in this output, one way or another, and never simply absent.
    if mc_is_marked_store "$STORE"; then
        echo "not-checked: $STORE/README.md $STORE/.gitignore $STORE (tree: topics incidents investigations concepts sources inbox/codex inbox/grok inbox/audit) -- written once at install, never re-rendered"
    fi

    if [ "$LEGACY" -eq 1 ] && [ "$APPLY" -eq 1 ] && [ "$LEGACY_ACTION" = "migrate" ] \
           && [ "$MIGRATE_DIRS_WALKED" -gt 0 ] \
           && [ "$MIGRATE_DIRS_RENDERED" -eq "$MIGRATE_DIRS_WALKED" ]; then
        MIGRATE_ARGS=(wired --repo "$KEY" --store "$STORE" --project "$PROJECT")
        mc_split_semi "$CLAUDE_DIRS_SEMI"
        for d in ${MC_SPLIT[@]+"${MC_SPLIT[@]}"}; do MIGRATE_ARGS+=(--claude-dir "$d"); done
        mc_build_wiring_args "$CODE_ROOTS_SEMI" "$LANGS_COMMA" "$NEVER_COMMA"
        MIGRATE_ARGS+=(${MC_BUILT_ARGS[@]+"${MC_BUILT_ARGS[@]}"})
        # KEY is a path here (the only LEGACY branch that reaches this point
        # -- the remote-keyed one `continue`d above), so `--repo "$KEY"` is
        # safe: decide.sh re-derives the very same key from it.
        if "$MC_BASH_BIN" "$DECIDE" "${MIGRATE_ARGS[@]}" >/dev/null 2>&1; then
            echo "  migrated: $KEY registry row now records claude-dirs/code-roots/langs/never" >&2
        else
            not_applied "  MIGRATE FAILED: $KEY -- registry row left as-is, re-render still applied above if it succeeded"
        fi
    elif [ "$LEGACY" -eq 1 ] && [ "$APPLY" -eq 1 ]; then
        : # Refused up front (migrate-needs-*/migrate-dirs-disagree), or at
          # least one claude-dir did not end up re-rendered -- it failed, or
          # it was skipped for a dead store or a foreign rules file. Either
          # way the row is left exactly as it was, `migrated:` is not printed,
          # and not_applied has already made the walk exit non-zero.
    elif [ "$LEGACY" -eq 1 ]; then
        if [ -n "$MIGRATE_BLOCKED_HINT" ]; then
            MIGRATE_HINTS="$MIGRATE_HINTS
  $KEY ($LEGACY_ACTION): $MIGRATE_BLOCKED_HINT"
        else
            MIGRATE_HINTS="$MIGRATE_HINTS
  $KEY: run with --apply to migrate this row to the new registry format"
        fi
    fi
done < "$DECISIONS_SRC"

if [ -n "$MIGRATE_HINTS" ]; then
    echo >&2
    echo "legacy rows found (pre-dates claude-dirs/code-roots/langs/never in the registry):" >&2
    printf '%s\n' "$MIGRATE_HINTS" >&2
fi

# --- the machine layer, compared against its OWN fingerprint ---------------
#
# Separate from every row above, because it is a separate layer: the detector
# hook and the user-level skill live in ~/.claude and are re-rendered by
# memcontinuum-setup.sh, not by any per-repo install. Comparing it against the
# repo fingerprint would report a template change as machine drift and an edit
# to memcontinuum-setup.sh as drift in every repository -- neither of which
# the reported command would fix.
if [ "$MACHINE" -eq 1 ]; then
    mc_render_fingerprint machine "$ENGINE_ROOT" || :
    MACHINE_ENGINE="$MC_RENDER_FINGERPRINT"

    # WHICH claude-dir the machine layer lives in is a fact only
    # memcontinuum-setup.sh knows -- it takes --claude-dir and defaults to
    # ~/.claude -- so it records it in config.sh and this reads it back.
    # Assuming ~/.claude reported a real install at a custom dir as absent
    # ("rendered by none"), and --apply then rendered a SECOND machine layer
    # at the default path: two detector hooks, two skill copies, and the one
    # that was actually stale still stale.
    MACHINE_CLAUDE_DIR=""
    if [ -f "$MEMCONTINUUM_HOME/config.sh" ]; then
        MACHINE_CLAUDE_DIR="$(
            . "$MEMCONTINUUM_HOME/config.sh" >/dev/null 2>&1
            printf '%s' "${MEMCONTINUUM_MACHINE_CLAUDE_DIR:-}"
        )"
    fi
    # Fallback only when nothing is on record (a config.sh written before
    # setup recorded it) -- setup's own default, so the answer is the same one
    # that install would have used.
    [ -n "$MACHINE_CLAUDE_DIR" ] || MACHINE_CLAUDE_DIR="$HOME/.claude"

    MACHINE_STAMP="none"
    if mc_first_command_matching "memcontinuum-detect.sh" \
            "$MACHINE_CLAUDE_DIR/settings.json" "$MACHINE_CLAUDE_DIR/settings.local.json"; then
        mc_command_env_value "$MC_WIRED_COMMAND" "MEMCONTINUUM_RENDERED"
        [ -n "$MC_ENV_VALUE" ] && MACHINE_STAMP="$MC_ENV_VALUE"
    fi
    if mc_fingerprint_match "$MACHINE_STAMP" "$MACHINE_ENGINE"; then
        MACHINE_ACTION="ok"
    else
        MACHINE_ACTION="stale"
    fi

    # The detector hook's own stamp (above) is memcontinuum-setup.sh's ONE
    # rendered artifact that carries a fingerprint; the machine-level skill
    # copy it also installs carries no stamp of its own at all (unlike the
    # per-repo skill/rules files) -- INC-0117 was exactly this copy drifting
    # while nothing checked it directly, only ever inferred through the hook
    # line's stamp (true only because setup.sh happens to write both in the
    # same run, sequentially -- an inference, not a check). Compared here
    # byte-for-byte against this engine's own copy instead: setup.sh writes
    # it verbatim (a plain `cp`, no template substitution), so byte equality
    # is the exact and complete answer, needing no stamp. Purely informational
    # -- it does not feed MACHINE_ACTION or --apply's refresh decision below,
    # both of which stay keyed on the hook stamp alone; --apply's
    # memcontinuum-setup.sh re-run already rewrites this file unconditionally
    # whenever it runs at all, and a stamp-current, byte-stale skill copy
    # would need its own separate diagnosis this table does not attempt.
    MACHINE_SKILL_STATE="not-checked"
    if [ -f "$ENGINE_ROOT/skills/memcontinuum/SKILL.md" ]; then
        if [ -f "$MACHINE_CLAUDE_DIR/skills/memcontinuum/SKILL.md" ]; then
            if cmp -s "$ENGINE_ROOT/skills/memcontinuum/SKILL.md" \
                      "$MACHINE_CLAUDE_DIR/skills/memcontinuum/SKILL.md"; then
                MACHINE_SKILL_STATE="ok"
            else
                MACHINE_SKILL_STATE="stale"
            fi
        else
            MACHINE_SKILL_STATE="missing"
        fi
    fi

    # config.sh: not rendered from a template (memcontinuum-setup.sh writes
    # its own values straight out), so "current/stale" does not apply -- only
    # "is it there at all", which is what every python/store resolution in
    # this whole engine depends on existing. Reported for the same reason as
    # the skill copy above: naming what was NOT independently checked, rather
    # than folding it silently into the hook-stamp verdict.
    MACHINE_CONFIG_STATE="missing"
    [ -f "$MEMCONTINUUM_HOME/config.sh" ] && MACHINE_CONFIG_STATE="present"

    echo
    echo "machine: $MACHINE_CLAUDE_DIR rendered by $MACHINE_STAMP, engine at $MACHINE_ENGINE, skill $MACHINE_SKILL_STATE, config $MACHINE_CONFIG_STATE -- $MACHINE_ACTION"

    if [ "$APPLY" -eq 1 ]; then
        REFRESH_OK=0
        if [ "$MACHINE_ACTION" = "ok" ]; then
            echo "machine layer already current -- nothing to refresh"
            # Nothing was re-rendered, and nothing needed to be: the stamp
            # matched the engine, so the config.sh already on disk is the one
            # a refresh would have written. It is safe to READ below.
            REFRESH_OK=1
        else
            echo "refreshing machine layer: $MC_BASH_BIN $SETUP --claude-dir $MACHINE_CLAUDE_DIR"
            PY="$(mc_update_resolve_python)" || PY=""
            SETUP_ARGS=(--claude-dir "$MACHINE_CLAUDE_DIR" --no-model-warm)
            [ -n "$PY" ] && SETUP_ARGS=(--claude-dir "$MACHINE_CLAUDE_DIR" --python "$PY" --no-model-warm)
            if "$MC_BASH_BIN" "$SETUP" "${SETUP_ARGS[@]}"; then
                echo "OK: machine layer refreshed"
                REFRESH_OK=1
            else
                not_applied "FAILED: machine layer refresh -- see above"
            fi
        fi

        # --- Task 9 (B4/TOP-0118): dependency reconciliation -------------------
        # Two different things live here, and they answer to different gates.
        #
        # The pip REINSTALL runs only after a refresh actually re-rendered the
        # machine layer, re-reading the config.sh that refresh just wrote (it
        # may have rewritten MEMCONTINUUM_VENV_MANAGED via its own sticky-flag
        # determination, see memcontinuum-setup.sh). A second, non-stale
        # --apply --machine run must not re-invoke pip: that is the
        # no-op-reconciliation guarantee.
        #
        # The backend-preflight REPORT runs on every --apply --machine run,
        # stale or not. It installs nothing and changes nothing; and the
        # condition it reports -- a grammar wheel or the tree-sitter runtime
        # installed at a version the row does not pin -- is a runtime-install
        # condition. Someone pip-installs a newer grammar into the venv and
        # nothing about the wiring goes stale, so a report gated on staleness
        # would stay silent for exactly the case it exists to catch.
        #
        # The REFRESH_OK gate on the config read is load-bearing for both.
        # not_applied only records WALK_RC=1 and returns, so a FAILED setup
        # used to fall through to the pip install -- against a config.sh that
        # failed setup never rewrote. A setup that dies at its own python
        # version gate, for instance, leaves the previous run's
        # MEMCONTINUUM_PYTHON and MEMCONTINUUM_VENV_MANAGED=1 on disk while
        # the python this run was told to use is a different, foreign one:
        # reconciliation would then pip-install the lockfile into a python the
        # engine does not own.
        MANAGED_PY=""
        MANAGED_FLAG="0"
        RECONCILED=0
        # mc_config_managed_python (scripts/mc-registry-lib.sh) is the ONE
        # implementation of this read, shared with memcontinuum-setup.sh's
        # own sticky-flag determination. Both need the DISK truth rather
        # than the ordinary env-wins resolution mc_update_resolve_python
        # does, and both would otherwise read a caller's own
        # MEMCONTINUUM_PYTHON back as if it were what config.sh records --
        # see that function for the full reason.
        if [ "$REFRESH_OK" -eq 1 ] && mc_config_managed_python "$MEMCONTINUUM_HOME/config.sh"; then
            MANAGED_PY="$MC_CONFIG_PYTHON"
            MANAGED_FLAG="$MC_CONFIG_MANAGED"
        fi
        if [ -n "$MANAGED_PY" ]; then
            if [ "$MACHINE_ACTION" != "ok" ] && [ "$MANAGED_FLAG" = "1" ]; then
                echo "machine: reinstalling requirements.lock into the engine-managed venv ($MANAGED_PY)"
                RECONCILE_LOG="$(mktemp 2>/dev/null || printf '/tmp/mc-reconcile-log.%s' "$$")"
                if PYTHONPATH= "$MANAGED_PY" -m pip install -r "$ENGINE_ROOT/requirements.lock" >"$RECONCILE_LOG" 2>&1; then
                    echo "OK: dependencies reconciled"
                    RECONCILED=1
                elif command -v uv >/dev/null 2>&1 \
                        && PYTHONPATH= uv pip install --python "$MANAGED_PY" -r "$ENGINE_ROOT/requirements.lock" >>"$RECONCILE_LOG" 2>&1; then
                    # A `-m pip` failure here almost always means a
                    # uv-created managed venv (uv venv omits pip/
                    # setuptools/wheel by default -- verified against the
                    # real uv on this machine: `-m pip` raises "No module
                    # named pip" in a plain `uv venv` target). uv's own
                    # installer needs no pip inside the target venv at
                    # all, so it is the fallback here, not the first
                    # attempt -- same preference order scripts/repo-init.sh's
                    # own bootstrap_venv already uses (uv over pip when
                    # both could apply).
                    echo "OK: dependencies reconciled (via uv)"
                    RECONCILED=1
                else
                    cat "$RECONCILE_LOG" >&2
                    not_applied "FAILED: dependency reconciliation into $MANAGED_PY -- see above"
                fi
                rm -f "$RECONCILE_LOG"
            fi
            PREFLIGHT_JSON="$(PYTHONPATH= "$MANAGED_PY" "$MEMIDX" backend-preflight --json 2>/dev/null)" || PREFLIGHT_JSON=""
            if [ -n "$PREFLIGHT_JSON" ]; then
                # Two lines out, one python: line 1 is the rows whose
                # backend cannot run here at all, line 2 the rows that
                # run at a version their pin does not name (ruling 108's
                # pin-mismatch state). Both need saying, and they need
                # different remedies, so neither is folded into the other.
                PREFLIGHT_SUMMARY="$(printf '%s' "$PREFLIGHT_JSON" | PYTHONPATH= "$MANAGED_PY" -c '
import json, sys
data = json.load(sys.stdin)
missing = [lang for lang, row in data.items() if not row["ok"]]
mismatched = [lang for lang, row in data.items() if row.get("state") == "pin-mismatch"]
print(",".join(sorted(missing)))
print(",".join(sorted(mismatched)))
')"
                MISSING="$(printf '%s\n' "$PREFLIGHT_SUMMARY" | sed -n '1p')"
                MISMATCHED="$(printf '%s\n' "$PREFLIGHT_SUMMARY" | sed -n '2p')"
                if [ -n "$MISMATCHED" ]; then
                    echo "machine: WARNING installed versions differ from the pins for: $MISMATCHED -- run 'PYTHONPATH= $MANAGED_PY $MEMIDX backend-preflight' for the pinned and installed version of each"
                fi
                if [ -n "$MISSING" ]; then
                    if [ "$RECONCILED" -eq 1 ]; then
                        echo "machine: WARNING still missing after reinstall: $MISSING"
                    elif [ "$MANAGED_FLAG" = "1" ]; then
                        echo "machine: WARNING $MISSING not installed in the engine-managed venv ($MANAGED_PY) -- run 'PYTHONPATH= $MANAGED_PY -m pip install -r $ENGINE_ROOT/requirements.lock' to install the pinned wheels into it"
                    else
                        echo "machine: $MISSING not available in $MANAGED_PY -- install the pinned tree-sitter grammar wheels and the tree-sitter runtime (see requirements.lock) into it yourself, or re-run memcontinuum-setup.sh without --python to get an engine-managed venv this updater can maintain"
                    fi
                fi
            fi
        fi
        # --- end Task 9 dependency reconciliation -----------------------------
    fi
fi

exit "$WALK_RC"
