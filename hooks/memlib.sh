#!/usr/bin/env bash
# memlib.sh -- shared helpers for MemContinuum's write-side reminder hooks
# (ledger-post-edit.sh, precompact-persist.sh, sessionstart-remind.sh,
# userprompt-remind.sh, sessionend-stamp.sh). Source this from each hook
# script; it is never executed standalone.
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
#   MEMCONTINUUM_CODE_ROOT   code root these hooks watch edits under (single-
#                        root fallback; see MEMCONTINUUM_CODE_ROOTS below).
#   MEMCONTINUUM_CODE_ROOTS  JSON array of every configured code root's
#                        physical path (design R5, audit MC-P1-05, TOP-0123
#                        L5). mc_code_roots() reads this first and falls
#                        back to the single MEMCONTINUUM_CODE_ROOT above
#                        when unset, so a project with one root never needs
#                        to set both.
#   MEMCONTINUUM_PYTHON      absolute path to the venv python. Falls back to
#                        <engine>/.venv/bin/python (see scripts/repo-init.sh
#                        --bootstrap-venv) when unset.
#
# WRITE-LOCK (ruling E): these scripts' only writable surface is
# $MEMCONTINUUM_HOME/sessions/**/*.json[.lock] and $MEMCONTINUUM_HOME/hook.log.
# Never write anything under MEMCONTINUUM_ROOT (the store) or any code root
# named by MEMCONTINUUM_CODE_ROOT or MEMCONTINUUM_CODE_ROOTS from any
# function in this file or any script that sources it.

export PYTHONPATH=

MC_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"

# mc_path_under_root lives in its own side-effect-free file (symlink-paths
# review round 1, finding 3) so hooks/newfile-nudge.sh can reach it WITHOUT
# paying everything below this line's cost -- see mc-path-lib.sh's own
# header. Sourcing it here costs every OTHER caller of this file nothing
# beyond defining one more function (no I/O, no side effects of its own).
# shellcheck source=mc-path-lib.sh
. "$MC_LIB_DIR/mc-path-lib.sh"

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
    elif f == "tool_input.notebook_path":
        # R5/R6 (audit MC-P1-05/MC-P1-04, TOP-0123 L5/T8): a NotebookEdit
        # payload carries notebook_path, not file_path -- added here so
        # Task 8 does not need to touch memlib.sh itself.
        v = (d.get("tool_input") or {}).get("notebook_path") or ""
        name = "NOTEBOOK_PATH"
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

# mc_code_roots -- prints one PHYSICAL code root per line, read by callers
# via the existing bash-3.2-safe idiom:
#   while IFS= read -r root; do ... ; done < <(mc_code_roots)
# (process substitution, never a pipe -- a pipe would run the loop in a
# subshell and drop any variable assignments made inside it). Design R5
# (audit MC-P1-05, TOP-0123 L5): parses MEMCONTINUUM_CODE_ROOTS (a JSON
# list, repo-init.sh's own esc_cmd(json.dumps(code_roots))) via ONE python
# call -- the same one-python-call discipline mc_extract_fields already
# uses, never a second process per root. Falls back to the single
# MEMCONTINUUM_CODE_ROOT when the list variable is unset (old-shape
# wiring rendered before this task, or a hand-written config) -- the list,
# when present, IS the complete set; the single var is a strict subset/
# legacy alias of it, never additional information, so this never reads
# both.
mc_code_roots() {
    if [ -n "${MEMCONTINUUM_CODE_ROOTS:-}" ]; then
        printf '%s' "$MEMCONTINUUM_CODE_ROOTS" | env PYTHONPATH= "$MC_PY" -c '
import json, sys
try:
    roots = json.load(sys.stdin)
except Exception:
    roots = []
if isinstance(roots, list):
    for r in roots:
        if isinstance(r, str) and r:
            print(r)
' 2>>"$MC_LOG"
    elif [ -n "${MEMCONTINUUM_CODE_ROOT:-}" ]; then
        printf '%s\n' "$MEMCONTINUUM_CODE_ROOT"
    fi
}

# mc_git_head DIR -- read-only; empty string if DIR is missing or not a repo.
# Never mutates DIR. No per-call timeout (see the file header).
mc_git_head() {
    local dir="$1"
    [ -n "$dir" ] && [ -d "$dir" ] || { printf ''; return; }
    git -C "$dir" rev-parse HEAD 2>>"$MC_LOG" || printf ''
}

