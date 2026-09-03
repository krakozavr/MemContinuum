#!/usr/bin/env bash
# memlib.sh -- shared helpers for MemContinuum's write-side reminder hooks
# (ledger-post-edit.sh, precompact-persist.sh, sessionstart-remind.sh,
# userprompt-remind.sh, sessionend-stamp.sh). Source this from each hook
# script; it is never executed standalone.
#
# hooks/newfile-nudge.sh (a PreToolUse hook) also sources this file, but
# ONLY for mc_path_under_root below, and only lazily (right before its own
# containment check, after every earlier early-exit) -- it deliberately
# keeps its own separate PY/LOG/PROJECT resolution rather than adopting
# this file's (see that file's own header for why), so most invocations
# never pay this file's mkdir/config.sh cost at all.
#
# Contract mirrors pre-edit-chain.sh (docs/DESIGN.md SS8 / docs/DESIGN.md
# ruling F): hard-clear PYTHONPATH, absolute venv python, MEMCONTINUUM_* env,
# a single hook.log, fail-open on every path. Every hook that sources this is
# still individually responsible for its own final `exit 0` -- memlib.sh never
# exits or traps on the caller's behalf.
#
# macOS port (docs/DESIGN.md SS8 port note, 2026-08-30): stock macOS bash is
# 3.2 and ships neither `flock` nor `timeout`. This file used to shell out to
# both; it no longer uses either anywhere. Locking now lives inside the one
# python process mc_update_state_json already spawns (real fcntl.flock, a 2s
# non-blocking-retry deadline, atomic tmp+rename write, see below). The
# overall per-call deadline that `timeout 2` used to provide is now the job
# of each CALLING hook script's own watchdog guard (a tiny python launcher
# that runs the whole script in its own process group and kills the group on
# a 2s budget -- see the top of each hooks/*.sh file) -- so no helper in this
# file wraps its own subprocess calls in any per-call timeout any more; the
# caller's watchdog bounds the entire run, including every helper call made
# along the way.
#
# Env (project-agnostic; concrete values belong only in project wiring, e.g.
# .claude/settings.json or a *.json.example next to it -- never in this repo):
#   MEMCONTINUUM_HOME        base dir for the index db, hook.log, and session
#                        state ($MEMCONTINUUM_HOME/sessions/<project>/<id>.json).
#                        Defaults to ~/.memcontinuum (memidx.py's own default).
#   MEMCONTINUUM_PROJECT     project namespace passed to memidx.py --project.
#                        Defaults to $(basename "$MEMCONTINUUM_ROOT"), else
#                        "default" (memidx.py's own DEFAULT_PROJECT).
#   MEMCONTINUUM_ROOT        store markdown root (the decision-chain repo).
#   MEMCONTINUUM_CODE_ROOT   code root these hooks watch edits under.
#   MEMCONTINUUM_PYTHON      absolute path to the venv python. Falls back to
#                        <engine>/.venv/bin/python (see scripts/repo-init.sh
#                        --bootstrap-venv) when unset.
#
# WRITE-LOCK (ruling E): these scripts' only writable surface is
# $MEMCONTINUUM_HOME/sessions/**/*.json[.lock] and $MEMCONTINUUM_HOME/hook.log.
# Never write anything under MEMCONTINUUM_ROOT (the store) or
# MEMCONTINUUM_CODE_ROOT (the code tree) from any function in this file or any
# script that sources it.

export PYTHONPATH=

