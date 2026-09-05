#!/usr/bin/env bash
# git post-commit hook for the STORE repo (memory/, not this tool repo):
# reindexes the store's markdown into the derived SQLite index after every
# commit, so `search`/`chain`/`for-path` see the new content immediately
# instead of waiting for the next manual `reindex`.
#
# Symlink or copy this as .git/hooks/post-commit inside the store repo --
# see install-hooks.md.
#
# Design R8 (audit MC-P2-02, TOP-0123 L7): a full (embedding) reindex can
# take tens of seconds on a real store (measured ~40s for a 49-file store,
# cold process/warm fastembed cache) -- unacceptable inside a synchronous
# `git commit`. This hook now does two things instead of one:
#   1. A bounded CONTENT pass only (`reindex --no-embed --auto`), run
#      through the shared watchdog launcher (hooks/mc-watchdog.sh) under
#      MEMCONTINUUM_POST_COMMIT_BUDGET (default 30s) -- text content is
#      always current the moment this hook returns; nothing here ever
#      calls the embedding backend, so a hung/slow embedding backend can
#      never delay a commit.
#   2. When the content pass leaves records without a fresh vector, this
#      hook touches a per-project marker file and spawns a detached
#      `embed-worker` subcommand (Python subprocess.Popen with a new
#      session -- never bash `&`, never a `setsid` binary, which macOS
#      does not ship) that backfills embeddings in the background and
#      coalesces repeated commits into one pass. See `memidx.py
#      embed-worker --help` and docs/INTERNALS.md's watchdog/embedding-
#      lifecycle sections for the full lock/marker/crash contract.
#
# Env (same names/defaults as pre-edit-chain.sh):
#   MEMCONTINUUM_ROOT     the store's markdown root to reindex. Required --
#                     unlike the pre-edit hook, this one DOES pass it to
#                     memidx.py, as `reindex --root`.
#   MEMCONTINUUM_PROJECT  project namespace. Defaults to $(basename "$MEMCONTINUUM_ROOT"),
#                     else "default". The literal project-name default lives
#                     only in project wiring, never in this script.
#   MEMCONTINUUM_HOME     passed through to memidx.py unchanged; also where this
#                     script's own log line, the embed marker/lock/log, and
#                     the index db all live. Defaults to ~/.memcontinuum,
#                     matching memidx.py's default. Resolved (with the
#                     config.sh pointer-chain fallback) by hooks/mc-watchdog.sh
#                     below -- not duplicated in this file.
#   MEMCONTINUUM_PYTHON   absolute path to the venv python. Falls back to
#                     $MEMCONTINUUM_HOME/config.sh (if it sets MEMCONTINUUM_PYTHON),
#                     then <engine>/.venv/bin/python (scripts/repo-init.sh
#                     --bootstrap-venv) when unset.
#   MEMCONTINUUM_POST_COMMIT_BUDGET  wall-clock budget in seconds for the
#                     content pass (default 30). Passed to the shared
#                     watchdog launcher as MC_WATCHDOG_BUDGET.
#   MEMCONTINUUM_EMBED_WORKER  set to "0" to disable spawning the
#                     background embed-worker entirely (tests/CI use this
#                     where a detached background process would outlive
#                     the run) -- the marker is still touched, so `stats`/
#                     `check --json` still show the backlog; nothing
#                     backfills it until a worker is run by hand or a
#                     later commit spawns one with this unset.

set -u
export PYTHONPATH=

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
MEMIDX="$SCRIPT_DIR/../memidx.py"

# Watchdog guard (design R8): must be the literal first thing after
# resolving SCRIPT_DIR and sourcing mc-watchdog.sh, same position/reasoning
# as every other guarded hook (see mc-watchdog.sh's own header and
# precompact-persist.sh's identical block). mc-watchdog.sh's sourced tail
# resolves MEMCONTINUUM_HOME itself (default, then the config.sh pointer
# chain -- the same steps this file used to duplicate inline) and exports
# it, so nothing else in this file needs to repeat that chain; it also
# resolves MC_GUARD_PY via the same $MEMCONTINUUM_PYTHON -> config.sh ->
# engine-venv order PY below uses. Budget defaults to 30s here (a real
# content pass is measured well under 1s; 30s is generous headroom for a
# slow filesystem, not a tuned ceiling) -- set before the guard so the
# launcher (which reads MC_WATCHDOG_BUDGET from the environment) sees it.
# shellcheck source=mc-watchdog.sh
source "${MC_WATCHDOG_LIB_PATH:-$SCRIPT_DIR/mc-watchdog.sh}" 2>/dev/null
if [ -z "${MC_UNDER_TIMEOUT:-}" ]; then
    export MC_UNDER_TIMEOUT=1
    export MC_WATCHDOG_BUDGET="${MEMCONTINUUM_POST_COMMIT_BUDGET:-30}"
    if [ -x "${MC_GUARD_PY:-}" ] && [ -n "${MC_WATCHDOG_LAUNCHER_PY:-}" ]; then
        "$MC_GUARD_PY" -c "$MC_WATCHDOG_LAUNCHER_PY" "${BASH:-bash}" "${BASH_SOURCE[0]}" "$@"
        exit 0
    fi
fi

# From here on: the guarded child. MEMCONTINUUM_HOME is already resolved
# and exported by mc-watchdog.sh above.
MEMCONTINUUM_HOME="${MEMCONTINUUM_HOME:-$HOME/.memcontinuum}"
PY="${MEMCONTINUUM_PYTHON:-$SCRIPT_DIR/../.venv/bin/python}"
LOG="$MEMCONTINUUM_HOME/hook.log"