# mc_code_heads_from CODE_ROOTS_TEXT -- prints "root<TAB>head\n" for every
# non-empty line of CODE_ROOTS_TEXT (mc_code_roots's own newline-separated
# output), via mc_git_head (no python -- git only). LOW-3 (task-7-review.md):
# shared by sessionstart-remind.sh, userprompt-remind.sh, and
# precompact-persist.sh, collapsing the per-root HEAD-reading loop that used
# to be duplicated (byte-for-byte in two of the three) across all three.
# Never spawns python itself -- the caller already paid for the ONE
# mc_code_roots call that produced CODE_ROOTS_TEXT, so folding this in adds
# no new spawn.
mc_code_heads_from() {
    local roots_text="$1"
    local cr
    while IFS= read -r cr; do
        [ -n "$cr" ] || continue
        printf '%s\t%s\n' "$cr" "$(mc_git_head "$cr")"
    done <<<"$roots_text"
}

# mc_head_changed STATE_FILE CODE_HEADS CUR_STORE_SHA FIRST_ROOT
#
# Prints "CODE_CHANGED=true|false", "STORE_CHANGED=true|false", and
# "MOVED_ROOTS=<root>\t<head>\n..." (shlex-quoted, suitable for
# `eval "$(...)"`) -- the shared "did any configured root's HEAD move"
# comparisons. LOW-3 (task-7-review.md): this ~30-line python heredoc used
# to be duplicated byte-for-byte in userprompt-remind.sh and
# precompact-persist.sh; now lives here once. CODE_HEADS is
# mc_code_heads_from's own output ("root<TAB>head\n" lines); STATE_FILE's
# `start_code_shas` ({root: sha}) is the per-root map sessionstart-remind.sh
# writes; `start_code_sha` (singular) is the pre-multi-root legacy value,
# recorded only for the FIRST configured root (repo-init.sh always renders
# MEMCONTINUUM_CODE_ROOT as code_roots[0] whenever any code root is
# configured, so FIRST_ROOT == that value identifies the one root the
# legacy key was ever measuring).
#
# LOW-4 fix (task-7-review.md): a root OTHER than FIRST_ROOT that is
# missing from `start_code_shas` (the transitional window before a resume
# repopulates the map -- see sessionstart-remind.sh's own header comment)
# is treated as UNKNOWN and skipped, never compared against a DIFFERENT
# root's start sha -- the pre-fix fallback compared every such root's
# current HEAD against the first root's own start sha (two unrelated git
# repositories), which could only ever read as a false "changed: yes".
#
# MOVED_ROOTS (TOP-0122 L1 rule 2a, the commit nudge): a SEPARATE, per-
# PROMPT comparison against `last_seen_heads` ({root: sha}, distinct from
# the per-SESSION `start_code_shas` above) -- folded into this same read
# (one state-file load, one CODE_HEADS scan) purely so the common "nothing
# moved" turn costs userprompt-remind.sh no extra python spawn at all. This
# is a CHEAP GATE only, not the authoritative decision: a root missing from
# `last_seen_heads` is treated as unknown and never reported moved (mirrors
# the LOW-4 policy above), and the caller re-derives the real comparison
# (and does the actual bookkeeping write) from a freshly-loaded state
# inside its own locked transform before acting on it -- this avoids ever
# trusting a value read outside a lock as the basis for a write.
mc_head_changed() {
    local state_file="$1"
    local code_heads="$2"
    local cur_store_sha="$3"
    local first_root="$4"
    CODE_HEADS="$code_heads" CUR_STORE_SHA="$cur_store_sha" MC_FIRST_ROOT="$first_root" \
        env PYTHONPATH= "$MC_PY" -c '
import json, os, shlex, sys

try:
    with open(sys.argv[1]) as f:
        state = json.load(f)
    if not isinstance(state, dict):
        state = {}
except Exception:
    state = {}

starts = state.get("start_code_shas")
if not isinstance(starts, dict):
    starts = {}
legacy_start = state.get("start_code_sha") or ""
last_seen = state.get("last_seen_heads")
if not isinstance(last_seen, dict):
    last_seen = {}
first_root = os.environ.get("MC_FIRST_ROOT") or ""
heads = os.environ.get("CODE_HEADS") or ""
code_changed = False
moved_lines = []
for line in heads.splitlines():
    if not line or "\t" not in line:
        continue
    root, cur = line.split("\t", 1)
    if root in starts:
        start = starts[root]
    elif root == first_root:
        start = legacy_start
    else:
        start = None
    if start is not None and cur and cur != start:
        code_changed = True

    prev = last_seen.get(root)
    if prev is not None and cur and cur != prev:
        moved_lines.append(root + "\t" + cur)

cur_store = os.environ.get("CUR_STORE_SHA") or ""
start_store = state.get("start_store_sha") or ""
store_changed = bool(cur_store) and cur_store != start_store

print("CODE_CHANGED=" + shlex.quote("true" if code_changed else "false"))
print("STORE_CHANGED=" + shlex.quote("true" if store_changed else "false"))
print("MOVED_ROOTS=" + shlex.quote("\n".join(moved_lines)))
' "$state_file" 2>>"$MC_LOG"
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

# mc_path_under_root now lives in mc-path-lib.sh (sourced near the top of
# this file) -- see that file's own header/doc comment.