MC_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
MC_MEMIDX="$MC_LIB_DIR/../memidx.py"
# Python resolution order (README.md "Requirements" / memcontinuum-setup.sh):
#   $MEMCONTINUUM_PYTHON -> $MEMCONTINUUM_HOME/config.sh -> <engine>/.venv/bin/python
#   -> (left unresolved; every caller here fails open, so a missing python
#   surfaces as a logged outcome, never a blocked hook -- see mc_log below and
#   each script's own finish()).
#
# The config.sh step exists because a venv does not have to live at
# <engine>/.venv: point --venv anywhere, or hand memcontinuum-setup.sh an existing
# --python, and the last fallback is wrong. Without this step every hook line
# in every project has to carry MEMCONTINUUM_PYTHON by hand, and the one that
# forgets fails silently -- observed in the field, hooks dead for two days
# behind a "no python resolved" line nobody was reading. config.sh is shell
# rather than JSON precisely so this costs a `.` and no interpreter.
# R2/R3 fix, round 4: a custom-HOME install also writes a minimal POINTER
# config.sh at the fixed default path (memcontinuum-setup.sh "3. config")
# that records only the real MEMCONTINUUM_HOME. Source the default/env
# path first; if that just redefined MEMCONTINUUM_HOME to a DIFFERENT
# directory than the file we sourced, it was a pointer -- follow through
# and source the REAL config.sh too, so MEMCONTINUUM_PYTHON actually
# resolves there instead of silently falling back to the engine venv (and
# so this script's own MC_LOG/MC_DB_PATH below land under the real HOME,
# not the default one -- R3). Unconditional on MEMCONTINUUM_PYTHON already
# being set: config.sh's own `if [ -z "${MEMCONTINUUM_PYTHON:-}" ]` guard
# keeps env/baked precedence for PYTHON either way. Both sources are
# fail-open (`|| true`): a missing/corrupt config.sh only costs sourcing
# time, never blocks the hook.
MEMCONTINUUM_HOME="${MEMCONTINUUM_HOME:-$HOME/.memcontinuum}"
MC_HOME_CONFIG_1="$MEMCONTINUUM_HOME/config.sh"
if [ -f "$MC_HOME_CONFIG_1" ]; then
    # shellcheck source=/dev/null
    . "$MC_HOME_CONFIG_1" 2>/dev/null || true
fi
# Re-default after every source: a damaged-but-sourceable config may have
# `unset MEMCONTINUUM_HOME`, and under `set -u` a bare expansion would
# kill the hook (regate round 2).
MEMCONTINUUM_HOME="${MEMCONTINUUM_HOME:-$HOME/.memcontinuum}"
if [ "$MEMCONTINUUM_HOME/config.sh" != "$MC_HOME_CONFIG_1" ] && [ -f "$MEMCONTINUUM_HOME/config.sh" ]; then
    # shellcheck source=/dev/null
    . "$MEMCONTINUUM_HOME/config.sh" 2>/dev/null || true
    MEMCONTINUUM_HOME="${MEMCONTINUUM_HOME:-$HOME/.memcontinuum}"
fi
unset MC_HOME_CONFIG_1
if [ -n "${MEMCONTINUUM_PYTHON:-}" ]; then
    MC_PY="$MEMCONTINUUM_PYTHON"
else
    MC_PY="$MC_LIB_DIR/../.venv/bin/python"
fi
MC_LOG="$MEMCONTINUUM_HOME/hook.log"
mkdir -p "$MEMCONTINUUM_HOME" 2>/dev/null || true

# Project resolution moved ABOVE the "no python resolved" check (liveness
# metric fix: memidx.py stats needs `project=` on every hook.log line,
# including this fail-open one -- MC_PROJECT costs nothing to compute this
# early, it is env/basename-only, no python involved).
MC_PROJECT="${MEMCONTINUUM_PROJECT:-}"
if [ -z "$MC_PROJECT" ]; then
    if [ -n "${MEMCONTINUUM_ROOT:-}" ]; then
        MC_PROJECT="$(basename "$MEMCONTINUUM_ROOT")"
    else
        MC_PROJECT="default"
    fi
fi