mkdir -p "$MEMCONTINUUM_HOME" 2>/dev/null

if [ -z "${MEMCONTINUUM_ROOT:-}" ]; then
    # Can't derive PROJECT the normal way (basename of a ROOT we don't
    # have) -- fall back to MEMCONTINUUM_PROJECT/"default", same as every
    # other hook's fallback chain, so this line still carries project=
    # (round-2 review finding: "every hook.log line carries project=" was
    # still false for this one).
    _SKIP_PROJECT="${MEMCONTINUUM_PROJECT:-default}"
    printf '%s post-commit-reindex: MEMCONTINUUM_ROOT not set, skipping project=%s\n' \
        "$(date -Iseconds 2>/dev/null || date)" "$_SKIP_PROJECT" >>"$LOG" 2>/dev/null || true
    exit 0
fi

PROJECT="${MEMCONTINUUM_PROJECT:-$(basename "$MEMCONTINUUM_ROOT")}"
DB_PATH="$MEMCONTINUUM_HOME/$PROJECT.sqlite"

# $EPOCHREALTIME is a bash 5-ism (unbound under `set -u` on macOS's stock
# bash 3.2); `date +%s` (whole seconds -- nothing downstream parses the
# elapsed value, so the lost sub-second precision costs nothing) is the
# portable substitute, matching BSD date (no `%N`) same as GNU date.
START_TS=$(date +%s 2>/dev/null || echo 0)
# Design R8: `--no-embed --auto` -- content only, never the embedding
# backend, so this call can never hang on a slow/broken embedding model.
# `cmd_reindex` prints "N record(s) awaiting embedding" at the end of its
# summary line whenever a row's vector is missing, stale, or from a
# different fingerprint -- parsed below into EMBED_STATE.
OUT="$(PYTHONPATH= "$PY" "$MEMIDX" reindex --root "$MEMCONTINUUM_ROOT" --project "$PROJECT" --db "$DB_PATH" --no-embed --auto 2>&1)"
RC=$?
NOW_TS=$(date +%s 2>/dev/null || echo "$START_TS")
ELAPSED=$(( NOW_TS - START_TS ))

# Parse "N record(s) awaiting embedding" off the end of the summary line
# without `[[ =~ ]]` (bash-3.2-safe: plain case/parameter expansion only).
# EMBED_STATE defaults to "skipped" -- the content pass failed (a non-zero
# rc, e.g. an integrity failure) or was killed by the watchdog before it
# ever printed a summary line at all (in the killed case, this file's own
# printf below never even runs -- the launcher already wrote its own
# `outcome=watchdog-killed hook=post-commit-reindex.sh` line and exited).
EMBED_STATE="skipped"
E=""
if [ "$RC" -eq 0 ]; then
    case "$OUT" in
        *"record(s) awaiting embedding")
            TAIL="${OUT##*, }"
            E="${TAIL%% *}"
            ;;
    esac
    case "$E" in
        ''|*[!0-9]*) E="" ;;
    esac
    if [ -n "$E" ]; then
        if [ "$E" -gt 0 ]; then
            EMBED_STATE="pending"
        else
            EMBED_STATE="clean"
        fi
    fi
fi

if [ "$EMBED_STATE" = "pending" ]; then
    MARKER="$MEMCONTINUUM_HOME/$PROJECT.embed-pending"
    touch "$MARKER" 2>/dev/null || true
    if [ "${MEMCONTINUUM_EMBED_WORKER:-1}" != "0" ]; then
        LOGF="$MEMCONTINUUM_HOME/$PROJECT.embed.log"
        # Design R8: spawned from Python (subprocess.Popen with a new
        # session), never bash `&` and never a `setsid` binary (macOS
        # ships neither `setsid` nor `flock`/`timeout` -- see
        # docs/INTERNALS.md's watchdog section). The watchdog launcher
        # above kills the WHOLE process group of its guarded child on
        # timeout (or its own death) via `os.killpg` -- a plain `bash foo
        # &` job stays inside that same group and would die with it,
        # before ever getting to run. `start_new_session=True` puts this
        # worker in a brand-new process group/session that `killpg`
        # cannot reach, so a watchdog kill (or the launcher's own
        # unconditional post-success group sweep) can never take the
        # worker down with it. Uses "$PY" (this hook's own resolved
        # python, not sys.executable) as the WORKER's interpreter too, so
        # a test that points MEMCONTINUUM_PYTHON at a stub keeps that
        # same stub in the spawned worker.
        "$PY" -c '
import subprocess
import sys

py, memidx_path, root, project, db, log_path = sys.argv[1:7]
log = open(log_path, "ab")
subprocess.Popen(
    [py, memidx_path, "embed-worker", "--root", root, "--project", project, "--db", db],
    start_new_session=True,
    stdin=subprocess.DEVNULL,
    stdout=log,
    stderr=log,
    close_fds=True,
)
' "$PY" "$MEMIDX" "$MEMCONTINUUM_ROOT" "$PROJECT" "$DB_PATH" "$LOGF" >/dev/null 2>&1 || true
    fi
fi

printf '%s post-commit-reindex: rc=%s elapsed=%ss project=%s root=%s embed=%s :: %s\n' \
    "$(date -Iseconds 2>/dev/null || date)" "$RC" "$ELAPSED" "$PROJECT" "$MEMCONTINUUM_ROOT" "$EMBED_STATE" "$OUT" >>"$LOG" 2>/dev/null || true

# A commit should never be blocked by a reindex failure.
exit 0
