#!/usr/bin/env bash
# mc-watchdog.sh -- shared watchdog-launcher Python source (finding 1
# dedup), sourced by every write-side hook script's guard preamble
# (ledger-post-edit.sh, precompact-persist.sh, sessionstart-remind.sh,
# userprompt-remind.sh, sessionend-stamp.sh) BEFORE `source memlib.sh`.
#
# This file does exactly one thing: a single-quoted heredoc assignment to
# MC_WATCHDOG_LAUNCHER_PY (via `$(cat <<'EOF' ... EOF)`, bash-3.2 safe --
# no arrays, no [[ ]], no process substitution). No filesystem/mkdir/
# subprocess work of its own, so sourcing it adds no measurable time ahead
# of the watchdog's own deadline -- the same invariant
# test_outer_deadline_covers_memlib_sourcing enforces for memlib.sh itself
# (nothing slow may run before the guard starts). It is deliberately NOT
# folded into memlib.sh: that test proves the guard bounds even a
# slow/hung memlib.sh source by running the guard strictly BEFORE
# memlib.sh is sourced at all -- a guard living inside memlib.sh would run
# AFTER any injected slowness in that same file, defeating the exact
# property under test.
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
MC_WATCHDOG_LAUNCHER_PY="$(cat <<'MC_WATCHDOG_PY_EOF'
import atexit, os, signal, subprocess, sys
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


def _kill_group():
    if proc is None:
        return
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except Exception:
        pass


def _on_signal(signum, frame):
    _kill_group()
    sys.exit(0)


signal.signal(signal.SIGTERM, _on_signal)
signal.signal(signal.SIGINT, _on_signal)
atexit.register(_kill_group)

try:
    proc = subprocess.Popen(sys.argv[1:], start_new_session=True)
except Exception:
    sys.exit(0)
try:
    proc.wait(timeout=budget)
except subprocess.TimeoutExpired:
    pass
# Unconditional group sweep (not just on a timeout): a hung call several
# layers deep can background a detached descendant that inherits the
# real stdout/stderr fds, which would otherwise keep those pipes open
# past the point the main script logically finished, even though it
# exited on time. Reaping the whole group here, always, is the actual
# orphaned-grandchild fix -- killing the group only on the timeout branch
# still leaves this exact gap on the success path.
_kill_group()
try:
    proc.wait(timeout=1)
except Exception:
    pass
sys.exit(0)
MC_WATCHDOG_PY_EOF
)"
