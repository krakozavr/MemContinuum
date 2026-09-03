#!/usr/bin/env bash
# mc-watchdog.sh -- shared watchdog-launcher Python source (finding 1
# dedup), sourced by every write-side hook script's guard preamble
# (ledger-post-edit.sh, precompact-persist.sh, sessionstart-remind.sh,
# userprompt-remind.sh, sessionend-stamp.sh, newfile-nudge.sh) BEFORE
# `source memlib.sh`. F6 (external-review fix round): pre-edit-chain.sh
# sources this file too, in the same guard-preamble position -- the one
# exception to "BEFORE memlib.sh" above, since pre-edit-chain.sh never
# sources memlib.sh at all (it has no write-side state of its own).
#
# This file does two things: a single-quoted heredoc assignment to
# MC_WATCHDOG_LAUNCHER_PY (via `read -r -d ''`, bash-3.2 safe -- no arrays,
# no [[ ]], no process substitution, no external `cat`), and resolving
# MC_GUARD_PY -- the python that launches that watchdog -- via the SAME
# three-step order memlib.sh uses for MC_PY ($MEMCONTINUUM_PYTHON ->
# $MEMCONTINUUM_HOME/config.sh -> <engine>/.venv/bin/python; F6 fix, round
# 4). Before this, the guard preamble in all six hooks resolved python with
# only two steps (env or engine venv, skipping config.sh), so a non-default
# venv (--venv elsewhere, or an existing --python handed to
# memcontinuum-setup.sh) left the watchdog guard itself silently unguarded
# in every hand/legacy install that relies on config.sh rather than an
# explicit MEMCONTINUUM_PYTHON in the hook line (installer-rendered hook
# lines bake MEMCONTINUUM_PYTHON directly and never depended on this step).
# The config.sh source is swallowed by `|| true`, so a missing/corrupt
# config.sh can only cost sourcing time, never block a hook -- same
# fail-open guarantee as memlib.sh's own step. This relies on SCRIPT_DIR
# already being set by the sourcing hook (every caller sets it before
# sourcing this file). No filesystem/mkdir/subprocess work beyond that one
# `[ -f ]` stat and (at most) sourcing a few config.sh assignment lines --
# nothing that shells out (no `cat`, no `sed`) -- so sourcing this file
# still adds no more than sourcing cost ahead of the watchdog's own
# deadline -- the same invariant test_outer_deadline_covers_memlib_sourcing
# enforces for memlib.sh itself (nothing SLOW, i.e. no external process,
# may run before the guard starts). It is deliberately NOT folded into
# memlib.sh: that test proves the guard bounds even a slow/hung memlib.sh
# source by running the guard strictly BEFORE memlib.sh is sourced at all
# -- a guard living inside memlib.sh would run AFTER any injected slowness
# in that same file, defeating the exact property under test.
#
# Used as: "$MC_GUARD_PY" -c "$MC_WATCHDOG_LAUNCHER_PY" "${BASH:-bash}"
# "${BASH_SOURCE[0]}" "$@" -- passing the source text itself (not a file
# path) as the `-c` argument keeps the `MC_WATCHDOG_LAUNCHER` marker
# comment inside it visible in argv, which
# test_overall_two_second_budget_bounds_sequential_subprocess_calls (and
# friends) rely on to distinguish the launcher's own startup from the
# hook's later WORK calls.
#
# Before this file existed, this ~40-line python blob was pasted
# identically into all five hook scripts' own guard blocks -- one bug fix
# (e.g. the SIGTERM/SIGINT + atexit handling below, finding 1) used to
# mean five identical edits made in lockstep; now it means one.
#
# Orphan fix (finding 1): previously nothing guarded the launcher process
# ITSELF against being killed -- if the harness sends this launcher a
# SIGTERM/SIGINT while it's inside proc.wait() (or start_new_session's
# child hasn't finished starting yet), the child's whole process group
# (start_new_session=True, so it outlives the launcher's own death by
# default) would be orphaned with no bound at all. A signal handler for
# SIGTERM/SIGINT plus an atexit hook now killpg the child group on every
# exit path, not just the normal timeout/success ones.
# Finding 5 (MEDIUM): this used to be `X="$(cat <<'EOF' ... EOF)"` -- a
# command substitution that forks+execs the external `cat` binary BEFORE
# this file's own "no filesystem/subprocess work of its own" guarantee
# even starts to hold, i.e. before the watchdog's own deadline clock below
# starts ticking at all (that only starts once the guarded script's Popen
# call runs, strictly after this assignment completes). A slow/hung `cat`
# on PATH at that point would blow the whole invocation's wall time with
# no bound whatsoever. `read -r -d ''` is a bash BUILTIN (bash 3.2 safe --
# the -d flag has existed since bash 2.04): it reads the heredoc directly
# off its own stdin with no external process and no subshell at all. It
# returns 1 because the heredoc never contains a NUL delimiter (read hits
# real EOF instead of finding one) -- expected and harmless, `|| true`
# only exists so `set -e` callers (none today, but never assume) aren't
# tripped by that nonzero-but-successful read.
IFS= read -r -d '' MC_WATCHDOG_LAUNCHER_PY <<'MC_WATCHDOG_PY_EOF' || true
import atexit, os, signal, subprocess, sys, time
from datetime import datetime
# MC_WATCHDOG_LAUNCHER: runs the real hook script as a child in its own
# process group and enforces a wall-clock budget (MC_WATCHDOG_BUDGET env,
# seconds, default 2 -- SessionEnd sets 1.2, its harness budget is 1.5s)
# for the whole run. On expiry, OR if this launcher itself receives
# SIGTERM/SIGINT, kills the entire child group so no descendant can
# outlive the deadline, then always exits 0.
try:
    budget = float(os.environ.get("MC_WATCHDOG_BUDGET") or 2)