if [ ! -x "$MC_PY" ]; then
    printf '%s memlib: no python resolved (checked MEMCONTINUUM_PYTHON, %s, %s) -- run memcontinuum-setup.sh project=%s\n' \
        "$(date -Iseconds 2>/dev/null || date)" "$MEMCONTINUUM_HOME/config.sh" \
        "$MC_LIB_DIR/../.venv/bin/python" "$MC_PROJECT" >>"$MC_LOG" 2>/dev/null || true
fi

MC_DB_PATH="$MEMCONTINUUM_HOME/$MC_PROJECT.sqlite"

# mc_log MESSAGE -- append one timestamped line to hook.log, with
# `project=$MC_PROJECT` always appended at the end (liveness metric:
# memidx.py stats groups hook.log by project; every line from this shared
# path must carry one, matching pre-edit-chain.sh's own independent logger,
# which already stamps project= -- see that file's finish()). Never fails
# the calling hook (logging failure is swallowed, not propagated).
mc_log() {
    printf '%s %s project=%s\n' "$(date -Iseconds 2>/dev/null || date)" "$1" "$MC_PROJECT" >>"$MC_LOG" 2>/dev/null || true
}

# mc_state_dir_for PROJECT
mc_state_dir_for() {
    printf '%s/sessions/%s' "$MEMCONTINUUM_HOME" "$1"
}

# mc_state_file_for PROJECT SESSION_ID
mc_state_file_for() {
    printf '%s/%s.json' "$(mc_state_dir_for "$1")" "$2"
}

# mc_extract_fields PAYLOAD_JSON FIELD...
# Reads PAYLOAD_JSON on stdin (never argv, never an env var -- see dual-gate
# review finding 1 below) and prints one shlex-quoted `NAME=value` line per
# requested field,
# suitable for `eval "$(mc_extract_fields ...)"`. Supported field tokens:
# any top-level payload key (uppercased for the shell var name), plus the
# special "tool_input.file_path" -> FILE_PATH, "_prompt_hash" -> PROMPT_HASH
# (sha256(payload["prompt_id"])[:16] -- the raw prompt_id itself is never
# extracted or emitted, dual-gate review finding 2), and "_top_keys_csv" ->
# TOP_KEYS_CSV (sorted top-level KEY NAMES only, comma-joined, never
# values -- the payload-shape capture addendum). Never reads
# transcript_path, user_input, last_assistant_message, or any payload VALUE
# beyond an explicitly requested scalar field on purpose -- callers must
# not ask for them (docs/DESIGN.md ruling B). The payload is
# piped to this one python's stdin only -- never placed in an env var or
# another process's argv (dual-gate review finding 1). No per-call timeout
# here (see the file header): the calling hook's own watchdog bounds this.
mc_extract_fields() {
    local payload="$1"
    shift
    printf '%s' "$payload" | env PYTHONPATH= "$MC_PY" -c '
import hashlib, json, sys, shlex
fields = sys.argv[1:]
try:
    d = json.load(sys.stdin)
except Exception:
    d = {}
if not isinstance(d, dict):
    d = {}
for f in fields:
    if f == "tool_input.file_path":
        v = (d.get("tool_input") or {}).get("file_path") or ""
        name = "FILE_PATH"
    elif f == "_prompt_hash":
        pid = d.get("prompt_id") or ""
        v = hashlib.sha256(pid.encode()).hexdigest()[:16] if pid else ""
        name = "PROMPT_HASH"
    elif f == "_top_keys_csv":
        v = ",".join(sorted(d.keys()))
        name = "TOP_KEYS_CSV"
    else:
        v = d.get(f)
        if v is None:
            v = ""
        name = f.upper()
    print(f"{name}={shlex.quote(str(v))}")
' "$@" 2>>"$MC_LOG"
}

# mc_git_head DIR -- read-only; empty string if DIR is missing or not a repo.
# Never mutates DIR. No per-call timeout (see the file header).
mc_git_head() {
    local dir="$1"
    [ -n "$dir" ] && [ -d "$dir" ] || { printf ''; return; }
    git -C "$dir" rev-parse HEAD 2>>"$MC_LOG" || printf ''
}

# mc_update_state_json STATE_FILE PY_TRANSFORM
#
# The one shared "lock + atomic-rename JSON update" primitive. Everything --
# acquiring the lock, loading the existing state, running the transform, and
# the atomic write -- happens inside ONE python process (macOS port: no
# `flock`/`timeout` binary involved anywhere). That process:
#   1. opens STATE_FILE.lock and takes a real fcntl.flock(LOCK_EX), retried
#      non-blocking every ~20ms up to a 2s deadline -- exits 97 (mapped to
#      outcome=lock-timeout below) if the deadline passes without the lock.
#   2. loads STATE_FILE (or {} if missing/corrupt/unreadable) into `state`.
#   3. runs PY_TRANSFORM (spliced in verbatim at column 0, exactly as
#      before) against it -- it may read any MC_*-prefixed env var the
#      caller exported beforehand, and must end by printing the new,
#      complete state object via `print(json.dumps(state))` (or print
#      nothing / exit non-zero to make this a no-op write).
#   4. via an atexit hook registered before the transform runs (so it fires
#      whether the transform falls through normally, calls sys.exit(N), or
#      raises), writes whatever was printed to a tmp file and os.replace()s
#      it onto STATE_FILE -- but only if something was actually printed --
#      then releases the lock.
# The transform never sees the existing state through an exported env var or
# argv (only via the `state` dict already loaded into that same process) --
# so a legacy or otherwise-sensitive key already sitting in a caller's
# persisted state (e.g. a pre-fingerprint-scheme raw prompt_id) is never
# inherited by any subprocess that python spawns (re-gate finding, HIGH; the
# same class of bug as dual-gate review finding 1, but for the EXISTING
# state rather than the incoming payload).
#
# Returns the transform's exit code (0 on a normal, evaluated write; a lock
# failure returns 1 after logging outcome=lock-timeout / outcome=lock-open-
# failed). A caller that also needs a *value* out of the transform (not just
# the persisted state) should have the transform write that value to a
# side-channel file of its own choosing (e.g. one named by an MC_*_OUT env
# var) and read it back itself afterward -- this function's own stdout/
# return value carries no such value.
mc_update_state_json() {
    local state_file="$1"
    local py_transform="$2"
    local state_dir
    state_dir="$(dirname "$state_file")"
    mkdir -p "$state_dir" 2>/dev/null || { mc_log "outcome=state-dir-failed dir=$state_dir"; return 1; }

    local rc
    env PYTHONPATH= "$MC_PY" -c "
import atexit, fcntl, io, json, os, sys, time

state_file = sys.argv[1]
lockfile = state_file + '.lock'

try:
    lock_fd = os.open(lockfile, os.O_CREAT | os.O_RDWR, 0o644)
except OSError:
    sys.exit(98)

_deadline = time.time() + 2.0
_locked = False
while True:
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _locked = True
        break
    except OSError:
        if time.time() >= _deadline:
            break
        time.sleep(0.02)
if not _locked:
    os.close(lock_fd)
    sys.exit(97)

_buf = io.StringIO()
_real_stdout = sys.stdout


def _finalize():
    sys.stdout = _real_stdout
    new_json = _buf.getvalue()
    if new_json.strip():
        tmp = state_file + '.tmp.' + str(os.getpid())
        try:
            with open(tmp, 'w') as f:
                f.write(new_json)
            os.replace(tmp, state_file)
        except OSError:
            pass
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
    except OSError:
        pass
    os.close(lock_fd)


atexit.register(_finalize)

existing = '{}'
if os.path.isfile(state_file):
    try:
        with open(state_file) as f:
            existing = f.read() or '{}'
    except OSError:
        existing = '{}'
try:
    state = json.loads(existing)
    if not isinstance(state, dict):
        state = {}
except Exception:
    state = {}

sys.stdout = _buf
$py_transform
" "$state_file" 2>>"$MC_LOG"
    rc=$?

    case $rc in
        97) mc_log "outcome=lock-timeout file=$state_file.lock"; return 1 ;;
        98) mc_log "outcome=lock-open-failed file=$state_file.lock"; return 1 ;;
        *) return $rc ;;
    esac
}