except ValueError:
    budget = 2.0

proc = None
# Finding 6 (LOW, startup signal race): a SIGTERM/SIGINT can land while
# `proc` is still None -- strictly between signal.signal() registering
# below and the `proc = subprocess.Popen(...)` assignment a few lines
# down actually completing (CPython checks for a pending signal at GIL
# reacquisition points inside a C call like Popen's own fork/exec, not
# only between Python bytecodes -- so the real child process can already
# exist even though this module's own `proc` name isn't bound to it yet).
# In that window the handler has no pid to kill at all, so it must NOT
# exit immediately (that would leak the about-to-exist child forever,
# unbounded) -- it only records the request; the code right after Popen
# returns checks it and kills immediately once `proc` is real.
pending_kill = False


def _kill_group():
    if proc is None:
        return
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except Exception:
        pass


def _log_watchdog_kill():
    # Every watchdog kill must leave one log line: on budget expiry the
    # guarded child is killed before it ever gets a chance to write its
    # own outcome line (it may be mid-call, or long before its own
    # finish()), so the invocation would otherwise leave NO trace at all
    # in hook.log. sys.argv here is [ "-c", "$BASH", "$BASH_SOURCE[0]",
    # ...rest ] (see this file's own "Used as:" header comment) --
    # argv[2] is the guarded hook script's own path. Best-effort only,
    # like every other log write in this repo: never lets a logging
    # failure change the launcher's own exit behavior.
    try:
        home = os.environ.get("MEMCONTINUUM_HOME") or os.path.join(
            os.path.expanduser("~"), ".memcontinuum"
        )
        hook_name = os.path.basename(sys.argv[2]) if len(sys.argv) > 2 else "unknown"
        # Round-2 review finding: this used to be a NAIVE `%Y-%m-%dT%H:%M:%S`
        # (no UTC offset) -- memidx.py stats' parser requires an
        # offset-bearing timestamp (the same `date -Iseconds 2>/dev/null ||
        # date` idiom every other hook.log line uses), so this line was
        # ALWAYS unparseable, never counted in any window. astimezone()
        # attaches the local UTC offset with no subprocess call (this is a
        # best-effort log write on an already-past-the-kill path -- adding
        # a `date` shell-out here would be a new failure mode for exactly
        # the moment this function exists to survive).
        try:
            ts = datetime.now().astimezone().isoformat()
        except Exception:
            ts = time.strftime("%Y-%m-%dT%H:%M:%S")
        # project=: this launcher runs BEFORE memlib.sh's own MC_PROJECT
        # resolution (basename(MEMCONTINUUM_ROOT) / "default" fallback) --
        # by design, so a slow/hung memlib.sh source can't blow the
        # watchdog's own deadline (see this file's header). Use
        # MEMCONTINUUM_PROJECT from the environment when the caller already
        # set/baked it (the common installer-rendered case); otherwise this
        # line genuinely cannot know the real project, and must say so
        # rather than ever emitting a bare line with none at all.
        project = os.environ.get("MEMCONTINUUM_PROJECT") or "(pre-resolution)"
        os.makedirs(home, exist_ok=True)
        with open(os.path.join(home, "hook.log"), "a") as f:
            f.write(f"{ts} outcome=watchdog-killed hook={hook_name} project={project}\n")
    except Exception:
        pass


def _on_signal(signum, frame):
    global pending_kill
    pending_kill = True
    if proc is not None:
        _kill_group()
        sys.exit(0)
    # else: proc not assigned yet -- return normally (do NOT sys.exit
    # here) so the in-flight subprocess.Popen() call/assignment below can
    # finish; the pending_kill check right after it takes over from here.


signal.signal(signal.SIGTERM, _on_signal)
signal.signal(signal.SIGINT, _on_signal)
atexit.register(_kill_group)

try:
    proc = subprocess.Popen(sys.argv[1:], start_new_session=True)
except Exception:
    sys.exit(0)
if pending_kill:
    _kill_group()
    sys.exit(0)
try:
    proc.wait(timeout=budget)