# mc_prune_old_state PROJECT MINUTES -- deletes *.json state files older than
# MINUTES under $MEMCONTINUUM_HOME/sessions/PROJECT (mtime-based; a state
# file's mtime is its last write, i.e. its last activity). Never touches any
# other project's directory, and never touches MEMCONTINUUM_ROOT/CODE_ROOT.
mc_prune_old_state() {
    local project="$1"
    local minutes="$2"
    local dir
    dir="$(mc_state_dir_for "$project")"
    [ -d "$dir" ] || return 0
    find "$dir" -maxdepth 1 -type f -name '*.json' -mmin "+$minutes" -exec rm -f {} + 2>/dev/null || true
}

# mc_path_under_root FILE_PATH ROOT
#
# Symlink-safe containment: does FILE_PATH's real location sit under ROOT's
# real location? Originally hooks/newfile-nudge.sh's own fix (2026-08-31
# review) for its PreToolUse containment check; factored out here so
# hooks/ledger-post-edit.sh shares the SAME implementation instead of
# keeping the plain lexical prefix match that fix already replaced in
# newfile-nudge.sh -- one implementation, both hooks call it.
#
# A plain lexical `case "$FILE_PATH" in "$ROOT"/*` prefix match is fooled
# both by a literal `/../` traversal segment (textually under ROOT while
# actually resolving to a sibling of it) and by a symlinked ancestor
# directory (every path segment textually under ROOT, but the real
# directory it names lives elsewhere). Fixed bash-3.2-safe, no external
# binaries beyond what every caller here already uses:
#   1. reject any literal `/../` traversal segment (or a leading `../`, or
#      a bare `..`) outright, purely as a string -- a syntactic red flag
#      regardless of what it would resolve to.
#   2. canonicalize ROOT and the nearest EXISTING ancestor directory of
#      FILE_PATH (walking up via dirname -- handles both a FILE_PATH that
#      already exists, ledger-post-edit.sh's usual case, and one that does
#      not yet, newfile-nudge.sh's usual case) via `cd ... && pwd -P`,
#      which resolves symlinks, and require that ancestor to sit under the
#      canonicalized root.
#
# Returns WHY, not just yes/no -- newfile-nudge.sh's outcome= vocabulary
# distinguishes these in hook.log (memidx.py stats greps it); a caller
# that only needs yes/no (ledger-post-edit.sh) collapses every nonzero
# into its own single out-of-scope outcome.
#   0  under ROOT
#   1  outside ROOT (both resolve, but FILE_PATH's ancestor is not under it)
#   2  literal `..` traversal segment in FILE_PATH
#   3  ROOT itself does not resolve (missing, not a directory, etc.)
#   4  FILE_PATH has no existing ancestor to resolve from
#   5  FILE_PATH's existing ancestor does not resolve
mc_path_under_root() {
    local file_path="$1" root="$2" root_real ancestor next ancestor_real
    case "$file_path" in
        */../*|*/..|../*|..) return 2 ;;
    esac
    root_real="$(cd "$root" 2>/dev/null && pwd -P)"
    [ -n "$root_real" ] || return 3
    ancestor="$file_path"
    while [ ! -d "$ancestor" ]; do
        next="$(dirname "$ancestor")"
        if [ "$next" = "$ancestor" ]; then
            return 4
        fi
        ancestor="$next"
    done
    ancestor_real="$(cd "$ancestor" 2>/dev/null && pwd -P)"
    [ -n "$ancestor_real" ] || return 5
    case "$ancestor_real" in
        "$root_real"|"$root_real"/*) return 0 ;;
        *) return 1 ;;
    esac
}