except subprocess.TimeoutExpired:
    # Coordinator review fix: kill (and reap) the whole child group FIRST,
    # strictly before this process writes anything of its own to the
    # shared stdout fd. The child inherits this launcher's real stdout
    # directly (no pipe in between, see the header's "Used as" note) --
    # while it is still alive and unreaped, it can still write to that
    # SAME fd, so writing the fallback before killing it left a real
    # window (this branch's own _log_watchdog_kill() file I/O alone is
    # enough) for the child's own output to land after -- or wrapped
    # around -- the fallback text, corrupting what Claude Code reads.
    # Once the group is confirmed dead (killed, then reaped with a bounded
    # wait -- not merely signaled), nothing it does can reach stdout ever
    # again, so the fallback write below is guaranteed to be the last
    # thing this whole process tree puts on that fd.
    _kill_group()
    try:
        proc.wait(timeout=1)
    except Exception:
        pass
    _log_watchdog_kill()
    # F6 (external-review fix round): opt-in timeout fallback -- for the
    # six guarded hooks that never set this, `fallback` is None/empty and
    # this is a no-op, preserving today's silent-on-timeout behavior
    # unchanged. pre-edit-chain.sh sets MC_WATCHDOG_TIMEOUT_FALLBACK to a
    # minimal, valid additionalContext JSON payload stating retrieval timed
    # out and that absence of a decision was NOT established, so Claude
    # Code reads a real uncertainty signal instead of an empty
    # additionalContext indistinguishable from "retrieval ran and found
    # nothing".
    fallback = os.environ.get("MC_WATCHDOG_TIMEOUT_FALLBACK")
    if fallback:
        try:
            sys.stdout.write(fallback)
            sys.stdout.flush()
        except Exception:
            pass
    sys.exit(0)
# Unconditional group sweep (not just on a timeout): a hung call several
# layers deep can background a detached descendant that inherits the
# real stdout/stderr fds, which would otherwise keep those pipes open
# past the point the main script logically finished, even though it
# exited on time. Reaping the whole group here, always, is the actual
# orphaned-grandchild fix -- the timeout branch above already does its
# own kill+reap (and returns before reaching here); this is the
# success-path twin of that same guarantee.
_kill_group()
try:
    proc.wait(timeout=1)
except Exception:
    pass
sys.exit(0)
MC_WATCHDOG_PY_EOF

# MC_GUARD_PY resolution (F6 fix, round 4) -- see the file header comment
# above. Mirrors hooks/memlib.sh's own MC_PY resolution exactly; kept
# separate from memlib.sh because this file is sourced BEFORE memlib.sh
# (see the header's "deliberately NOT folded into memlib.sh" note). Relies
# on the sourcing hook having already set SCRIPT_DIR and (optionally)
# MEMCONTINUUM_HOME.
# R2/R3 fix, round 4: the file just sourced below may be a POINTER (a
# custom-HOME install also writes a minimal config.sh at the fixed default
# path recording only the real MEMCONTINUUM_HOME -- memcontinuum-setup.sh
# "3. config"). If sourcing it just redefined MEMCONTINUUM_HOME to a
# DIFFERENT directory than the file we sourced, follow through and source
# the REAL config.sh too, so MEMCONTINUUM_PYTHON actually resolves there.
# Unconditional on MEMCONTINUUM_PYTHON already being set (R3): config.sh's
# own `if [ -z "${MEMCONTINUUM_PYTHON:-}" ]` guard keeps env/baked
# precedence for PYTHON either way -- the guard itself, and every hook's
# session state/hook.log under it, must resolve to the real HOME even when
# PYTHON was already baked into the installer-rendered hook line.
MEMCONTINUUM_HOME="${MEMCONTINUUM_HOME:-$HOME/.memcontinuum}"
MC_HOME_CONFIG_1="$MEMCONTINUUM_HOME/config.sh"
if [ -f "$MC_HOME_CONFIG_1" ]; then
    # shellcheck source=/dev/null
    . "$MC_HOME_CONFIG_1" 2>/dev/null || true
fi
# A damaged-but-sourceable config may have `unset MEMCONTINUUM_HOME` --
# re-default after every source so `set -u` can never trip on it (regate
# round 2).
MEMCONTINUUM_HOME="${MEMCONTINUUM_HOME:-$HOME/.memcontinuum}"
if [ "$MEMCONTINUUM_HOME/config.sh" != "$MC_HOME_CONFIG_1" ] && [ -f "$MEMCONTINUUM_HOME/config.sh" ]; then
    # shellcheck source=/dev/null
    . "$MEMCONTINUUM_HOME/config.sh" 2>/dev/null || true
    MEMCONTINUUM_HOME="${MEMCONTINUUM_HOME:-$HOME/.memcontinuum}"
fi
unset MC_HOME_CONFIG_1
# The guarded hook re-execs ITSELF as a child under the launcher -- the
# child must see the HOME just resolved (possibly via the pointer), or it
# re-defaults and writes session state/hook.log under ~/.memcontinuum
# again (regate round 2). Export is safe: same value this chain would
# resolve, just made visible across the re-exec boundary.
export MEMCONTINUUM_HOME
MC_GUARD_PY="${MEMCONTINUUM_PYTHON:-$SCRIPT_DIR/../.venv/bin/python}"
